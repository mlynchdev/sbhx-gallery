-- Applied to D1 with `wrangler d1 migrations apply photo-share --remote`
-- and to the local SQLite file with `flask --app app init-db`.

CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    oidc_sub     TEXT    NOT NULL UNIQUE,
    username     TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT    NOT NULL,
    email        TEXT,
    bio          TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS photos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title        TEXT    NOT NULL,
    description  TEXT    NOT NULL DEFAULT '',
    object_key   TEXT    NOT NULL UNIQUE,
    content_type TEXT    NOT NULL,
    size_bytes   INTEGER NOT NULL,
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_photos_user_created ON photos (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_photos_created ON photos (created_at DESC);
