import pytest

import app.database as database


@pytest.fixture(autouse=True)
def isolated_database(monkeypatch, tmp_path):
    test_database = tmp_path / "documents.db"
    monkeypatch.setattr(database, "DATABASE_URL", f"sqlite:///{test_database.as_posix()}")
