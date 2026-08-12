from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
connect_args = {"check_same_thread": False, "timeout": 30} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)


if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    # 轻量迁移：为已有 invoices 表补充 title_warning 列（新库由 create_all 直接创建）
    with engine.begin() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(invoices)")}
        if "title_warning" not in columns:
            conn.exec_driver_sql("ALTER TABLE invoices ADD COLUMN title_warning TEXT NOT NULL DEFAULT ''")
        job_log_columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(job_logs)")}
        if "user_id" not in job_log_columns:
            conn.exec_driver_sql("ALTER TABLE job_logs ADD COLUMN user_id VARCHAR(36)")
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_job_logs_user_id ON job_logs (user_id)")
        # Backfill historical invoice/mailbox records so existing users keep
        # seeing their own history after member dashboards become isolated.
        conn.exec_driver_sql(
            """
            UPDATE job_logs
            SET user_id = json_extract(details, '$.user_id')
            WHERE user_id IS NULL
              AND json_valid(details)
              AND json_extract(details, '$.user_id') IS NOT NULL
            """
        )
        conn.exec_driver_sql(
            """
            UPDATE job_logs
            SET user_id = (
                SELECT invoices.owner_id FROM invoices
                WHERE invoices.id = json_extract(job_logs.details, '$.invoice_id')
            )
            WHERE user_id IS NULL
              AND json_valid(details)
              AND json_extract(details, '$.invoice_id') IS NOT NULL
            """
        )
        conn.exec_driver_sql(
            """
            UPDATE job_logs
            SET user_id = (
                SELECT mailboxes.created_by FROM mailboxes
                WHERE mailboxes.id = json_extract(job_logs.details, '$.mailbox_id')
            )
            WHERE user_id IS NULL
              AND json_valid(details)
              AND json_extract(details, '$.mailbox_id') IS NOT NULL
            """
        )
