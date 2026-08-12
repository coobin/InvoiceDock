from __future__ import annotations

import re
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


def _sqlite_unique_indexes(cursor, table: str) -> dict[str, tuple[str, ...]]:  # type: ignore[no-untyped-def]
    result: dict[str, tuple[str, ...]] = {}
    for row in cursor.execute(f'PRAGMA index_list("{table}")').fetchall():
        if not row[2]:
            continue
        name = str(row[1])
        columns = tuple(
            str(item[2])
            for item in cursor.execute(f'PRAGMA index_info("{name}")').fetchall()
        )
        result[name] = columns
    return result


def _sqlite_replace_unique_constraint(
    cursor,  # type: ignore[no-untyped-def]
    *,
    table: str,
    old_columns: tuple[str, ...],
    old_constraint: str,
    new_constraint_sql: str,
) -> None:
    """Rebuild one SQLite table to replace a table-level UNIQUE constraint.

    SQLite cannot drop the automatic index created for a UNIQUE constraint.
    Rebuilding preserves every column, foreign key and explicit index from the
    existing database while changing only the unsafe uniqueness rule.
    """
    unique_indexes = _sqlite_unique_indexes(cursor, table)
    target_indexes = {
        name for name, columns in unique_indexes.items() if columns == old_columns
    }
    if not target_indexes:
        return

    table_row = cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    if not table_row or not table_row[0]:
        raise RuntimeError(f"无法读取 SQLite 表结构：{table}")
    original_sql = str(table_row[0])
    unique_column_pattern = r"\s*,\s*".join(map(re.escape, old_columns))
    explicit_indexes = [
        (str(row[0]), str(row[1]))
        for row in cursor.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL",
            (table,),
        ).fetchall()
        if str(row[0]) not in target_indexes
    ]

    named_pattern = re.compile(
        rf"CONSTRAINT\s+[\"`\[]?{re.escape(old_constraint)}[\"`\]]?\s+"
        rf"UNIQUE\s*\(\s*{unique_column_pattern}\s*\)",
        flags=re.IGNORECASE,
    )
    changed_sql, count = named_pattern.subn(new_constraint_sql, original_sql, count=1)
    if not count:
        column_pattern = re.compile(
            rf"UNIQUE\s*\(\s*{unique_column_pattern}\s*\)",
            flags=re.IGNORECASE,
        )
        changed_sql, count = column_pattern.subn(new_constraint_sql, original_sql, count=1)
    if not count:
        raise RuntimeError(f"无法迁移 SQLite 唯一约束：{table}.{old_columns}")

    temporary = f"{table}__tenant_migration"
    create_sql, count = re.subn(
        rf"^CREATE\s+TABLE\s+(?:[\"`\[]?{re.escape(table)}[\"`\]]?)",
        f'CREATE TABLE "{temporary}"',
        changed_sql,
        count=1,
        flags=re.IGNORECASE,
    )
    if not count:
        raise RuntimeError(f"无法生成 SQLite 临时表：{table}")

    columns = [str(row[1]) for row in cursor.execute(f'PRAGMA table_info("{table}")')]
    column_list = ", ".join(f'"{column}"' for column in columns)
    cursor.execute(f'DROP TABLE IF EXISTS "{temporary}"')
    cursor.execute(create_sql)
    cursor.execute(
        f'INSERT INTO "{temporary}" ({column_list}) '
        f'SELECT {column_list} FROM "{table}"'
    )
    cursor.execute(f'DROP TABLE "{table}"')
    cursor.execute(f'ALTER TABLE "{temporary}" RENAME TO "{table}"')
    for _name, sql in explicit_indexes:
        cursor.execute(sql)


def _migrate_sqlite_tenant_isolation() -> None:
    """Upgrade existing SQLite data without exposing one tenant to another."""
    raw = engine.raw_connection()
    cursor = raw.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute("BEGIN IMMEDIATE")

        _sqlite_replace_unique_constraint(
            cursor,
            table="invoices",
            old_columns=("sha256",),
            old_constraint="uq_invoice_sha256",
            new_constraint_sql=(
                "CONSTRAINT uq_invoice_owner_sha256 UNIQUE (owner_id, sha256)"
            ),
        )

        cache_columns = {
            str(row[1])
            for row in cursor.execute('PRAGMA table_info("verification_caches")')
        }
        additions = {
            "owner_id": "VARCHAR(36) REFERENCES users(id)",
            "invoice_code": "VARCHAR(40) NOT NULL DEFAULT ''",
            "invoice_date": "VARCHAR(20) NOT NULL DEFAULT ''",
            "total_amount": "VARCHAR(40) NOT NULL DEFAULT ''",
        }
        for name, definition in additions.items():
            if name not in cache_columns:
                cursor.execute(
                    f'ALTER TABLE "verification_caches" ADD COLUMN "{name}" {definition}'
                )
        # Preserve historical cache rows for audit purposes. Only rows with a
        # recoverable owner and a complete normalized fingerprint can be reused
        # by the stricter application lookup.
        cursor.execute(
            """
            UPDATE verification_caches
            SET owner_id = created_by
            WHERE owner_id IS NULL AND created_by IS NOT NULL
            """
        )
        cursor.execute(
            """
            UPDATE verification_caches
            SET invoice_code = upper(replace(coalesce(
                    json_extract(fields, '$.invoice_code'), ''), ' ', '')),
                invoice_number = upper(replace(coalesce(invoice_number, ''), ' ', '')),
                invoice_date = replace(replace(replace(coalesce(
                    json_extract(fields, '$.invoice_date'), ''), '-', ''), '/', ''), '.', ''),
                total_amount = CASE
                    WHEN json_extract(fields, '$.total_amount') IS NULL THEN ''
                    ELSE printf('%.2f', CAST(json_extract(fields, '$.total_amount') AS REAL))
                END
            """
        )
        _sqlite_replace_unique_constraint(
            cursor,
            table="verification_caches",
            old_columns=("invoice_number", "verify_date"),
            old_constraint="uq_verify_cache_number_date",
            new_constraint_sql=(
                "CONSTRAINT uq_verify_cache_owner_fingerprint_date UNIQUE "
                "(owner_id, invoice_code, invoice_number, invoice_date, total_amount, verify_date)"
            ),
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS ix_verification_caches_owner_id "
            "ON verification_caches (owner_id)"
        )

        # Remove unsafe relations written by an older release. Null-safe `IS`
        # treats two legacy unowned records as belonging to the same scope.
        cursor.execute(
            """
            UPDATE invoices
            SET duplicate_of = NULL
            WHERE duplicate_of IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM invoices AS original
                  WHERE original.id = invoices.duplicate_of
                    AND NOT (original.owner_id IS invoices.owner_id)
              )
            """
        )
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
        raw.close()


def init_db() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    if engine.url.get_backend_name() == "sqlite":
        _migrate_sqlite_tenant_isolation()
    # 轻量迁移：为已有 invoices 表补充 title_warning 列（新库由 create_all 直接创建）
    with engine.begin() as conn:
        user_columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)")}
        if "session_version" not in user_columns:
            conn.exec_driver_sql(
                "ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0"
            )
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
