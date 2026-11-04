import re
from pathlib import Path

import pytest

from app import create_app


@pytest.fixture
def app(tmp_path):
    app = create_app({
        "TESTING": True,
        "DEBUG": True,
        "WTF_CSRF_ENABLED": False,
        "DEV_LOGIN": True,
        "SECRET_KEY": "test",
        "SQLITE_PATH": str(tmp_path / "db.sqlite3"),
        "LOCAL_UPLOAD_DIR": str(tmp_path / "uploads"),
        "OIDC_DISCOVERY_URL": "",
        "SERVER_NAME": "localhost",
    })
    app.extensions["db"].executescript((Path(__file__).parents[1] / "migrations" / "0001_init.sql").read_text())
    return app


def login(client, username):
    return client.post("/auth/dev-login", data={"username": username})


def upload(client, data=b"\x89PNG fake", title="Bracket", content_type="image/png"):
    up = client.post("/api/uploads", json={"content_type": content_type, "size": len(data)}).get_json()
    put_path = up["upload_url"].replace("http://localhost", "")
    assert client.put(put_path, data=data, content_type=content_type).status_code == 200
    return up["key"], client.post("/api/photos", json={"key": up["key"], "title": title, "description": "6061"})


def test_upload_then_public_profile_shows_photo(app):
    client = app.test_client()
    login(client, "alice")
    key, res = upload(client)
    assert res.status_code == 201

    anon = app.test_client()
    page = anon.get("/u/alice").get_data(as_text=True)
    assert "Bracket" in page
    detail = anon.get(res.get_json()["url"]).get_data(as_text=True)
    assert "Download" in detail and "PNG" in detail
    img = re.search(r'src="(/_local-storage/[^"]+)"', detail).group(1)
    assert anon.get(img).data == b"\x89PNG fake"


def test_cannot_claim_another_users_key(app):
    alice, bob = app.test_client(), app.test_client()
    login(alice, "alice")
    login(bob, "bob")
    up = alice.post("/api/uploads", json={"content_type": "image/png", "size": 3}).get_json()
    res = bob.post("/api/photos", json={"key": up["key"], "title": "mine now"})
    assert res.status_code == 400


def test_save_requires_object_to_exist(app):
    client = app.test_client()
    login(client, "alice")
    up = client.post("/api/uploads", json={"content_type": "image/png", "size": 3}).get_json()
    assert client.post("/api/photos", json={"key": up["key"], "title": "x"}).status_code == 400


def test_rejects_bad_type_and_size(app):
    client = app.test_client()
    login(client, "alice")
    assert client.post("/api/uploads", json={"content_type": "text/html", "size": 3}).status_code == 400
    too_big = app.config["MAX_UPLOAD_BYTES"] + 1
    assert client.post("/api/uploads", json={"content_type": "image/png", "size": too_big}).status_code == 400


def test_put_rejects_tampered_content_type(app):
    client = app.test_client()
    login(client, "alice")
    up = client.post("/api/uploads", json={"content_type": "image/png", "size": 3}).get_json()
    put_path = up["upload_url"].replace("http://localhost", "")
    assert client.put(put_path, data=b"<h1>", content_type="text/html").status_code == 403


def test_only_owner_can_edit_or_delete(app):
    alice, bob = app.test_client(), app.test_client()
    login(alice, "alice")
    login(bob, "bob")
    _, res = upload(alice)
    pid = res.get_json()["id"]
    assert bob.post(f"/photos/{pid}/edit", data={"title": "hacked"}).status_code == 403
    assert bob.post(f"/photos/{pid}/delete").status_code == 403

    assert alice.post(f"/photos/{pid}/edit", data={"title": "Renamed"}).status_code == 302
    assert "Renamed" in alice.get(f"/photos/{pid}").get_data(as_text=True)
    assert alice.post(f"/photos/{pid}/delete").status_code == 302
    assert alice.get(f"/photos/{pid}").status_code == 404


def test_upload_requires_login(app):
    client = app.test_client()
    assert client.get("/upload").status_code == 302
    assert client.post("/api/uploads", json={}).status_code == 302


def test_dev_login_disabled_without_debug(app):
    app.debug = False
    assert app.test_client().get("/auth/dev-login").status_code == 404
