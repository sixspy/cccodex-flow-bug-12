import pytest

from app.db import init_db
from app.main import app

app.config.TOUCHUP = False


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    init_db(str(db))
    return app.test_client
