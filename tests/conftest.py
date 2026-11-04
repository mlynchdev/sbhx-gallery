import io
from pathlib import Path

import pytest
from PIL import Image

from app import create_app

MIGRATIONS = Path(__file__).parents[1] / "migrations"


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
    for path in sorted(MIGRATIONS.glob("*.sql")):
        app.extensions["db"].executescript(path.read_text())
    return app


def image_bytes(fmt: str, width: int, height: int, **save_kwargs) -> bytes:
    img = Image.new("RGB", (width, height), (90, 100, 110))
    # A little structure so encoders produce real frames, not degenerate ones.
    for x in range(0, width, 7):
        img.putpixel((x, height // 2), (250, 250, 250))
    buf = io.BytesIO()
    img.save(buf, fmt, **save_kwargs)
    return buf.getvalue()
