import datetime
import sqlite3
import json
from pathlib import Path
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from mathbank.paths import DATABASE_FILE, sqlite_url

# SQLite Database URL
SQLALCHEMY_DATABASE_URL = sqlite_url(DATABASE_FILE)

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


@event.listens_for(Engine, "connect")
def _configure_sqlite_connection(dbapi_connection, _connection_record):
    """Apply relational safety settings to every SQLite connection."""

    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def _utcnow_naive():
    """Return UTC without tzinfo for the existing SQLite DateTime columns."""

    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def configure_sqlite_wal(database_engine: Engine) -> str | None:
    """Enable and verify WAL mode for a persistent SQLite database."""

    database_name = database_engine.url.database
    if not database_name or database_name == ":memory:":
        return None
    with database_engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as connection:
        mode = str(
            connection.exec_driver_sql("PRAGMA journal_mode=WAL").scalar_one()
        ).lower()
        if mode != "wal":
            raise RuntimeError(f"SQLite WAL 模式启用失败，当前模式: {mode}")
        connection.exec_driver_sql("PRAGMA synchronous=NORMAL")
        connection.exec_driver_sql("PRAGMA wal_autocheckpoint=1000")
    try:
        Path(database_name).resolve().chmod(0o600)
    except OSError:
        pass
    return mode

def format_source_label(name: str, scope: str | None, number: int | None) -> str:
    """渲染结构化来源的显示标签，如 ``880基础篇·第二章·选择(10)``、``1991数一(8)``。"""

    label = (name or "").strip()
    scope_text = (scope or "").strip()
    if scope_text:
        label = f"{label}·{scope_text}" if label else scope_text
    if number is not None:
        label = f"{label}({number})" if label else str(number)
    return label


class Source(Base):
    """题目来源库：每册教辅分篇或每张真题卷一条记录，独立于题目生命周期。"""

    __tablename__ = "sources"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, unique=True, index=True)  # 显示名：880基础篇 / 1991数一
    series = Column(String(100), default="")  # 系列：李林880 / 考研数学真题（用于聚合）
    note = Column(Text, default="")
    created_at = Column(DateTime, default=_utcnow_naive)

    def to_dict(self, usage_count: int | None = None):
        data = {
            "id": self.id,
            "name": self.name,
            "series": self.series or "",
            "note": self.note or "",
            "created_at": (self.created_at.isoformat() + "Z") if self.created_at else None,
        }
        if usage_count is not None:
            data["usage_count"] = usage_count
        return data


