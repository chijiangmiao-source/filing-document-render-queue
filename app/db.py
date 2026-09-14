from __future__ import annotations

import os

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str | None = None) -> Engine:
    settings = get_settings()
    url = database_url or settings.database_url
    connect_args: dict = {}
    if url.startswith("sqlite"):
        connect_args["timeout"] = 30  # busy_timeout seconds
    engine = create_engine(url, connect_args=connect_args, future=True)

    if url.startswith("sqlite") and url not in ("sqlite://", "sqlite:///:memory:"):
        path = settings.db_path
        if path and not os.path.exists(path):
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

    return engine


engine = make_engine()
# expire_on_commit keeps post-commit reads from returning stale identity-map
# objects (important after the conditional lease/publish UPDATEs).
SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)


def _add_pdf_fingerprint_columns(target_engine=None) -> None:
    """Incremental, backward-compatible migration for pre-fingerprint databases.

    New tables already carry ``pdf_size``/``pdf_sha256`` from ``create_all``;
    this only ALTERs databases created by an older build. The columns are
    nullable and default to NULL, so historical succeeded rows keep the
    original fingerprint-free behavior.
    """
    from sqlalchemy import inspect, text

    target_engine = target_engine or engine
    inspector = inspect(target_engine)
    if "jobs" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("jobs")}
    additions = [
        ("pdf_size", "INTEGER"),
        ("pdf_sha256", "VARCHAR(64)"),
    ]
    missing = [(name, ddl) for name, ddl in additions if name not in existing]
    if not missing:
        return
    with target_engine.begin() as conn:
        for name, ddl in missing:
            conn.execute(text(f"ALTER TABLE jobs ADD COLUMN {name} {ddl}"))


def init_db() -> None:
    # Import models so they are registered on the metadata before create_all.
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _add_pdf_fingerprint_columns(engine)


def get_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
