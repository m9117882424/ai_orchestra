from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from .settings import get_settings


def create_configured_engine():
    """Create a DB engine from Control Plane settings without policy side effects."""
    database_url = get_settings().sqlalchemy_url()
    engine_kwargs: dict = {"pool_pre_ping": True}

    if str(database_url).startswith("sqlite"):
        engine_kwargs["connect_args"] = {"check_same_thread": False}
        if str(database_url).endswith(":memory:"):
            engine_kwargs["poolclass"] = StaticPool

    return create_engine(database_url, **engine_kwargs)
