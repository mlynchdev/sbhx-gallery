import re
from datetime import datetime, timedelta, timezone

import pytest
from conftest import image_bytes

from app import models
from app.storage import storage


def login(client, username):
    return client.post("/auth/dev-login", data={"username": username})


def local_path(url: str) -> str:
    return url.replace("http://localhost", "")


def standard_files(thumb_type="image/webp", detail_type="image/webp"):
    """A 3000x2000 JPEG original with correctly sized variants."""
    fmt = {"image/webp": "WEBP", "image/jpeg": "JPEG"}
    return {
        "original": (image_bytes("JPEG", 3000, 2000), "image/jpeg", None),
        "thumb": (image_bytes(fmt[thumb_type], 640, 427), thumb_type, (640, 427)),
        "detail": (image_bytes(fmt[detail_type], 2048, 1365), detail_type, (2048, 1365)),
    }


def presign(client, files):
    specs = []
    for role, (data, ct, dims) in files.items():
        spec = {"role": role, "content_type": ct, "size": len(data)}
        if dims:
            spec["width"], spec["height"] = dims
        specs.append(spec)
    return client.post("/api/uploads", json={"files": specs})


def put_all(client, session, files, skip=()):
    for up in session["uploads"]:
        if up["role"] in skip:
            continue
        data = files[up["role"]][0]
        res = client.put(local_path(up["url"]), data=data, content_type=up["content_type"])
        assert res.status_code == 200


def upload(client, files=None, title="Bracket", skip=()):
    files = files or standard_files()
    session = presign(client, files).get_json()
    put_all(client, session, files, skip)
    res = client.post("/api/photos", json={"upload_token": session["upload_token"], "title": title,
                                           "description": "6061"})
    return session, res


def stored_keys(app):
    with app.app_context():
        return {o.key for o in storage().list_objects("users/")}


# ----- happy path and rendering -----

def test_upload_publishes_photo_with_variants(app):
    client = app.test_client()
    login(client, "alice")
    session, res = upload(client)
    assert res.status_code == 201

    keys = {u["role"]: u["key"] for u in session["uploads"]}
    assert keys["thumb"].endswith("/thumb.webp")
    assert keys["detail"].endswith("/detail.webp")
    assert keys["original"].endswith("/original.jpg")
    assert len({k.rsplit("/", 1)[0] for k in keys.values()}) == 1  # one folder per upload

    with app.app_context():
        photo = models.get_photo(res.get_json()["id"])
    assert (photo["thumb_width"], photo["thumb_height"]) == (640, 427)
    assert (photo["detail_width"], photo["detail_height"]) == (2048, 1365)
    assert photo["object_key"] == keys["original"]


def test_gallery_uses_thumbnail_and_detail_page_uses_detail(app):
    client = app.test_client()
    login(client, "alice")
    session, res = upload(client)
    keys = {u["role"]: u["key"] for u in session["uploads"]}

    anon = app.test_client()
    for page in ("/", "/u/alice"):
        html = anon.get(page).get_data(as_text=True)
        imgs = re.findall(r"<img src=\"(/_local-storage/[^\"]+)\"[^>]*>", html)
        assert imgs == [f"/_local-storage/{keys['thumb']}"]
        assert 'width="640" height="427"' in html
        assert "detail.webp" not in html and "original.jpg" not in html

    detail = anon.get(res.get_json()["url"]).get_data(as_text=True)
    assert f'src="/_local-storage/{keys["detail"]}"' in detail
    assert 'width="2048" height="1365"' in detail
    assert "640w" in detail and "2048w" in detail
    # Download serves the untouched original.
    download = re.search(r'href="(/_local-storage/[^"]+original\.jpg\?download=[^"]+)"', detail).group(1)
    assert anon.get(download.replace("&amp;", "&")).data == standard_files()["original"][0]


def test_legacy_photo_without_variants_renders_original(app):
    with app.app_context():
        user = models.upsert_oidc_user("dev:old", "old", "Old", None)
        pid = models.create_photo(user["id"], "Legacy", "", "users/1/abc.jpg", "image/jpeg", 10)
    anon = app.test_client()
    html = anon.get("/u/old").get_data(as_text=True)
    assert 'src="/_local-storage/users/1/abc.jpg"' in html
    assert "width=" not in re.search(r"<img src=\"/_local-storage[^>]*>", html).group(0)
    assert 'src="/_local-storage/users/1/abc.jpg"' in anon.get(f"/photos/{pid}").get_data(as_text=True)


