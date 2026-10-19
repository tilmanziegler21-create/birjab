"""Engine factory. SQLite is the local prototype; PostgreSQL is available via Compose."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base


def make_engine(url: str, *, schema: str | None = None) -> Engine:
    if url.startswith("sqlite"):
        kwargs: dict[str, object] = {"connect_args": {"check_same_thread": False}}
        if url in {"sqlite://", "sqlite:///:memory:"}:
            kwargs["poolclass"] = StaticPool
        elif url.startswith("sqlite:///"):
            Path(url[len("sqlite:///") :]).expanduser().parent.mkdir(parents=True, exist_ok=True)
        return create_engine(url, **kwargs)
    if schema is not None:
        if not schema.isidentifier():
            raise ValueError("schema name")
        engine = create_engine(url, connect_args={"options": f"-csearch_path={schema},public"})
        engine.ats_schema = schema  # type: ignore[attr-defined]
        return engine
    return create_engine(url)


def arm_database_url(url: str, arm: str) -> str:
    """SQLite gets one file per arm. Other backends keep the URL and use a schema."""

    if not arm.isidentifier():
        raise ValueError("arm name")
    if url in {"sqlite://", "sqlite:///:memory:"}:
        return url
    if url.startswith("sqlite:///"):
        path = url[len("sqlite:///") :]
        if path.endswith(".db"):
            return f"sqlite:///{path[:-3]}.{arm}.db"
        return f"sqlite:///{path}.{arm}"
    return url


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def init_db(engine: Engine) -> None:
    schema = getattr(engine, "ats_schema", None)
    if schema is not None:
        with engine.begin() as connection:
            connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
    Base.metadata.create_all(engine)
    _ensure_sqlite_columns(engine)


def _ensure_sqlite_columns(engine: Engine) -> None:
    """Add columns that create_all will not attach to an already-created SQLite file."""

    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as connection:
        existing = {row[1] for row in connection.execute(text("PRAGMA table_info(balance_snapshots)"))}
        if existing and "profit_reserve" not in existing:
            connection.execute(
                text("ALTER TABLE balance_snapshots ADD COLUMN profit_reserve NUMERIC(38, 18) NOT NULL DEFAULT 0")
            )
