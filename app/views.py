from __future__ import annotations

import re
import uuid

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import models
from .auth import login_required
from .storage import storage

bp = Blueprint("main", __name__)

TITLE_MAX = 120
DESCRIPTION_MAX = 4000
DISPLAY_NAME_MAX = 80
BIO_MAX = 1000


@bp.app_template_global()
def photo_src(photo: dict) -> str:
    return storage().view_url(photo["object_key"])


@bp.app_template_global()
def photo_download(photo: dict) -> str:
    ext = photo["object_key"].rsplit(".", 1)[-1]
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", photo["title"]).strip("-")[:60] or "photo"
    return storage().download_url(photo["object_key"], f"{stem}.{ext}")


@bp.app_template_filter()
def filesize(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


@bp.app_template_filter()
def date(iso: str) -> str:
    # Stored as 2026-11-04T18:30:00Z
    return iso[:10] if iso else ""


def _clean_photo_fields(data) -> tuple[str, str, str | None]:
    title = (data.get("title") or "").strip()
    description = (data.get("description") or "").strip()
    if not title:
        return title, description, "Title is required."
    if len(title) > TITLE_MAX:
        return title, description, f"Title must be at most {TITLE_MAX} characters."
    if len(description) > DESCRIPTION_MAX:
        return title, description, f"Description must be at most {DESCRIPTION_MAX} characters."
    return title, description, None


def _owned_photo_or_404(photo_id: int) -> dict:
    photo = models.get_photo(photo_id)
    if photo is None:
        abort(404)
    if photo["user_id"] != g.user["id"]:
        abort(403)
    return photo


# ----- public pages -----

@bp.get("/")
def index():
    return render_template("index.html", photos=models.recent_photos())


@bp.get("/u/<username>")
def profile(username: str):
    user = models.get_user_by_username(username)
    if user is None:
        abort(404)
    return render_template("users/profile.html", user=user, photos=models.photos_for_user(user["id"]))


@bp.get("/photos/<int:photo_id>")
def photo_detail(photo_id: int):
    photo = models.get_photo(photo_id)
    if photo is None:
        abort(404)
    return render_template("photos/detail.html", photo=photo)


# ----- upload flow -----
# 1. Browser asks /api/uploads for a presigned PUT URL.
# 2. Browser PUTs the file straight to R2.
# 3. Browser posts the metadata to /api/photos; we confirm the object exists
#    in R2 (and its size/type) before writing the D1 row.

@bp.get("/upload")
@login_required
def upload():
    cfg = current_app.config
    return render_template(
        "photos/upload.html",
        allowed_types=sorted(cfg["ALLOWED_CONTENT_TYPES"]),
        max_bytes=cfg["MAX_UPLOAD_BYTES"],
    )


@bp.post("/api/uploads")
@login_required
def create_upload():
    cfg = current_app.config
    data = request.get_json(silent=True) or {}
    content_type = data.get("content_type", "")
    size = data.get("size")
    ext = cfg["ALLOWED_CONTENT_TYPES"].get(content_type)
    if ext is None:
        return jsonify(error="Unsupported file type. Use JPEG, PNG, WebP or GIF."), 400
    if not isinstance(size, int) or size <= 0 or size > cfg["MAX_UPLOAD_BYTES"]:
        return jsonify(error=f"File must be under {filesize(cfg['MAX_UPLOAD_BYTES'])}."), 400

    key = f"users/{g.user['id']}/{uuid.uuid4().hex}.{ext}"
    return jsonify(key=key, upload_url=storage().presign_put(key, content_type), content_type=content_type)


@bp.post("/api/photos")
@login_required
def create_photo():
    cfg = current_app.config
    data = request.get_json(silent=True) or {}
    key = data.get("key", "")
    title, description, error = _clean_photo_fields(data)
    if error:
        return jsonify(error=error), 400

    if not re.fullmatch(rf"users/{g.user['id']}/[0-9a-f]{{32}}\.[a-z]+", key):
        return jsonify(error="Invalid upload key."), 400
    if models.key_in_use(key):
        return jsonify(error="This upload was already saved."), 409

    info = storage().head(key)
    if info is None:
        return jsonify(error="Upload not found. Try again."), 400
    if info.size > cfg["MAX_UPLOAD_BYTES"] or info.content_type not in cfg["ALLOWED_CONTENT_TYPES"]:
        storage().delete(key)
        return jsonify(error="Uploaded file was rejected (too large or wrong type)."), 400

    photo_id = models.create_photo(g.user["id"], title, description, key, info.content_type, info.size)
    return jsonify(id=photo_id, url=url_for("main.photo_detail", photo_id=photo_id)), 201


# ----- owner actions -----

@bp.route("/photos/<int:photo_id>/edit", methods=["GET", "POST"])
@login_required
def edit_photo(photo_id: int):
    photo = _owned_photo_or_404(photo_id)
    if request.method == "POST":
        title, description, error = _clean_photo_fields(request.form)
        if error:
            flash(error, "error")
            photo = {**photo, "title": title, "description": description}
        else:
            models.update_photo(photo_id, title, description)
            flash("Photo updated.", "success")
            return redirect(url_for("main.photo_detail", photo_id=photo_id))
    return render_template("photos/edit.html", photo=photo)


@bp.post("/photos/<int:photo_id>/delete")
@login_required
def delete_photo(photo_id: int):
    photo = _owned_photo_or_404(photo_id)
    models.delete_photo(photo_id)
    storage().delete(photo["object_key"])
    flash("Photo deleted.", "success")
    return redirect(url_for("main.profile", username=g.user["username"]))


@bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        display_name = request.form.get("display_name", "").strip()
        bio = request.form.get("bio", "").strip()
        if not display_name or len(display_name) > DISPLAY_NAME_MAX:
            flash(f"Display name is required (max {DISPLAY_NAME_MAX} characters).", "error")
        elif len(bio) > BIO_MAX:
            flash(f"Bio must be at most {BIO_MAX} characters.", "error")
        else:
            models.update_profile(g.user["id"], display_name, bio)
            flash("Profile saved.", "success")
            return redirect(url_for("main.profile", username=g.user["username"]))
    return render_template("users/settings.html")