def test_jpeg_fallback_variants_accepted(app):
    client = app.test_client()
    login(client, "alice")
    session, res = upload(client, standard_files("image/jpeg", "image/jpeg"))
    assert res.status_code == 201
    assert session["uploads"][1]["key"].endswith(".jpg")


def test_finalize_retry_is_idempotent(app):
    client = app.test_client()
    login(client, "alice")
    session, first = upload(client)
    again = client.post("/api/photos", json={"upload_token": session["upload_token"], "title": "Bracket"})
    assert again.status_code == 200
    assert again.get_json()["id"] == first.get_json()["id"]


# ----- presign validation -----

@pytest.mark.parametrize("mutate, message", [
    (lambda f: f.pop(), "Expected exactly"),
    (lambda f: f[0].update(content_type="text/html"), "Unsupported original"),
    (lambda f: f[1].update(content_type="image/png"), "WebP or JPEG"),
    (lambda f: f[1].update(width=641), "at most 640"),
    (lambda f: f[2].update(width=4096, height=10), "at most 2048"),
    (lambda f: f[1].update(size=11 * 1024 * 1024), "under"),
    (lambda f: f[0].update(size=26 * 1024 * 1024), "under"),
])
def test_presign_rejects_bad_declarations(app, mutate, message):
    client = app.test_client()
    login(client, "alice")
    files = [
        {"role": "original", "content_type": "image/jpeg", "size": 100},
        {"role": "thumb", "content_type": "image/webp", "size": 100, "width": 640, "height": 480},
        {"role": "detail", "content_type": "image/webp", "size": 100, "width": 2048, "height": 1536},
    ]
    mutate(files)
    res = client.post("/api/uploads", json={"files": files})
    assert res.status_code == 400
    assert message in res.get_json()["error"]


def test_put_must_match_signed_content_type(app):
    client = app.test_client()
    login(client, "alice")
    files = standard_files()
    session = presign(client, files).get_json()
    thumb = next(u for u in session["uploads"] if u["role"] == "thumb")
    res = client.put(local_path(thumb["url"]), data=b"x", content_type="image/png")
    assert res.status_code == 403


# ----- finalize verification and partial failures -----

def test_png_bytes_labeled_webp_are_rejected_and_cleaned_up(app):
    client = app.test_client()
    login(client, "alice")
    files = standard_files()
    # What a careless client would do if canvas fell back to PNG.
    files["thumb"] = (image_bytes("PNG", 640, 427), "image/webp", (640, 427))
    session, res = upload(client, files)
    assert res.status_code == 400
    assert "isn't a valid WEBP" in res.get_json()["error"]
    assert stored_keys(app) == set()


def test_actual_dimensions_must_match_declared(app):
    client = app.test_client()
    login(client, "alice")
    files = standard_files()
    files["detail"] = (image_bytes("WEBP", 2000, 1333), "image/webp", (2048, 1365))
    _, res = upload(client, files)
    assert res.status_code == 400
    assert "dimensions" in res.get_json()["error"]


def test_thumb_and_detail_must_share_shape(app):
    client = app.test_client()
    login(client, "alice")
    files = standard_files()
    files["thumb"] = (image_bytes("WEBP", 640, 640), "image/webp", (640, 640))
    _, res = upload(client, files)
    assert res.status_code == 400
    assert "same shape" in res.get_json()["error"]


def test_missing_upload_fails_and_deletes_the_rest(app):
    client = app.test_client()
    login(client, "alice")
    _, res = upload(client, skip={"original"})
    assert res.status_code == 400
    assert "original image never reached storage" in res.get_json()["error"]
    assert stored_keys(app) == set()
    with app.app_context():
        assert models.recent_photos() == []


def test_abort_deletes_partial_upload(app):
    client = app.test_client()
    login(client, "alice")
    files = standard_files()
    session = presign(client, files).get_json()
    put_all(client, session, files, skip={"detail"})
    assert len(stored_keys(app)) == 2
    assert client.post("/api/uploads/abort", json={"upload_token": session["upload_token"]}).status_code == 204
    assert stored_keys(app) == set()


def test_abort_never_deletes_published_objects(app):
    client = app.test_client()
    login(client, "alice")
    session, res = upload(client)
    assert res.status_code == 201
    client.post("/api/uploads/abort", json={"upload_token": session["upload_token"]})
    assert len(stored_keys(app)) == 3


def test_expired_session_rejected(app):
    client = app.test_client()
    login(client, "alice")
    app.config["UPLOAD_SESSION_TTL_SECONDS"] = -1
    _, res = upload(client)
    assert res.status_code == 400
    assert "expired" in res.get_json()["error"]


# ----- authorization -----

