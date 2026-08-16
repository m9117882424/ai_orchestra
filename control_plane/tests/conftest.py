import os

import pytest


os.environ["CONTROL_PLANE_DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["CONTROL_PLANE_ENVIRONMENT"] = "test"
os.environ["CONTROL_PLANE_SERVER_USERNAME"] = "manager"
os.environ["CONTROL_PLANE_SERVER_PASSWORD"] = "test-password"
os.environ["CONTROL_PLANE_DEFAULT_MONTHLY_BUDGET"] = "12000"

from control_plane.app.db import Base, SessionLocal, engine  # noqa: E402
from control_plane.app.services import seed_defaults  # noqa: E402


@pytest.fixture(autouse=True)
def reset_database():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as session:
        seed_defaults(session, 12000)
    yield


@pytest.fixture
def auth():
    return ("manager", "test-password")


@pytest.fixture
def mutation_headers():
    return {"X-Control-Request": "ai-orchestra"}
