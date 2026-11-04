"""Queries for users and photos. Plain SQL that runs on SQLite and D1 alike."""

from __future__ import annotations

import re

from .db import db

NOW = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"


# ----- users -----

def get_user(user_id: int) -> dict | None:
    rows = db().query("SELECT * FROM users WHERE id = ?", [user_id])
    return rows[0] if rows else None


def get_user_by_username(username: str) -> dict | None:
    rows = db().query("SELECT * FROM users WHERE username = ?", [username])
    return rows[0] if rows else None


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-")
    return slug[:32] or "maker"


def upsert_oidc_user(sub: str, preferred_username: str, name: str, email: str | None) -> dict:
    rows = db().query("SELECT * FROM users WHERE oidc_sub = ?", [sub])
    if rows:
        user = rows[0]
        if email and email != user["email"]:
            db().execute(f"UPDATE users SET email = ?, updated_at = {NOW} WHERE id = ?", [email, user["id"]])
        return user

    base = _slug(preferred_username or name or "maker")
    username, n = base, 1
    while get_user_by_username(username):
        n += 1
        username = f"{base}-{n}"
    res = db().execute(
        "INSERT INTO users (oidc_sub, username, display_name, email) VALUES (?, ?, ?, ?)",
        [sub, username, name or username, email],
    )
    return get_user(res.last_row_id)


def update_profile(user_id: int, display_name: str, bio: str) -> None:
    db().execute(
        f"UPDATE users SET display_name = ?, bio = ?, updated_at = {NOW} WHERE id = ?",
        [display_name, bio, user_id],
    )


# ----- photos -----

_PHOTO_COLS = """
    p.id, p.user_id, p.title, p.description, p.object_key, p.content_type,
    p.size_bytes, p.created_at, p.updated_at,
    u.username, u.display_name
"""


def get_photo(photo_id: int) -> dict | None:
    rows = db().query(
        f"SELECT {_PHOTO_COLS} FROM photos p JOIN users u ON u.id = p.user_id WHERE p.id = ?",
        [photo_id],
    )
    return rows[0] if rows else None


def photos_for_user(user_id: int) -> list[dict]:
    return db().query(
        f"SELECT {_PHOTO_COLS} FROM photos p JOIN users u ON u.id = p.user_id "
        "WHERE p.user_id = ? ORDER BY p.created_at DESC, p.id DESC",
        [user_id],
    )


def recent_photos(limit: int = 24) -> list[dict]:
    return db().query(
        f"SELECT {_PHOTO_COLS} FROM photos p JOIN users u ON u.id = p.user_id "
        "ORDER BY p.created_at DESC, p.id DESC LIMIT ?",
        [limit],
    )


def key_in_use(object_key: str) -> bool:
    return bool(db().query("SELECT 1 FROM photos WHERE object_key = ?", [object_key]))


def create_photo(user_id: int, title: str, description: str, object_key: str,
                 content_type: str, size_bytes: int) -> int:
    res = db().execute(
        "INSERT INTO photos (user_id, title, description, object_key, content_type, size_bytes) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [user_id, title, description, object_key, content_type, size_bytes],
    )
    return res.last_row_id


def update_photo(photo_id: int, title: str, description: str) -> None:
    db().execute(
        f"UPDATE photos SET title = ?, description = ?, updated_at = {NOW} WHERE id = ?",
        [title, description, photo_id],
    )


def delete_photo(photo_id: int) -> None:
    db().execute("DELETE FROM photos WHERE id = ?", [photo_id])
