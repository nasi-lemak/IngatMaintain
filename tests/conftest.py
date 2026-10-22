import re

import pytest

from ingatmaintain import create_app
from ingatmaintain.db import connect, create_user


@pytest.fixture
def app(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test",
            "DATABASE": str(tmp_path / "test.sqlite3"),
            "BASE_URL": "http://test",
            "SMTP_HOST": "",
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_CHAT_ID": "",
            "WEBHOOK_URL": "",
        }
    )
    yield app


@pytest.fixture
def conn(app):
    c = connect(app.config["DATABASE"])
    yield c
    c.close()


class Client:
    """Test client that carries the CSRF token like a browser would."""

    def __init__(self, flask_client):
        self.c = flask_client
        self.token = None

    def get(self, url, **kw):
        resp = self.c.get(url, **kw)
        m = re.search(rb'name="csrf-token" content="([^"]+)"', resp.data)
        if m:
            self.token = m.group(1).decode()
        return resp

    def post(self, url, data=None, **kw):
        if self.token is None:
            self.get("/login")
        data = dict(data or {})
        data.setdefault("csrf_token", self.token)
        return self.c.post(url, data=data, **kw)


@pytest.fixture
def client(app):
    return Client(app.test_client())


@pytest.fixture
def admin(app, conn, client):
    create_user(conn, "admin", "password123", "Ada Admin", "ada@example.com", is_admin=True)
    client.get("/login")
    resp = client.post("/login", {"username": "admin", "password": "password123"})
    assert resp.status_code == 302
    client.get("/")
    return client
