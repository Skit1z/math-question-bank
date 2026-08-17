"""Small, versioned SQLite migrations for MathBank.

The project intentionally avoids a heavyweight migration framework.  This
module keeps schema upgrades explicit, backup-first, and fail-closed.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from mathbank.paths import SCHEMA_SNAPSHOT_DIR


LATEST_SCHEMA_VERSION = 4
REQUIRED_TABLES = {"questions", "question_curriculums", "papers", "paper_questions"}


def _database_path(engine: Engine) -> Path | None:
    database = engine.url.database
    if not database or database == ":memory:":
        return None
    return Path(database).resolve()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def schema_version(engine: Engine) -> int:
    with engine.connect() as connection:
        return int(connection.exec_driver_sql("PRAGMA user_version").scalar_one())


def create_pre_migration_backup(
    engine: Engine,
    *,
    from_version: int,
    to_version: int,
) -> Path | None:
    """Create and verify a consistent SQLite snapshot before a migration."""

    database_path = _database_path(engine)
    if database_path is None or not database_path.exists():
        return None

    backup_dir = SCHEMA_SNAPSHOT_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backup_dir / (
        f"{database_path.stem}.schema-v{from_version}-to-v{to_version}.{timestamp}.db"
    )

    with closing(sqlite3.connect(database_path)) as source, closing(
        sqlite3.connect(backup_path)
    ) as target:
        source.backup(target)
        journal_mode = str(target.execute("PRAGMA journal_mode=DELETE").fetchone()[0])
        if journal_mode.lower() != "delete":
            raise RuntimeError(
                f"迁移前快照无法转换为独立日志模式: {journal_mode}"
            )
        integrity = target.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError(f"迁移前数据库快照校验失败: {integrity}")

    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{backup_path}{suffix}")
        if suffix == "-wal" and sidecar.exists() and sidecar.stat().st_size:
            raise RuntimeError(f"迁移前快照仍依赖未归档 WAL: {sidecar}")
        sidecar.unlink(missing_ok=True)

    backup_path.chmod(0o600)
    checksum_path = backup_path.with_suffix(backup_path.suffix + ".sha256")
    checksum_path.write_text(f"{_sha256(backup_path)}  {backup_path.name}\n", encoding="utf-8")
    checksum_path.chmod(0o600)
    return backup_path


def _rebuild_relationship_tables(engine: Engine) -> dict[str, int]:
    """Rebuild relation tables with real FKs while repairing legacy drift."""

    stats: dict[str, int] = {}
    # AUTOCOMMIT lets us issue an explicit BEGIN IMMEDIATE.  Relying on the
    # sqlite3 driver's implicit transaction behavior is unsafe for DDL because
    # older driver modes may otherwise auto-commit CREATE/DROP statements.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        transaction_started = False
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            transaction_started = True
            before_curriculums = int(
                connection.exec_driver_sql("SELECT COUNT(*) FROM question_curriculums").scalar_one()
            )
            before_paper_questions = int(
                connection.exec_driver_sql("SELECT COUNT(*) FROM paper_questions").scalar_one()
            )

            connection.exec_driver_sql("DROP TABLE IF EXISTS question_curriculums__new")
            connection.exec_driver_sql(
                """
                CREATE TABLE question_curriculums__new (
                    id INTEGER NOT NULL PRIMARY KEY,
                    question_id INTEGER NOT NULL,
                    version_code VARCHAR(50) NOT NULL,
                    exam_track VARCHAR(100) DEFAULT '',
                    subject VARCHAR(100) DEFAULT '',
                    topic VARCHAR(100) DEFAULT '',
                    CONSTRAINT uq_question_curriculum_version
                        UNIQUE (question_id, version_code),
                    CONSTRAINT fk_question_curriculums_question
                        FOREIGN KEY(question_id) REFERENCES questions(id) ON DELETE CASCADE
                )
                """
            )
            connection.exec_driver_sql(
                """
                INSERT INTO question_curriculums__new
                    (id, question_id, version_code, exam_track, subject, topic)
                SELECT qc.id, qc.question_id, qc.version_code,
                       qc.exam_track, qc.subject, qc.topic
                FROM question_curriculums AS qc
                JOIN questions AS q ON q.id = qc.question_id
                JOIN (
                    SELECT question_id, version_code, MAX(id) AS keep_id
                    FROM question_curriculums
                    GROUP BY question_id, version_code
                ) AS newest ON newest.keep_id = qc.id
                """
            )
            connection.exec_driver_sql("DROP TABLE question_curriculums")
            connection.exec_driver_sql(
                "ALTER TABLE question_curriculums__new RENAME TO question_curriculums"
            )
            connection.exec_driver_sql(
                "CREATE INDEX idx_question_curriculums_lookup "
                "ON question_curriculums (version_code, exam_track, subject, topic)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX idx_question_curriculums_qid "
                "ON question_curriculums (question_id)"
            )

            connection.exec_driver_sql("DROP TABLE IF EXISTS paper_questions__new")
            connection.exec_driver_sql(
                """
                CREATE TABLE paper_questions__new (
                    id INTEGER NOT NULL PRIMARY KEY,
                    paper_id INTEGER NOT NULL,
                    question_id INTEGER NOT NULL,
                    order_index INTEGER NOT NULL DEFAULT 0,
                    score INTEGER NOT NULL DEFAULT 5 CHECK (score >= 0),
                    CONSTRAINT uq_paper_question_order UNIQUE (paper_id, order_index),
                    CONSTRAINT fk_paper_questions_paper
                        FOREIGN KEY(paper_id) REFERENCES papers(id) ON DELETE CASCADE,
                    CONSTRAINT fk_paper_questions_question
                        FOREIGN KEY(question_id) REFERENCES questions(id) ON DELETE CASCADE
                )
                """
            )
            connection.exec_driver_sql(
                """
                INSERT INTO paper_questions__new
                    (id, paper_id, question_id, order_index, score)
                SELECT pq.id, pq.paper_id, pq.question_id,
                       COALESCE(pq.order_index, 0),
                       CASE WHEN pq.score IS NULL OR pq.score < 0 THEN 0 ELSE pq.score END
                FROM paper_questions AS pq
                JOIN papers AS p ON p.id = pq.paper_id
                JOIN questions AS q ON q.id = pq.question_id
                JOIN (
                    SELECT paper_id, COALESCE(order_index, 0), MAX(id) AS keep_id
                    FROM paper_questions
                    GROUP BY paper_id, COALESCE(order_index, 0)
                ) AS newest ON newest.keep_id = pq.id
                """
            )
            connection.exec_driver_sql("DROP TABLE paper_questions")
            connection.exec_driver_sql(
                "ALTER TABLE paper_questions__new RENAME TO paper_questions"
            )
            connection.exec_driver_sql(
                "CREATE INDEX idx_paper_questions_paper_id ON paper_questions (paper_id)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX idx_paper_questions_question_id ON paper_questions (question_id)"
            )
            connection.exec_driver_sql(
                """
                UPDATE papers
                SET total_score = COALESCE(
                    (
                        SELECT SUM(pq.score)
                        FROM paper_questions AS pq
                        WHERE pq.paper_id = papers.id
                    ),
                    0
                )
                """
            )

            remaining_curriculums = int(
                connection.exec_driver_sql("SELECT COUNT(*) FROM question_curriculums").scalar_one()
            )
            remaining_paper_questions = int(
                connection.exec_driver_sql("SELECT COUNT(*) FROM paper_questions").scalar_one()
            )
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"迁移后仍存在外键异常: {violations[:5]}")

            connection.exec_driver_sql("PRAGMA user_version=3")
            connection.exec_driver_sql("COMMIT")
            transaction_started = False
            stats = {
                "removed_question_curriculums": before_curriculums - remaining_curriculums,
                "removed_paper_questions": before_paper_questions - remaining_paper_questions,
            }
        except Exception:
            if transaction_started:
                connection.exec_driver_sql("ROLLBACK")
            raise
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            enabled = int(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one())
            if enabled != 1:
                raise RuntimeError("迁移连接未能恢复 SQLite 外键检查")
    return stats


def _migrate_v3_to_v4_sources(engine: Engine) -> dict[str, int]:
    """Introduce the standalone sources table and structured question refs (v4).

    Legacy ``questions.source`` free-text values each become one source row and
    the questions table is rebuilt without that column.  Source tables created
    by ``create_all`` ahead of the version stamp are detected and only stamped.
    """

    stats: dict[str, int] = {"created_sources": 0}
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        transaction_started = False
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            transaction_started = True

            question_columns = {
                row[1]
                for row in connection.exec_driver_sql(
                    'PRAGMA table_info("questions")'
                ).fetchall()
            }
            if "source" not in question_columns:
                # Nothing legacy to convert; just stamp the version.
                connection.exec_driver_sql("PRAGMA user_version=4")
                connection.exec_driver_sql("COMMIT")
                transaction_started = False
                return stats

            connection.exec_driver_sql(
                """
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER NOT NULL PRIMARY KEY,
                    name VARCHAR(100) NOT NULL UNIQUE,
                    series VARCHAR(100) DEFAULT '',
                    note TEXT DEFAULT '',
                    created_at DATETIME
                )
                """
            )
            connection.exec_driver_sql(
                """
                INSERT OR IGNORE INTO sources (name, series, note)
                SELECT DISTINCT SUBSTR(TRIM(CAST(source AS TEXT)), 1, 100),
                       '', '迁移自原 questions.source 字段'
                FROM questions
                WHERE TRIM(COALESCE(source, '')) != ''
                """
            )
            stats["created_sources"] = int(
                connection.exec_driver_sql("SELECT COUNT(*) FROM sources").scalar_one()
            )

            def col(name: str, fallback: str) -> str:
                # Older releases kept fewer optional columns; missing ones fall
                # back to the ORM default literal so the rebuild stays total.
                return f"q.{name}" if name in question_columns else fallback

            connection.exec_driver_sql("DROP TABLE IF EXISTS questions__new")
            connection.exec_driver_sql(
                """
                CREATE TABLE questions__new (
                    id INTEGER NOT NULL PRIMARY KEY,
                    content TEXT NOT NULL,
                    question_type VARCHAR(50) DEFAULT 'single_choice',
                    exam_track VARCHAR(100) DEFAULT '数学一',
                    subject VARCHAR(100) DEFAULT '',
                    topic VARCHAR(100) DEFAULT '',
                    difficulty VARCHAR(50) DEFAULT 'standard',
                    answer_markdown TEXT DEFAULT '',
                    review TEXT DEFAULT '',
                    association_group_id VARCHAR(100) DEFAULT '',
                    image_paths TEXT DEFAULT '[]',
                    tikz_code TEXT DEFAULT '',
                    figure_align VARCHAR(50) DEFAULT 'right',
                    tags TEXT DEFAULT '',
                    usage_count INTEGER DEFAULT 0,
                    source_id INTEGER,
                    source_number INTEGER,
                    source_scope VARCHAR(100) DEFAULT '',
                    created_at DATETIME,
                    CONSTRAINT fk_questions_source
                        FOREIGN KEY(source_id) REFERENCES sources(id)
                )
                """
            )
            connection.exec_driver_sql(
                """
                INSERT INTO questions__new (
                    id, content, question_type, exam_track, subject, topic,
                    difficulty, answer_markdown, review, association_group_id,
                    image_paths, tikz_code, figure_align, tags, usage_count,
                    source_id, source_number, source_scope, created_at
                )
                SELECT
                    q.id,
                    COALESCE(q.content, ''),
                    COALESCE(%(question_type)s, 'single_choice'),
                    COALESCE(%(exam_track)s, '数学一'),
                    COALESCE(%(subject)s, ''),
                    COALESCE(%(topic)s, ''),
                    COALESCE(%(difficulty)s, 'standard'),
                    COALESCE(%(answer_markdown)s, ''),
                    COALESCE(%(review)s, ''),
                    COALESCE(%(association_group_id)s, ''),
                    COALESCE(%(image_paths)s, '[]'),
                    COALESCE(%(tikz_code)s, ''),
                    COALESCE(%(figure_align)s, 'right'),
                    COALESCE(%(tags)s, ''),
                    COALESCE(%(usage_count)s, 0),
                    CASE WHEN TRIM(COALESCE(q.source, '')) = '' THEN NULL
                         ELSE (SELECT s.id FROM sources AS s
                               WHERE s.name = SUBSTR(TRIM(CAST(q.source AS TEXT)), 1, 100))
                    END,
                    NULL,
                    '',
                    %(created_at)s
                FROM questions AS q
                """ % {
                    "question_type": col("question_type", "NULL"),
                    "exam_track": col("exam_track", "NULL"),
                    "subject": col("subject", "NULL"),
                    "topic": col("topic", "NULL"),
                    "difficulty": col("difficulty", "NULL"),
                    "answer_markdown": col("answer_markdown", "NULL"),
                    "review": col("review", "NULL"),
                    "association_group_id": col("association_group_id", "NULL"),
                    "image_paths": col("image_paths", "NULL"),
                    "tikz_code": col("tikz_code", "NULL"),
                    "figure_align": col("figure_align", "NULL"),
                    "tags": col("tags", "NULL"),
                    "usage_count": col("usage_count", "NULL"),
                    "created_at": col("created_at", "NULL"),
                }
            )
            connection.exec_driver_sql("DROP TABLE questions")
            connection.exec_driver_sql(
                "ALTER TABLE questions__new RENAME TO questions"
            )
            for index_sql in (
                "CREATE INDEX idx_questions_exam_track ON questions (exam_track)",
                "CREATE INDEX idx_questions_subject ON questions (subject)",
                "CREATE INDEX idx_questions_topic ON questions (topic)",
                "CREATE INDEX idx_questions_question_type ON questions (question_type)",
                "CREATE INDEX idx_questions_difficulty ON questions (difficulty)",
                "CREATE INDEX idx_questions_association_group_id ON questions (association_group_id)",
                "CREATE INDEX idx_questions_tags ON questions (tags)",
                "CREATE INDEX idx_questions_usage_count ON questions (usage_count)",
                "CREATE INDEX idx_questions_source_id ON questions (source_id)",
            ):
                connection.exec_driver_sql(index_sql)

            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"迁移后仍存在外键异常: {violations[:5]}")

            connection.exec_driver_sql("PRAGMA user_version=4")
            connection.exec_driver_sql("COMMIT")
            transaction_started = False
        except Exception:
            if transaction_started:
                connection.exec_driver_sql("ROLLBACK")
            raise
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            enabled = int(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one())
            if enabled != 1:
                raise RuntimeError("迁移连接未能恢复 SQLite 外键检查")
    return stats


def migrate_database(
    engine: Engine,
    *,
    pre_migration_backup: Path | None = None,
) -> dict[str, object]:
    """Upgrade the connected SQLite database and fail loudly on errors."""

    current = schema_version(engine)
    if current > LATEST_SCHEMA_VERSION:
        raise RuntimeError(
            f"数据库版本 {current} 高于程序支持版本 {LATEST_SCHEMA_VERSION}，请升级程序。"
        )
    if current == LATEST_SCHEMA_VERSION:
        return {"from_version": current, "to_version": current, "backup": None}

    table_names = set(inspect(engine).get_table_names())
    if not table_names:
        # A caller may version an empty database immediately before creating the
        # current ORM metadata.
        with engine.begin() as connection:
            connection.exec_driver_sql(f"PRAGMA user_version={LATEST_SCHEMA_VERSION}")
        return {"from_version": current, "to_version": LATEST_SCHEMA_VERSION, "backup": None}
    if not REQUIRED_TABLES.issubset(table_names):
        missing = ", ".join(sorted(REQUIRED_TABLES - table_names))
        raise RuntimeError(f"数据库结构不完整，缺少必要数据表: {missing}")

    backup = pre_migration_backup or create_pre_migration_backup(
        engine, from_version=current, to_version=LATEST_SCHEMA_VERSION
    )
    stats: dict[str, object] = {}
    if current < 3:
        stats.update(_rebuild_relationship_tables(engine))
    stats.update(_migrate_v3_to_v4_sources(engine))
    return {
        "from_version": current,
        "to_version": LATEST_SCHEMA_VERSION,
        "backup": str(backup) if backup else None,
        **stats,
    }
