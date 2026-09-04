from collections.abc import Generator
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from .database_base import Base
from .database_engine import create_configured_engine
from .settings import get_settings


settings = get_settings()
engine = create_configured_engine()


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
