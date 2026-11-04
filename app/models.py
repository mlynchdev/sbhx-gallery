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
# Each photo row is the original upload. Its thumb and detail variants are
# joined in when present; older photos have none and fall back to the original.

_PHOTO_COLS = """
    p.id, p.user_id, p.title, p.description, p.object_key, p.content_type,
    p.size_bytes, p.created_at, p.updated_at,
    u.username, u.display_name,
    t.object_key AS thumb_key, t.width AS thumb_width, t.height AS thumb_height,
    d.object_key AS detail_key, d.width AS detail_width, d.height AS detail_height,
    d.content_type AS detail_content_type, d.size_bytes AS detail_size_bytes
"""

_PHOTO_FROM = """
    FROM photos p
    JOIN users u ON u.id = p.user_id
    LEFT JOIN photo_variants t ON t.photo_id = p.id AND t.kind = 'thumb'
    LEFT JOIN photo_variants d ON d.photo_id = p.id AND d.kind = 'detail'
"""


def get_photo(photo_id: int) -> dict | None:
    rows = db().query(f"SELECT {_PHOTO_COLS} {_PHOTO_FROM} WHERE p.id = ?", [photo_id])
    return rows[0] if rows else None


def get_photo_by_key(object_key: str) -> dict | None:
    rows = db().query(f"SELECT {_PHOTO_COLS} {_PHOTO_FROM} WHERE p.object_key = ?", [object_key])
    return rows[0] if rows else None


def photos_for_user(user_id: int) -> list[dict]:
    return db().query(
        f"SELECT {_PHOTO_COLS} {_PHOTO_FROM} WHERE p.user_id = ? ORDER BY p.created_at DESC, p.id DESC",
        [user_id],
    )


def recent_photos(limit: int = 24) -> list[dict]:
    return db().query(
        f"SELECT {_PHOTO_COLS} {_PHOTO_FROM} ORDER BY p.created_at DESC, p.id DESC LIMIT ?",
        [limit],
    )


def variant_keys(photo_id: int) -> list[str]:
    rows = db().query("SELECT object_key FROM photo_variants WHERE photo_id = ?", [photo_id])
    return [r["object_key"] for r in rows]


def key_in_use(object_key: str) -> bool:
    return bool(db().query(
        "SELECT 1 FROM photos WHERE object_key = ? UNION ALL "
        "SELECT 1 FROM photo_variants WHERE object_key = ? LIMIT 1",
        [object_key, object_key],
    ))


def referenced_keys() -> set[str]:
    rows = db().query("SELECT object_key FROM photos UNION SELECT object_key FROM photo_variants")
    return {r["object_key"] for r in rows}


def create_photo(user_id: int, title: str, description: str, object_key: str,
                 content_type: str, size_bytes: int, variants: list[dict] = ()) -> int:
    """Insert a photo and its variants. D1's HTTP API has no multi-call
    transactions, so if the variant insert fails the photo row is removed."""
    res = db().execute(
        "INSERT INTO photos (user_id, title, description, object_key, content_type, size_bytes) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [user_id, title, description, object_key, content_type, size_bytes],
    )
    photo_id = res.last_row_id
    if variants:
        values = ", ".join(["(?, ?, ?, ?, ?, ?, ?)"] * len(variants))
        params = []
        for v in variants:
            params += [photo_id, v["kind"], v["object_key"], v["content_type"],
                       v["size_bytes"], v["width"], v["height"]]
        try:
            db().execute(
                "INSERT INTO photo_variants "
                "(photo_id, kind, object_key, content_type, size_bytes, width, height) "
                f"VALUES {values}",
                params,
            )
        except Exception:
            delete_photo(photo_id)
            raise
    return photo_id


def update_photo(photo_id: int, title: str, description: str) -> None:
    db().execute(
        f"UPDATE photos SET title = ?, description = ?, updated_at = {NOW} WHERE id = ?",
        [title, description, photo_id],
    )


def delete_photo(photo_id: int) -> None:
    db().execute("DELETE FROM photo_variants WHERE photo_id = ?", [photo_id])
    db().execute("DELETE FROM photos WHERE id = ?", [photo_id])
