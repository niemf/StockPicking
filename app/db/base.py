"""Database engine, session factory and helpers.

The storage layer is deliberately thin: one engine per process, one session per
request/job. Business code never imports the engine directly; it takes a
``Session`` from :func:`get_db` (FastAPI) or :func:`session_scope` (scripts and
background jobs).

SQLite is the MVP database. The SQLAlchemy URL makes the switch to PostgreSQL a
configuration change rather than a rewrite.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from loguru import logger
from sqlalchemy import MetaData, create_engine, event, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Stable constraint names keep future Alembic migrations predictable.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _resolve_database_url(raw_url: str) -> tuple[str, dict]:
    """Return an absolute URL plus engine ``connect_args``.

    A relative SQLite path would otherwise depend on the current working
    directory, which breaks when the same code is started from a service
    manager instead of the project root.
    """
    url = make_url(raw_url)
    connect_args: dict = {}

    if url.get_backend_name() != "sqlite":
        return str(url), connect_args

    if not url.database or url.database == ":memory:":
        return str(url), connect_args

    database_path = Path(url.database)
    if not database_path.is_absolute():
        database_path = (PROJECT_ROOT / database_path).resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connect_args["check_same_thread"] = False
    return str(url.set(database=str(database_path))), connect_args


DATABASE_URL, _CONNECT_ARGS = _resolve_database_url(settings.database_url)

engine: Engine = create_engine(
    DATABASE_URL,
    connect_args=_CONNECT_ARGS,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


if engine.dialect.name == "sqlite":

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        """WAL + foreign keys: SQLite defaults are unsafe for this workload."""
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()


def init_db() -> None:
    """Create missing tables. Safe to call on every startup."""
    from app.db import models  # noqa: F401 - registers the mappers

    Base.metadata.create_all(engine)
    _ensure_columns()
    logger.info("database ready: {} ({})", engine.dialect.name, engine.url.database)


# Columns added after the first release. ``create_all`` never alters existing
# tables, so a fresh SQLite file would silently diverge from the models.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "watchlist": {
        "group_name": "VARCHAR(32) DEFAULT '默认'",
        "tags": "JSON",
    },
}


def _ensure_columns(target: Engine | None = None) -> None:
    from sqlalchemy import inspect

    target = target or engine
    inspector = inspect(target)
    tables = set(inspector.get_table_names())
    with target.begin() as connection:
        for table, columns in _ADDED_COLUMNS.items():
            if table not in tables:
                continue
            present = {column["name"] for column in inspector.get_columns(table)}
            for name, ddl in columns.items():
                if name in present:
                    continue
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                logger.info("schema migrated: {}.{}", table, name)


def check_database() -> dict:
    """Cheap liveness probe used by ``/api/health``."""
    info = {"dialect": engine.dialect.name, "path": engine.url.database}
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"ok": True, **info}
    except Exception as exc:  # noqa: BLE001 - health must never raise
        logger.error("database health check failed: {}", exc)
        return {"ok": False, **info, "error": str(exc)}


def get_db() -> Iterator[Session]:
    """FastAPI dependency: yields a session and always closes it."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for scripts and scheduled jobs."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