def test_cannot_finalize_or_abort_another_users_upload(app):
    alice, bob = app.test_client(), app.test_client()
    login(alice, "alice")
    login(bob, "bob")
    files = standard_files()
    session = presign(alice, files).get_json()
    put_all(alice, session, files)
    token = session["upload_token"]
    assert bob.post("/api/photos", json={"upload_token": token, "title": "mine"}).status_code == 403
    assert bob.post("/api/uploads/abort", json={"upload_token": token}).status_code == 403
    assert len(stored_keys(app)) == 3


def test_tampered_token_rejected(app):
    client = app.test_client()
    login(client, "alice")
    session = presign(client, standard_files()).get_json()
    res = client.post("/api/photos", json={"upload_token": session["upload_token"] + "x", "title": "t"})
    assert res.status_code == 400


def test_upload_requires_login(app):
    client = app.test_client()
    assert client.get("/upload").status_code == 302
    assert client.post("/api/uploads", json={}).status_code == 302
    assert client.post("/api/photos", json={}).status_code == 302


def test_only_owner_can_edit_or_delete_and_delete_removes_variants(app):
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
    assert stored_keys(app) == set()


def test_dev_login_disabled_without_debug(app):
    app.debug = False
    assert app.test_client().get("/auth/dev-login").status_code == 404


# ----- orphan cleanup -----

def test_cleanup_removes_only_old_unreferenced_objects(app):
    client = app.test_client()
    login(client, "alice")
    upload(client)  # published: kept
    files = standard_files()
    session = presign(client, files).get_json()
    put_all(client, session, files)  # abandoned: never finalized
    abandoned = {u["key"] for u in session["uploads"]}

    runner = app.test_cli_runner()
    out = runner.invoke(args=["cleanup-uploads"]).output
    assert "0 orphaned" in out  # too recent

    import os
    old = (datetime.now(timezone.utc) - timedelta(hours=48)).timestamp()
    root = app.extensions["storage"].root
    for key in abandoned:
        os.utime(root / key, (old, old))
    out = runner.invoke(args=["cleanup-uploads", "--dry-run"]).output
    assert "3 orphaned object(s) found" in out
    runner.invoke(args=["cleanup-uploads"])
    remaining = stored_keys(app)
    assert len(remaining) == 3 and not remaining & abandoned


# ----- detail variant reusing the original -----

def reuse_files(original, original_ct, detail_dims):
    w, h = detail_dims
    tw, th = (round(w * 640 / max(w, h)), round(h * 640 / max(w, h)))
    return {
        "original": (original, original_ct, None),
        "thumb": (image_bytes("WEBP", tw, th), "image/webp", (tw, th)),
    }, {"role": "detail", "content_type": original_ct, "size": len(original),
        "width": w, "height": h, "reuse_original": True}


def upload_reusing(client, files, detail_spec, title="Small photo"):
    specs = [{"role": r, "content_type": ct, "size": len(d), **({"width": dims[0], "height": dims[1]} if dims else {})}
             for r, (d, ct, dims) in files.items()]
    res = client.post("/api/uploads", json={"files": specs + [detail_spec]})
    if res.status_code != 200:
        return res, None
    session = res.get_json()
    put_all(client, session, files)
    return client.post("/api/photos", json={"upload_token": session["upload_token"], "title": title}), session


def test_small_original_is_reused_as_detail(app):
    client = app.test_client()
    login(client, "alice")
    files, detail = reuse_files(image_bytes("WEBP", 1500, 2000), "image/webp", (1500, 2000))
    res, session = upload_reusing(client, files, detail)
    assert res.status_code == 201
    assert sorted(u["role"] for u in session["uploads"]) == ["original", "thumb"]  # no detail PUT
    original_key = next(u["key"] for u in session["uploads"] if u["role"] == "original")

    with app.app_context():
        photo = models.get_photo(res.get_json()["id"])
    assert photo["detail_key"] == original_key
    assert (photo["detail_width"], photo["detail_height"]) == (1500, 2000)
    assert len(stored_keys(app)) == 2
    page = app.test_client().get(res.get_json()["url"]).get_data(as_text=True)
    assert f'src="/_local-storage/{original_key}" width="1500" height="2000"' in page

    assert client.post(f"/photos/{photo['id']}/delete").status_code == 302
    assert stored_keys(app) == set()


def test_reused_jpeg_accepts_exif_rotated_dimensions(app):
    # Stored 2000x1500 with Orientation=6: browsers show it as 1500x2000.
    from PIL import Image
    import io
    img = Image.new("RGB", (2000, 1500), (80, 80, 80))
    exif = Image.Exif()
    exif[0x0112] = 6
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif.tobytes())
    client = app.test_client()
    login(client, "alice")
    files, detail = reuse_files(buf.getvalue(), "image/jpeg", (1500, 2000))
    res, _ = upload_reusing(client, files, detail)
    assert res.status_code == 201


