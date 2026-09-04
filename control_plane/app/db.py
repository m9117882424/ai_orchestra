from collections.abc import Generator
import os
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .settings import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
database_url = settings.sqlalchemy_url()
engine_kwargs: dict = {"pool_pre_ping": True}

if str(database_url).startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}
    if str(database_url).endswith(":memory:"):
        engine_kwargs["poolclass"] = StaticPool

engine = create_engine(database_url, **engine_kwargs)


def _expected_schema_revision() -> str:
    root = Path(__file__).resolve().parents[1]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    head = ScriptDirectory.from_config(cfg).get_current_head()
    if not head:
        raise RuntimeError("Alembic head revision не определена")
    return head


def _guard_production_schema() -> None:
    if settings.environment.lower() == "test":
        return
    if os.environ.get("CONTROL_PLANE_SCHEMA_MODE", "runtime").lower() == "migrate":
        return

    expected = _expected_schema_revision()
    with engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
    if current != expected:
        raise RuntimeError(
            "Control Plane отказался запускаться на неподготовленной схеме: "
            f"current={current or 'unversioned'}, expected={expected}. "
            "Выполните scripts/migrate-control-plane.sh."
        )

    ddl_prefixes = ("CREATE ", "ALTER ", "DROP ", "TRUNCATE ", "COMMENT ON ")

    @event.listens_for(engine, "before_cursor_execute")
    def _block_runtime_ddl(conn, cursor, statement, parameters, context, executemany):  # noqa: ARG001
        normalized = " ".join(str(statement).lstrip().upper().split())
        if normalized.startswith(ddl_prefixes):
            raise RuntimeError(
                "Runtime DDL запрещен: schema изменяется только управляемой Alembic migration"
            )


_guard_production_schema()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session
