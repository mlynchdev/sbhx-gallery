"""Object storage: Cloudflare R2 in production, a local folder in development.

Both backends hand the browser a short-lived URL it can PUT the file to
directly, so image bytes never pass through Flask.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from flask import (
    Blueprint,
    Flask,
    abort,
    current_app,
    request,
    send_from_directory,
    url_for,
)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


@dataclass
class ObjectInfo:
    size: int
    content_type: str


@dataclass
class ListedObject:
    key: str
    last_modified: datetime


class R2Storage:
    def __init__(self, cfg):
        import boto3
        from botocore.config import Config as BotoConfig

        self.bucket = cfg["R2_BUCKET"]
        self.public_base = cfg["R2_PUBLIC_BASE_URL"]
        self.ttl = cfg["UPLOAD_URL_TTL_SECONDS"]
        self.s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{cfg['CF_ACCOUNT_ID']}.r2.cloudflarestorage.com",
            aws_access_key_id=cfg["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=cfg["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
            config=BotoConfig(signature_version="s3v4"),
        )

    def presign_put(self, key: str, content_type: str) -> str:
        return self.s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=self.ttl,
        )

    def head(self, key: str) -> ObjectInfo | None:
        from botocore.exceptions import ClientError

        try:
            r = self.s3.head_object(Bucket=self.bucket, Key=key)
        except ClientError:
            return None
        return ObjectInfo(r["ContentLength"], r.get("ContentType", ""))

    def delete(self, key: str) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=key)

    def read_prefix(self, key: str, length: int) -> bytes:
        r = self.s3.get_object(Bucket=self.bucket, Key=key, Range=f"bytes=0-{length - 1}")
        return r["Body"].read()

    def list_objects(self, prefix: str) -> Iterator[ListedObject]:
        pages = self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix)
        for page in pages:
            for obj in page.get("Contents", []):
                yield ListedObject(obj["Key"], obj["LastModified"])

    def view_url(self, key: str) -> str:
        if self.public_base:
            return f"{self.public_base}/{key}"
        return self.s3.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=3600
        )

    def download_url(self, key: str, filename: str) -> str:
        return self.s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ResponseContentDisposition": f'attachment; filename="{filename}"',
            },
            ExpiresIn=3600,
        )


class LocalStorage:
    """Mimics R2's presigned-PUT flow against a folder, for offline development."""

    def __init__(self, cfg, secret_key: str):
        self.root = Path(cfg["LOCAL_UPLOAD_DIR"]).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.ttl = cfg["UPLOAD_URL_TTL_SECONDS"]
        self.signer = URLSafeTimedSerializer(secret_key, salt="local-upload")

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            abort(400)
        return path

    def presign_put(self, key: str, content_type: str) -> str:
        token = self.signer.dumps({"k": key, "ct": content_type})
        return url_for("local_storage.put_object", key=key, token=token, _external=True)

    def head(self, key: str) -> ObjectInfo | None:
        path = self._path(key)
        if not path.exists():
            return None
        meta = json.loads(path.with_name(path.name + ".meta").read_text())
        return ObjectInfo(path.stat().st_size, meta["content_type"])

    def delete(self, key: str) -> None:
        path = self._path(key)
        path.unlink(missing_ok=True)
        path.with_name(path.name + ".meta").unlink(missing_ok=True)

    def read_prefix(self, key: str, length: int) -> bytes:
        with self._path(key).open("rb") as f:
            return f.read(length)

    def list_objects(self, prefix: str) -> Iterator[ListedObject]:
        for path in sorted(self.root.rglob("*")):
            key = path.relative_to(self.root).as_posix()
            if path.is_file() and not key.endswith(".meta") and key.startswith(prefix):
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                yield ListedObject(key, mtime)

    def view_url(self, key: str) -> str:
        return url_for("local_storage.get_object", key=key)

    def download_url(self, key: str, filename: str) -> str:
        return url_for("local_storage.get_object", key=key, download=filename)


local_bp = Blueprint("local_storage", __name__, url_prefix="/_local-storage")


@local_bp.put("/<path:key>")
def put_object(key: str):
    store: LocalStorage = current_app.extensions["storage"]
    try:
        claims = store.signer.loads(request.args.get("token", ""), max_age=store.ttl)
    except (BadSignature, SignatureExpired):
        abort(403)
    if claims["k"] != key or request.content_type != claims["ct"]:
        abort(403)
    path = store._path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(request.get_data())
    path.with_name(path.name + ".meta").write_text(json.dumps({"content_type": claims["ct"]}))
    return "", 200


@local_bp.get("/<path:key>")
def get_object(key: str):
    store: LocalStorage = current_app.extensions["storage"]
    info = store.head(key)
    if info is None:
        abort(404)
    filename = request.args.get("download")
    return send_from_directory(
        store.root,
        key,
        mimetype=info.content_type,
        as_attachment=bool(filename),
        download_name=filename or None,
    )


def init_app(app: Flask) -> None:
    if app.config["STORAGE_BACKEND"] == "r2":
        app.extensions["storage"] = R2Storage(app.config)
    else:
        app.extensions["storage"] = LocalStorage(app.config, app.config["SECRET_KEY"])
        app.register_blueprint(local_bp)


def storage() -> R2Storage | LocalStorage:
    return current_app.extensions["storage"]
