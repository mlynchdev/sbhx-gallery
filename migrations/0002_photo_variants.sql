-- Browser-generated derivatives of a photo. The original stays in photos.object_key.
-- Photos uploaded before this migration have no rows here and render from the original.

CREATE TABLE IF NOT EXISTS photo_variants (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_id     INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
    kind         TEXT    NOT NULL,              -- 'thumb' | 'detail'
    object_key   TEXT    NOT NULL UNIQUE,
    content_type TEXT    NOT NULL,
    size_bytes   INTEGER NOT NULL,
    width        INTEGER NOT NULL,
    height       INTEGER NOT NULL,
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (photo_id, kind)
);