class Question(Base):
    __tablename__ = "questions"

    id = Column(Integer, primary_key=True, index=True)
    content = Column(Text, nullable=False)  # 题干 (LaTeX + markdown)
    question_type = Column(String(50), default="single_choice", index=True)  # single_choice, fill_in_blank, detailed_answer
    exam_track = Column(String(100), default="数学一", index=True)  # 数学一/数学二/数学三
    subject = Column(String(100), default="", index=True)  # 高等数学/线性代数/概率论与数理统计
    topic = Column(String(100), default="", index=True)  # 具体考点
    difficulty = Column(String(50), default="standard", index=True)  # basic, standard, comprehensive, advanced
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=True, index=True)  # 结构化来源引用
    source_number = Column(Integer, nullable=True)  # 书内原始题号/卷内题号
    source_scope = Column(String(100), default="")  # 编号作用域，如「第二章·选择」
    answer_markdown = Column(Text, default="")  # 答案与解析 (LaTeX + markdown)
    review = Column(Text, default="")  # 评述 (允许空白)
    association_group_id = Column(String(100), default="", index=True)  # 关联题目分组ID (支持传递关系)
    _image_paths = Column(Text, default="[]", name="image_paths")  # 以JSON字符串形式存储相对路径列表
    tikz_code = Column(Text, default="")  # TikZ 几何绘图源代码
    figure_align = Column(String(50), default="right")  # 插图排版位置: right (题干右侧), center (下方居中), bottom_right (下方居右)
    tags = Column(Text, default="")  # 自定义标签 (逗号分隔或字符串)
    usage_count = Column(Integer, default=0, index=True)  # 组卷引用次数
    created_at = Column(DateTime, default=_utcnow_naive)

    source = relationship("Source", foreign_keys=[source_id])

    @property
    def image_paths(self):
        try:
            value = json.loads(self._image_paths)
            return value if isinstance(value, list) else []
        except Exception:
            return []

    @image_paths.setter
    def image_paths(self, value):
        if isinstance(value, list):
            self._image_paths = json.dumps(value)
        else:
            self._image_paths = "[]"

    def to_dict(self):
        return {
            "id": self.id,
            "content": self.content,
            "question_type": self.question_type,
            "exam_track": self.exam_track,
            "subject": self.subject,
            "topic": self.topic,
            "difficulty": self.difficulty,
            "source_id": self.source_id,
            "source_number": self.source_number,
            "source_scope": self.source_scope or "",
            "source_label": format_source_label(
                self.source.name if self.source is not None else "",
                self.source_scope,
                self.source_number,
            ),
            "answer_markdown": self.answer_markdown,
            "has_answer": bool((self.answer_markdown or "").strip()),
            "review": self.review,
            "association_group_id": self.association_group_id,
            "image_paths": self.image_paths,
            "tikz_code": self.tikz_code,
            "figure_align": self.figure_align or "right",
            "tags": self.tags,
            "usage_count": self.usage_count or 0,
            "created_at": (self.created_at.isoformat() + "Z") if self.created_at else None
        }

    def to_summary_dict(self):
        return {
            "id": self.id,
            "content": self.content,
            "question_type": self.question_type,
            "exam_track": self.exam_track,
            "subject": self.subject,
            "topic": self.topic,
            "difficulty": self.difficulty,
            "source_id": self.source_id,
            "source_number": self.source_number,
            "source_scope": self.source_scope or "",
            "source_label": format_source_label(
                self.source.name if self.source is not None else "",
                self.source_scope,
                self.source_number,
            ),
            "has_answer": bool((self.answer_markdown or "").strip()),
            "association_group_id": self.association_group_id,
            "image_paths": self.image_paths,
            "tikz_code": self.tikz_code,
            "figure_align": self.figure_align or "right",
            "tags": self.tags,
            "usage_count": self.usage_count or 0,
            "created_at": (self.created_at.isoformat() + "Z") if self.created_at else None
        }

