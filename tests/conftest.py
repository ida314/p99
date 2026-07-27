from __future__ import annotations

import pytest

from core import branding, paths


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Never touch the real data directory from a test."""
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path))
    monkeypatch.delenv(paths.ENV_DB, raising=False)
    yield tmp_path


@pytest.fixture
def conn(isolated_home):
    from core import catalog, db

    connection = db.open_db()
    catalog.seed(connection)
    yield connection
    connection.close()


@pytest.fixture
def env_editor():
    """The environment variable that overrides `$EDITOR` for capture."""
    return branding.env("EDITOR")
