import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event

from mathbank import database as database_module
from mathbank import db_migrations


def _create_current_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE questions (
                id INTEGER PRIMARY KEY,
                content TEXT NOT NULL,
                question_type VARCHAR(50) DEFAULT 'single_choice',
                exam_track VARCHAR(100) DEFAULT '数学一',
                subject VARCHAR(100) DEFAULT '高等数学',
                topic VARCHAR(100) DEFAULT '函数、极限与连续',
                difficulty VARCHAR(50) DEFAULT 'standard',
                source VARCHAR(200) DEFAULT '',
                answer_markdown TEXT DEFAULT '',
                image_paths TEXT DEFAULT '[]',
                created_at DATETIME
            );
            CREATE TABLE papers (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                subtitle TEXT DEFAULT '',
                paper_type TEXT DEFAULT 'kaoyan',
                total_score INTEGER DEFAULT 150,
                metadata_json TEXT DEFAULT '{}',
                created_at DATETIME
            );
            CREATE TABLE question_curriculums (
                id INTEGER PRIMARY KEY,
                question_id INTEGER NOT NULL,
                version_code VARCHAR(50) NOT NULL,
                exam_track VARCHAR(100) DEFAULT '',
                subject VARCHAR(100) DEFAULT '',
                topic VARCHAR(100) DEFAULT ''
            );
            CREATE TABLE paper_questions (
                id INTEGER PRIMARY KEY,
                paper_id INTEGER NOT NULL,
                question_id INTEGER NOT NULL,
                order_index INTEGER DEFAULT 0,
                score INTEGER DEFAULT 5
            );
            INSERT INTO questions VALUES
                (1, 'question', 'single_choice', '数学一', '高等数学', '函数、极限与连续', 'standard', '', '', '[]', NULL);
            INSERT INTO papers VALUES (1, 'paper', '', 'kaoyan', 999, '{}', NULL);
            INSERT INTO question_curriculums VALUES
                (1, 1, 'K', '数学一', '高等数学', '旧考点'),
                (2, 1, 'K', '数学一', '高等数学', '新考点'),
                (3, 999, 'K', '数学一', '高等数学', '孤立记录');
            INSERT INTO paper_questions VALUES
                (1, 1, 1, 0, 5),
                (2, 1, 1, 0, 10),
                (3, 1, 999, 1, 5);
            """
        )


def test_migration_moves_legacy_source_strings_into_sources_table(tmp_path, monkeypatch):
    database_path = tmp_path / "legacy-source.db"
    backup_dir = tmp_path / "backups"
    _create_current_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("UPDATE questions SET source = '李林880基础篇' WHERE id = 1")
        connection.execute("PRAGMA user_version=3")
    monkeypatch.setattr(db_migrations, "SCHEMA_SNAPSHOT_DIR", backup_dir / "schema_snapshots")

    result = db_migrations.migrate_database(create_engine(f"sqlite:///{database_path}"))

    assert result["from_version"] == 3
    assert result["to_version"] == db_migrations.LATEST_SCHEMA_VERSION
    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(questions)").fetchall()}
        assert "source" not in columns
        assert {"source_id", "source_number", "source_scope"} <= columns
        rows = connection.execute("SELECT name, series FROM sources").fetchall()
        assert rows == [("李林880基础篇", "")]
        source_id = connection.execute("SELECT id FROM sources").fetchone()[0]
        assert connection.execute(
            "SELECT source_id, source_number, source_scope FROM questions WHERE id = 1"
        ).fetchone() == (source_id, None, "")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='questions'"
            ).fetchall()
        }
        assert "idx_questions_source_id" in indexes


def test_migration_repairs_current_relationships_and_adds_constraints(tmp_path, monkeypatch):
    database_path = tmp_path / "current.db"
    backup_dir = tmp_path / "backups"
    _create_current_database(database_path)
    monkeypatch.setattr(db_migrations, "SCHEMA_SNAPSHOT_DIR", backup_dir / "schema_snapshots")
    engine = create_engine(f"sqlite:///{database_path}")

    result = db_migrations.migrate_database(engine)

    assert result["from_version"] == 0
    assert result["to_version"] == db_migrations.LATEST_SCHEMA_VERSION
    assert result["removed_question_curriculums"] == 2
    assert result["removed_paper_questions"] == 2
    assert result["backup"]

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("PRAGMA user_version").fetchone()[0] == db_migrations.LATEST_SCHEMA_VERSION
        assert connection.execute(
            "SELECT id, topic FROM question_curriculums"
        ).fetchall() == [(2, "新考点")]
        assert connection.execute(
            "SELECT id, score FROM paper_questions"
        ).fetchall() == [(2, 10)]
        assert connection.execute("SELECT total_score FROM papers WHERE id = 1").fetchone()[0] == 10
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO question_curriculums (question_id, version_code) VALUES (1, 'K')"
            )
        connection.execute("DELETE FROM questions WHERE id = 1")
        assert connection.execute("SELECT COUNT(*) FROM question_curriculums").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM paper_questions").fetchone()[0] == 0


def test_migration_refuses_unknown_future_schema(tmp_path):
    database_path = tmp_path / "future.db"
    _create_current_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(f"PRAGMA user_version={db_migrations.LATEST_SCHEMA_VERSION + 1}")
    with pytest.raises(RuntimeError, match="高于程序支持版本"):
        db_migrations.migrate_database(create_engine(f"sqlite:///{database_path}"))


def test_migration_deduplicates_null_and_zero_paper_order(tmp_path, monkeypatch):
    database_path = tmp_path / "null-order.db"
    _create_current_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO paper_questions (id, paper_id, question_id, order_index, score) VALUES (4, 1, 1, NULL, 20)"
        )
    monkeypatch.setattr(db_migrations, "SCHEMA_SNAPSHOT_DIR", tmp_path / "schema_snapshots")
    result = db_migrations.migrate_database(create_engine(f"sqlite:///{database_path}"))
    assert result["removed_paper_questions"] == 3
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT id, order_index, score FROM paper_questions").fetchall() == [(4, 0, 20)]
        assert connection.execute("SELECT total_score FROM papers WHERE id = 1").fetchone()[0] == 20


def test_migration_ddl_failure_rolls_back_current_schema(tmp_path, monkeypatch):
    database_path = tmp_path / "crash-safe.db"
    _create_current_database(database_path)
    monkeypatch.setattr(db_migrations, "SCHEMA_SNAPSHOT_DIR", tmp_path / "schema_snapshots")
    engine = create_engine(f"sqlite:///{database_path}")

    def inject_failure(_conn, _cursor, statement, _parameters, _context, _many):
        if "ALTER TABLE question_curriculums__new RENAME" in statement:
            raise RuntimeError("injected migration failure")

    event.listen(engine, "before_cursor_execute", inject_failure)
    with pytest.raises(RuntimeError, match="injected migration failure"):
        db_migrations.migrate_database(engine)
    event.remove(engine, "before_cursor_execute", inject_failure)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute("SELECT id, topic FROM question_curriculums ORDER BY id").fetchall() == [
            (1, "旧考点"), (2, "新考点"), (3, "孤立记录")
        ]
        assert connection.execute("SELECT id, score FROM paper_questions ORDER BY id").fetchall() == [
            (1, 5), (2, 10), (3, 5)
        ]


def test_migration_refuses_partially_missing_schema(tmp_path):
    database_path = tmp_path / "partial.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE questions (id INTEGER PRIMARY KEY)")
    with pytest.raises(RuntimeError, match="缺少必要数据表"):
        db_migrations.migrate_database(create_engine(f"sqlite:///{database_path}"))


def test_persistence_modules_remain_importable_on_python_310():
    project_root = Path(__file__).resolve().parents[1]
    module_paths = [
        project_root / "mathbank" / "backup.py",
        project_root / "mathbank" / "db_migrations.py",
        project_root / "mathbank" / "database.py",
        project_root / "mathbank" / "task_manager.py",
        project_root / "mathbank" / "health.py",
        project_root / "scripts" / "backup.py",
        project_root / "scripts" / "restore.py",
    ]
    for module_path in module_paths:
        source = module_path.read_text(encoding="utf-8")
        assert "datetime.UTC" not in source
        assert "dt.UTC" not in source


def test_init_db_refuses_future_schema_before_mutating_it(tmp_path, monkeypatch):
    database_path = tmp_path / "future-init.db"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            f"""
            PRAGMA user_version={db_migrations.LATEST_SCHEMA_VERSION + 1};
            CREATE TABLE future_only (id INTEGER PRIMARY KEY, payload TEXT);
            INSERT INTO future_only (payload) VALUES ('preserve-me');
            """
        )
    before = database_path.read_bytes()
    future_engine = create_engine(f"sqlite:///{database_path}")
    monkeypatch.setattr(database_module, "engine", future_engine)
    with pytest.raises(RuntimeError, match="高于程序支持版本"):
        database_module.init_db()
    assert database_path.read_bytes() == before


def test_init_db_accepts_only_the_current_graduate_schema(tmp_path, monkeypatch):
    database_path = tmp_path / "current-init.db"
    _create_current_database(database_path)
    engine = create_engine(f"sqlite:///{database_path}")
    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(db_migrations, "SCHEMA_SNAPSHOT_DIR", tmp_path / "schema_snapshots")

    database_module.init_db()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == db_migrations.LATEST_SCHEMA_VERSION
        assert connection.execute("SELECT version_code FROM question_curriculums").fetchall() == [("K",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_init_db_rejects_damaged_questions_table_before_creating_missing_tables(tmp_path, monkeypatch):
    database_path = tmp_path / "damaged.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE questions (id INTEGER PRIMARY KEY)")
    damaged_engine = create_engine(f"sqlite:///{database_path}")
    monkeypatch.setattr(database_module, "engine", damaged_engine)
    monkeypatch.setattr(db_migrations, "SCHEMA_SNAPSHOT_DIR", tmp_path / "schema_snapshots")
    with pytest.raises(RuntimeError, match="缺少核心字段"):
        database_module.init_db()
    with sqlite3.connect(database_path) as connection:
        assert {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        } == {"questions"}