class QuestionCurriculum(Base):
    __tablename__ = "question_curriculums"

    __table_args__ = (
        UniqueConstraint(
            "question_id",
            "version_code",
            name="uq_question_curriculum_version",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    question_id = Column(
        Integer,
        ForeignKey("questions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    version_code = Column(String(50), index=True, nullable=False)  # 'K'
    exam_track = Column(String(100), default="", index=True)
    subject = Column(String(100), default="", index=True)
    topic = Column(String(100), default="", index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "question_id": self.question_id,
            "version_code": self.version_code,
            "exam_track": self.exam_track,
            "subject": self.subject,
            "topic": self.topic
        }

class Paper(Base):
    __tablename__ = "papers"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(200), nullable=False)
    subtitle = Column(String(200), default="")
    paper_type = Column(String(50), default="kaoyan")  # kaoyan, quiz
    total_score = Column(Integer, default=150)
    metadata_json = Column(Text, default="{}")
    created_at = Column(DateTime, default=_utcnow_naive)

    def to_dict(self):
        meta = {}
        try:
            parsed_meta = json.loads(self.metadata_json or "{}")
            meta = parsed_meta if isinstance(parsed_meta, dict) else {}
        except Exception:
            meta = {}
        return {
            "id": self.id,
            "title": self.title,
            "subtitle": self.subtitle,
            "paper_type": self.paper_type,
            "total_score": self.total_score,
            "show_secret": meta.get("show_secret", True),
            "show_notice": meta.get("show_notice", True),
            "metadata_json": self.metadata_json,
            "created_at": (self.created_at.isoformat() + "Z") if self.created_at else None
        }

class PaperQuestion(Base):
    __tablename__ = "paper_questions"

    __table_args__ = (
        UniqueConstraint("paper_id", "order_index", name="uq_paper_question_order"),
        CheckConstraint("score >= 0", name="ck_paper_question_score_nonnegative"),
    )

    id = Column(Integer, primary_key=True, index=True)
    paper_id = Column(
        Integer,
        ForeignKey("papers.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    question_id = Column(
        Integer,
        ForeignKey("questions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    order_index = Column(Integer, default=0)
    score = Column(Integer, default=5)

    def to_dict(self):
        return {
            "id": self.id,
            "paper_id": self.paper_id,
            "question_id": self.question_id,
            "order_index": self.order_index,
            "score": self.score
        }

# Dependency to get db session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Create tables
def init_db():
    from mathbank.db_migrations import (
        LATEST_SCHEMA_VERSION,
        REQUIRED_TABLES,
        create_pre_migration_backup,
        migrate_database,
        schema_version,
    )

    # Refuse a future schema before create_all or any legacy ALTER can mutate it.
    current_version = schema_version(engine)
    if current_version > LATEST_SCHEMA_VERSION:
        raise RuntimeError(
            f"数据库版本 {current_version} 高于程序支持版本 "
            f"{LATEST_SCHEMA_VERSION}，请升级程序。"
        )
    with engine.connect() as connection:
        existing_tables = {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        core_tables = existing_tables & REQUIRED_TABLES
        if existing_tables:
            upgradeable_layouts = (
                {"questions"},
                {"questions", "question_curriculums"},
                REQUIRED_TABLES,
            )
            if current_version != 0:
                upgradeable_layouts = (REQUIRED_TABLES,)
            if core_tables not in upgradeable_layouts:
                missing = ", ".join(sorted(REQUIRED_TABLES - core_tables))
                raise RuntimeError(f"数据库结构不完整，缺少必要数据表: {missing}")

            # Old releases legitimately had only the question tables.  Accept
            # those known layouts, but reject a similarly named damaged table
            # before create_all can disguise the missing core columns.
            required_columns = {
                "questions": {
                    "id", "content", "question_type", "exam_track",
                    "subject", "topic", "difficulty",
                    "answer_markdown", "image_paths", "created_at",
                },
                "question_curriculums": {
                    "id", "question_id", "version_code", "exam_track",
                    "subject", "topic",
                },
                "papers": {
                    "id", "title", "subtitle", "paper_type", "total_score",
                    "metadata_json", "created_at",
                },
                "paper_questions": {
                    "id", "paper_id", "question_id", "order_index", "score",
                },
            }
            table_columns: dict[str, set[str]] = {}
            for table_name in core_tables:
                columns = {
                    row[1]
                    for row in connection.exec_driver_sql(
                        f'PRAGMA table_info("{table_name}")'
                    ).fetchall()
                }
                table_columns[table_name] = columns
                missing_columns = required_columns[table_name] - columns
                if missing_columns:
                    missing = ", ".join(sorted(missing_columns))
                    raise RuntimeError(
                        f"数据库表 {table_name} 缺少核心字段: {missing}"
                    )
            if "questions" in table_columns:
                question_columns = table_columns["questions"]
                has_legacy_source = "source" in question_columns
                has_structured_source = {
                    "source_id", "source_number", "source_scope"
                } <= question_columns
                if not has_legacy_source and not has_structured_source:
                    raise RuntimeError(
                        "数据库表 questions 缺少核心字段: source/source_id"
                    )
    pre_migration_backup = None
    if current_version < LATEST_SCHEMA_VERSION and existing_tables:
        pre_migration_backup = create_pre_migration_backup(
            engine,
            from_version=current_version,
            to_version=LATEST_SCHEMA_VERSION,
        )

    Base.metadata.create_all(bind=engine)
    # Create indexes manually and execute automatic migrations for SQLite databases to ensure maximum performance at scale
    try:
        from sqlalchemy import text
        with engine.begin() as conn:
            # Check column existence
            cursor = conn.execute(text("PRAGMA table_info(questions)"))
            columns = [row[1] for row in cursor.fetchall()]
            
            if "review" not in columns:
                conn.execute(text("ALTER TABLE questions ADD COLUMN review TEXT DEFAULT ''"))
                print("Added column 'review' to questions table successfully.")
                
            if "association_group_id" not in columns:
                conn.execute(text("ALTER TABLE questions ADD COLUMN association_group_id VARCHAR(100) DEFAULT ''"))
                print("Added column 'association_group_id' to questions table successfully.")
                
            if "tikz_code" not in columns:
                conn.execute(text("ALTER TABLE questions ADD COLUMN tikz_code TEXT DEFAULT ''"))
                print("Added column 'tikz_code' to questions table successfully.")

            if "figure_align" not in columns:
                conn.execute(text("ALTER TABLE questions ADD COLUMN figure_align VARCHAR(50) DEFAULT 'right'"))
                print("Added column 'figure_align' to questions table successfully.")
                
            if "tags" not in columns:
                conn.execute(text("ALTER TABLE questions ADD COLUMN tags TEXT DEFAULT ''"))
                print("Added column 'tags' to questions table successfully.")
                
            if "usage_count" not in columns:
                conn.execute(text("ALTER TABLE questions ADD COLUMN usage_count INTEGER DEFAULT 0"))
                print("Added column 'usage_count' to questions table successfully.")

            if "source_id" not in columns:
                conn.execute(text("ALTER TABLE questions ADD COLUMN source_id INTEGER REFERENCES sources(id)"))
                print("Added column 'source_id' to questions table successfully.")

            if "source_number" not in columns:
                conn.execute(text("ALTER TABLE questions ADD COLUMN source_number INTEGER"))
                print("Added column 'source_number' to questions table successfully.")

            if "source_scope" not in columns:
                conn.execute(text("ALTER TABLE questions ADD COLUMN source_scope VARCHAR(100) DEFAULT ''"))
                print("Added column 'source_scope' to questions table successfully.")

            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_questions_exam_track ON questions (exam_track)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_questions_subject ON questions (subject)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_questions_topic ON questions (topic)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_questions_question_type ON questions (question_type)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_questions_difficulty ON questions (difficulty)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_questions_association_group_id ON questions (association_group_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_questions_tags ON questions (tags)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_questions_usage_count ON questions (usage_count)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_questions_source_id ON questions (source_id)"))

            # Create indexes on question_curriculums
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_question_curriculums_lookup ON question_curriculums (version_code, exam_track, subject, topic)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_question_curriculums_qid ON question_curriculums (question_id)"))

            # Keep the active exam-track mirror in sync for a fresh database.
            cursor = conn.execute(text("SELECT COUNT(*) FROM question_curriculums"))
            count = cursor.fetchone()[0]
            if count == 0:
                conn.execute(text("""
                    INSERT INTO question_curriculums (question_id, version_code, exam_track, subject, topic)
                    SELECT id, 'K', exam_track, subject, topic
                    FROM questions
                """))
                print("Initialized K-version question classification mirrors.")
    except Exception as e:
        raise RuntimeError("数据库结构与考研数学模型不匹配，服务已停止启动") from e

    migration_result = migrate_database(
        engine, pre_migration_backup=pre_migration_backup
    )
    configure_sqlite_wal(engine)
    if migration_result.get("from_version") != migration_result.get("to_version"):
        print(f"[Database] Schema migration complete: {migration_result}")
