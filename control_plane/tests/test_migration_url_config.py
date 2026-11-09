"""Exercise Alembic's offline and online URL configuration without a database."""
from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest
from alembic import context
from alembic.config import Config
import sqlalchemy
from sqlalchemy.engine import URL


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "check_matrix_0123456789abcdef"
URL_TEXT = (
    "postgresql+psycopg://check_matrix:synthetic%25password@127.0.0.1:5432/"
    f"check_matrix?options=-csearch_path%3D{SCHEMA}"
)


@pytest.mark.parametrize("as_url_object", [False, True])
@pytest.mark.parametrize("offline", [False, True])
def test_migration_url_preserves_percent_encoded_search_path(
    monkeypatch, as_url_object, offline
):
    supplied = sqlalchemy.engine.make_url(URL_TEXT) if as_url_object else URL_TEXT
    assert isinstance(supplied, URL) == as_url_object
    cfg = Config()
    configured = []
    monkeypatch.syspath_prepend(str(ROOT))
    monkeypatch.setattr("app.settings.get_settings", lambda: SimpleNamespace(
        sqlalchemy_url=lambda: supplied
    ))
    monkeypatch.setattr(context, "config", cfg, raising=False)
    monkeypatch.setattr(context, "is_offline_mode", lambda: offline, raising=False)
    monkeypatch.setattr(context, "configure", lambda **kw: configured.append(kw), raising=False)
    monkeypatch.setattr(context, "begin_transaction", nullcontext, raising=False)
    monkeypatch.setattr(context, "run_migrations", lambda: None, raising=False)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    class Engine:
        def connect(self):
            return Connection()

    captured = []

    def fake_engine_from_config(section, **kw):
        captured.append(section["sqlalchemy.url"])
        return Engine()

    monkeypatch.setattr(sqlalchemy, "engine_from_config", fake_engine_from_config)
    runpy.run_path(str(ROOT / "migrations" / "env.py"))

    # ConfigParser must decode the escaped percents exactly once. Both paths
    # must receive the original URL, including the disposable search_path.
    assert cfg.get_main_option("sqlalchemy.url") == URL_TEXT
    assert configured
    if offline:
        assert configured[0]["url"] == URL_TEXT
        assert not captured
    else:
        assert captured == [URL_TEXT]
        assert configured[0]["connection"] is not None
