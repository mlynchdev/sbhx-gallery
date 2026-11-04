from __future__ import annotations

import json
import re

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
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
def photo_src(photo: dict, kind: str = "detail") -> str:
    """URL of a photo's variant, or of the original for photos uploaded
    before variants existed."""
    key = photo.get(f"{kind}_key") or photo["object_key"]
    return storage().view_url(key)


@bp.app_template_global()
def photo_dims(photo: dict, kind: str) -> str:
    """width/height attributes when known, so the browser reserves space."""
    w, h = photo.get(f"{kind}_width"), photo.get(f"{kind}_height")
    return f'width="{w}" height="{h}"' if w and h else ""


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


def clean_photo_fields(data) -> tuple[str, str, str | None]:
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


# ----- upload page (API lives in uploads.py) -----

@bp.get("/upload")
@login_required
def upload():
    cfg = current_app.config
    client_config = {
        "variants": cfg["IMAGE_VARIANTS"],
        "preferredType": cfg["VARIANT_PREFERRED_TYPE"],
        "fallbackType": cfg["VARIANT_FALLBACK_TYPE"],
        "variantTypes": sorted(cfg["VARIANT_CONTENT_TYPES"]),
        "originalTypes": sorted(cfg["ALLOWED_CONTENT_TYPES"]),
        "maxOriginalBytes": cfg["MAX_UPLOAD_BYTES"],
        "maxVariantBytes": cfg["MAX_VARIANT_BYTES"],
        "reusableOriginalTypes": list(cfg["REUSABLE_ORIGINAL_TYPES"]),
    }
    return render_template(
        "photos/upload.html",
        client_config=json.dumps(client_config),
        max_bytes=cfg["MAX_UPLOAD_BYTES"],
    )


# ----- owner actions -----

@bp.route("/photos/<int:photo_id>/edit", methods=["GET", "POST"])
@login_required
def edit_photo(photo_id: int):
    photo = _owned_photo_or_404(photo_id)
    if request.method == "POST":
        title, description, error = clean_photo_fields(request.form)
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
    # A variant may point at the original, so de-duplicate.
    keys = dict.fromkeys([photo["object_key"], *models.variant_keys(photo_id)])
    models.delete_photo(photo_id)
    for key in keys:
        storage().delete(key)
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
