from collections.abc import Generator

from sqlalchemy import create_engine
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
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session