def test_reuse_rejects_wrong_dimensions(app):
    client = app.test_client()
    login(client, "alice")
    files, detail = reuse_files(image_bytes("PNG", 1500, 2000), "image/png", (1500, 2000))
    detail["height"] = 1999
    res, _ = upload_reusing(client, files, detail)
    assert res.status_code == 400
    assert "size doesn't match" in res.get_json()["error"]
    assert stored_keys(app) == set()


@pytest.mark.parametrize("fmt, ct, dims, message", [
    ("GIF", "image/gif", (800, 600), "can't be shown"),
    ("WEBP", "image/webp", (3000, 2000), "too large"),
])
def test_reuse_refused_at_presign(app, fmt, ct, dims, message):
    client = app.test_client()
    login(client, "alice")
    files, detail = reuse_files(image_bytes(fmt, *dims), ct, dims)
    res, _ = upload_reusing(client, files, detail)
    assert res.status_code == 400
    assert message in res.get_json()["error"]


def test_abort_with_reused_original(app):
    client = app.test_client()
    login(client, "alice")
    files, detail = reuse_files(image_bytes("WEBP", 1000, 800), "image/webp", (1000, 800))
    specs = [{"role": r, "content_type": ct, "size": len(d), **({"width": dims[0], "height": dims[1]} if dims else {})}
             for r, (d, ct, dims) in files.items()]
    session = client.post("/api/uploads", json={"files": specs + [detail]}).get_json()
    put_all(client, session, files)
    assert client.post("/api/uploads/abort", json={"upload_token": session["upload_token"]}).status_code == 204
    assert stored_keys(app) == set()


# ----- RETAIN_ORIGINALS off -----

def without_original(files):
    return {r: v for r, v in files.items() if r != "original"}


def test_without_originals_only_variants_are_stored(app):
    app.config["RETAIN_ORIGINALS"] = False
    client = app.test_client()
    login(client, "alice")
    session, res = upload(client, without_original(standard_files()))
    assert res.status_code == 201
    assert sorted(u["role"] for u in session["uploads"]) == ["detail", "thumb"]
    detail_key = next(u["key"] for u in session["uploads"] if u["role"] == "detail")

    with app.app_context():
        photo = models.get_photo(res.get_json()["id"])
    assert photo["object_key"] == photo["detail_key"] == detail_key
    assert photo["content_type"] == "image/webp"
    assert len(stored_keys(app)) == 2

    page = app.test_client().get(res.get_json()["url"]).get_data(as_text=True)
    assert ">Download<" in page and "Download original" not in page
    assert "<dt>Original</dt>" not in page
    assert f"{detail_key}?download=" in page

    assert client.post(f"/photos/{photo['id']}/delete").status_code == 302
    assert stored_keys(app) == set()


def test_without_originals_an_original_is_refused(app):
    app.config["RETAIN_ORIGINALS"] = False
    client = app.test_client()
    login(client, "alice")
    res = presign(client, standard_files())
    assert res.status_code == 400
    assert "detail, thumb" in res.get_json()["error"]


def test_with_originals_the_original_is_required(app):
    client = app.test_client()
    login(client, "alice")
    res = presign(client, without_original(standard_files()))
    assert res.status_code == 400
    assert "detail, original, thumb" in res.get_json()["error"]


def test_without_originals_a_small_original_is_still_kept_as_detail(app):
    app.config["RETAIN_ORIGINALS"] = False
    client = app.test_client()
    login(client, "alice")
    files, detail = reuse_files(image_bytes("WEBP", 1500, 2000), "image/webp", (1500, 2000))
    res, session = upload_reusing(client, files, detail)
    assert res.status_code == 201
    page = app.test_client().get(res.get_json()["url"]).get_data(as_text=True)
    assert "Download original" in page


def test_without_originals_retry_is_idempotent_and_failures_clean_up(app):
    app.config["RETAIN_ORIGINALS"] = False
    client = app.test_client()
    login(client, "alice")
    session, first = upload(client, without_original(standard_files()))
    again = client.post("/api/photos", json={"upload_token": session["upload_token"], "title": "Bracket"})
    assert again.status_code == 200 and again.get_json()["id"] == first.get_json()["id"]

    _, res = upload(client, without_original(standard_files()), skip={"detail"})
    assert res.status_code == 400
    assert len(stored_keys(app)) == 2  # only the first photo's files remain
