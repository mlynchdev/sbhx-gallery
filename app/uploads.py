"""Direct-to-storage upload API.

1. POST /api/uploads      The browser has already made the thumb and detail
                          variants. It sends each file's role, type, size and
                          (for variants) pixel size. Flask picks the object
                          keys, presigns a PUT for each, and returns a signed
                          upload token listing exactly what it authorized.
2. PUT <presigned url>    The browser sends each Blob straight to R2.
3. POST /api/photos       The browser sends the token plus title and
                          description. Flask checks every object in storage
                          (exists, signed type, size limits, real format and
                          pixel size from the file header) and only then
                          writes the photo and variant rows.
   POST /api/uploads/abort  Deletes a failed upload's objects right away.

Anything left behind (closed tab, network drop) has no database row and is
removed by `flask cleanup-uploads`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import click
from flask import Blueprint, current_app, g, jsonify, request, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import imagesniff, models
from .auth import login_required
from .storage import storage
from .views import clean_photo_fields, filesize

bp = Blueprint("uploads", __name__, cli_group=None)

ORIGINAL = "original"
# With RETAIN_ORIGINALS off, this variant becomes the photo's main file.
PRIMARY_WITHOUT_ORIGINAL = "detail"


class UploadRejected(Exception):
    pass


def _signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="upload-session")


def _fit_within(width: int, height: int, max_edge: int) -> tuple[int, int]:
    """Same rule as ImagePrep.fitWithin in the browser."""
    scale = min(1.0, max_edge / max(width, height))
    return max(1, round(width * scale)), max(1, round(height * scale))


def _validate_request(files) -> dict:
    """Check the browser's declared files. Returns {role: spec}. These values
    are only used to presign; finalize re-checks the stored objects."""
    cfg = current_app.config
    variants = cfg["IMAGE_VARIANTS"]
    if not isinstance(files, list):
        raise UploadRejected("Missing file list.")
    by_role = {f.get("role"): f for f in files if isinstance(f, dict)}
    reusing = any(by_role.get(role, {}).get("reuse_original") for role in variants)
    # The original is uploaded when originals are kept, or when it doubles as
    # a variant (it already fits and re-encoding wouldn't shrink it).
    expected = set(variants) | ({ORIGINAL} if cfg["RETAIN_ORIGINALS"] or reusing else set())
    if set(by_role) != expected or len(files) != len(expected):
        raise UploadRejected(f"Expected exactly these files: {', '.join(sorted(expected))}.")

    original = by_role.get(ORIGINAL)
    if original is not None:
        if original.get("content_type") not in cfg["ALLOWED_CONTENT_TYPES"]:
            raise UploadRejected("Unsupported original format. Use JPEG, PNG, WebP, GIF, HEIC or AVIF.")
        if not _positive_int(original.get("size")) or original["size"] > cfg["MAX_UPLOAD_BYTES"]:
            raise UploadRejected(f"The original must be under {filesize(cfg['MAX_UPLOAD_BYTES'])}.")

    for role, settings in variants.items():
        spec = by_role[role]
        w, h = spec.get("width"), spec.get("height")
        if spec.get("reuse_original"):
            # The original already fits and re-encoding it would only make it
            # bigger, so this variant points at the original instead.
            if original["content_type"] not in cfg["REUSABLE_ORIGINAL_TYPES"]:
                raise UploadRejected(f"A {original['content_type']} original can't be shown as the {role} image.")
            if not (_positive_int(w) and _positive_int(h)) or max(w, h) > settings["max_edge"]:
                raise UploadRejected(f"The original is too large to use as the {role} image.")
            continue
        if spec.get("content_type") not in cfg["VARIANT_CONTENT_TYPES"]:
            raise UploadRejected(f"The {role} image must be WebP or JPEG.")
        if not _positive_int(spec.get("size")) or spec["size"] > cfg["MAX_VARIANT_BYTES"]:
            raise UploadRejected(f"The {role} image must be under {filesize(cfg['MAX_VARIANT_BYTES'])}.")
        if not (_positive_int(w) and _positive_int(h)) or max(w, h) > settings["max_edge"]:
            raise UploadRejected(f"The {role} image must be at most {settings['max_edge']} px on its longest edge.")
    return by_role


def _positive_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


@bp.post("/api/uploads")
@login_required
def create_upload():
    cfg = current_app.config
    data = request.get_json(silent=True) or {}
    try:
        by_role = _validate_request(data.get("files"))
    except UploadRejected as e:
        return jsonify(error=str(e)), 400

    upload_id = uuid.uuid4().hex
    signed, uploads = {}, []
    reused = []
    for role, spec in by_role.items():
        if role != ORIGINAL and spec.get("reuse_original"):
            reused.append(role)
            continue
        ct = spec["content_type"]
        types = cfg["ALLOWED_CONTENT_TYPES"] if role == ORIGINAL else cfg["VARIANT_CONTENT_TYPES"]
        key = f"users/{g.user['id']}/{upload_id}/{role}.{types[ct]}"
        signed[role] = {"k": key, "ct": ct}
        if role != ORIGINAL:
            signed[role].update(w=spec["width"], h=spec["height"])
        uploads.append({"role": role, "key": key, "content_type": ct, "url": storage().presign_put(key, ct)})
    for role in reused:
        spec = by_role[role]
        signed[role] = {**signed[ORIGINAL], "w": spec["width"], "h": spec["height"], "reuse": True}

    token = _signer().dumps({"u": g.user["id"], "files": signed})
    return jsonify(upload_token=token, uploads=uploads)


def _load_token(token: str, max_age: int | None):
    try:
        claims = _signer().loads(token or "", max_age=max_age)
    except SignatureExpired:
        raise UploadRejected("This upload session expired. Choose the photo again.") from None
    except BadSignature:
        raise UploadRejected("Invalid upload session.") from None
    if claims["u"] != g.user["id"]:
        return None
    return claims


def _verify_object(role: str, spec: dict):
    """Check one stored object. Returns (record, header)."""
    cfg = current_app.config
    store = storage()
    key, ct = spec["k"], spec["ct"]
    info = store.head(key)
    if info is None:
        raise UploadRejected(f"The {role} image never reached storage.")
    if info.content_type != ct:
        raise UploadRejected(f"The {role} image was stored with the wrong type.")
    limit = cfg["MAX_UPLOAD_BYTES"] if role == ORIGINAL else cfg["MAX_VARIANT_BYTES"]
    if info.size > limit:
        raise UploadRejected(f"The {role} image is larger than allowed.")

    header = imagesniff.sniff(store.read_prefix(key, imagesniff.HEADER_BYTES))
    if header and header.width is None and ct == "image/jpeg":
        # Phone JPEGs can carry enough EXIF/MPF data to push the frame
        # header past the first read.
        header = imagesniff.sniff(store.read_prefix(key, imagesniff.EXTENDED_HEADER_BYTES))
    if not imagesniff.matches(header, ct):
        raise UploadRejected(f"The {role} file isn't a valid {ct.split('/')[1].upper()} image.")

    record = {"kind": role, "object_key": key, "content_type": ct, "size_bytes": info.size}
    if role != ORIGINAL:
        max_edge = cfg["IMAGE_VARIANTS"][role]["max_edge"]
        if (header.width, header.height) != (spec["w"], spec["h"]) or max(header.width, header.height) > max_edge:
            raise UploadRejected(f"The {role} image's dimensions don't match what was prepared.")
        record.update(width=header.width, height=header.height)
    return record, header


def _reuse_original(role: str, spec: dict, original: dict, header) -> dict:
    """A variant that points at the already-verified original."""
    max_edge = current_app.config["IMAGE_VARIANTS"][role]["max_edge"]
    w, h = spec["w"], spec["h"]
    stored = (header.width, header.height)
    # A JPEG's frame header holds the size before EXIF rotation; orientations
    # 5-8 swap width and height, and browsers display the rotated size.
    ok = stored == (w, h) or (original["content_type"] == "image/jpeg" and stored == (h, w))
    if header.width is None or not ok or max(w, h) > max_edge:
        raise UploadRejected(f"The original can't be used as the {role} image: its size doesn't match.")
    return {**original, "kind": role, "width": w, "height": h}


def _check_variant_shapes(records: dict) -> None:
    """Both variants come from the same original, so the thumb must be the
    detail image scaled down (within a pixel of rounding)."""
    thumb, detail = records["thumb"], records["detail"]
    ew, eh = _fit_within(detail["width"], detail["height"], current_app.config["IMAGE_VARIANTS"]["thumb"]["max_edge"])
    if abs(thumb["width"] - ew) > 1 or abs(thumb["height"] - eh) > 1:
        raise UploadRejected("The thumbnail and detail images don't have the same shape.")


def _delete_unreferenced(files: dict) -> None:
    for key in {spec["k"] for spec in files.values()}:
        if not models.key_in_use(key):
            storage().delete(key)


@bp.post("/api/photos")
@login_required
def create_photo():
    data = request.get_json(silent=True) or {}
    title, description, error = clean_photo_fields(data)
    if error:
        return jsonify(error=error), 400
    try:
        claims = _load_token(data.get("upload_token"), current_app.config["UPLOAD_SESSION_TTL_SECONDS"])
    except UploadRejected as e:
        return jsonify(error=str(e)), 400
    if claims is None:
        return jsonify(error="This upload belongs to another account."), 403
    files = claims["files"]

    # A retry after a lost response: the photo already exists, return it.
    primary = files.get(ORIGINAL) or files[PRIMARY_WITHOUT_ORIGINAL]
    existing = models.get_photo_by_key(primary["k"])
    if existing:
        return jsonify(id=existing["id"], url=url_for("main.photo_detail", photo_id=existing["id"])), 200

    try:
        records = {}
        if ORIGINAL in files:
            original, header = _verify_object(ORIGINAL, files[ORIGINAL])
        for role, spec in files.items():
            if role == ORIGINAL:
                continue
            records[role] = (_reuse_original(role, spec, original, header) if spec.get("reuse")
                             else _verify_object(role, spec)[0])
        _check_variant_shapes(records)
        if ORIGINAL not in files:
            # No original kept: the detail image is the photo's main file
            # (and what Download serves).
            detail = records[PRIMARY_WITHOUT_ORIGINAL]
            original = {k: detail[k] for k in ("object_key", "content_type", "size_bytes")}
    except UploadRejected as e:
        _delete_unreferenced(files)
        return jsonify(error=f"{e} Nothing was saved; please try the upload again."), 400

    photo_id = models.create_photo(
        g.user["id"], title, description, original["object_key"], original["content_type"],
        original["size_bytes"], list(records.values()),
    )
    return jsonify(id=photo_id, url=url_for("main.photo_detail", photo_id=photo_id)), 201


@bp.post("/api/uploads/abort")
@login_required
def abort_upload():
    data = request.get_json(silent=True) or {}
    try:
        claims = _load_token(data.get("upload_token"), None)
    except UploadRejected as e:
        return jsonify(error=str(e)), 400
    if claims is None:
        return jsonify(error="This upload belongs to another account."), 403
    _delete_unreferenced(claims["files"])
    return "", 204


@bp.cli.command("cleanup-uploads")
@click.option("--max-age-hours", type=int, default=None, help="Default: ORPHAN_MAX_AGE_HOURS.")
@click.option("--dry-run", is_flag=True)
def cleanup_uploads(max_age_hours: int | None, dry_run: bool) -> None:
    """Delete stored objects that no photo references (abandoned uploads)."""
    hours = max_age_hours if max_age_hours is not None else current_app.config["ORPHAN_MAX_AGE_HOURS"]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    referenced = models.referenced_keys()
    removed = 0
    for obj in list(storage().list_objects("users/")):
        if obj.key in referenced or obj.last_modified > cutoff:
            continue
        click.echo(f"{'would delete' if dry_run else 'deleting'} {obj.key}")
        if not dry_run:
            storage().delete(obj.key)
        removed += 1
    click.echo(f"{removed} orphaned object(s) {'found' if dry_run else 'removed'}.")
