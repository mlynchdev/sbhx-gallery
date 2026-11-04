import os


def _bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).lower() in {"1", "true", "yes", "on"}


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-change-me")
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", False)

    # Database: "sqlite" for local dev, "d1" for Cloudflare D1 over its HTTP API.
    DB_BACKEND = os.environ.get("DB_BACKEND", "sqlite")
    SQLITE_PATH = os.environ.get("SQLITE_PATH", "instance/photo-share.sqlite3")
    CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "")
    CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "")
    D1_DATABASE_ID = os.environ.get("D1_DATABASE_ID", "")

    # Storage: "local" for dev (files under instance/uploads), "r2" for Cloudflare R2.
    STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND", "local")
    LOCAL_UPLOAD_DIR = os.environ.get("LOCAL_UPLOAD_DIR", "instance/uploads")
    R2_BUCKET = os.environ.get("R2_BUCKET", "")
    R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
    R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
    # Public base URL for the bucket (custom domain or r2.dev). If empty, images
    # are served through short-lived presigned GET URLs instead.
    R2_PUBLIC_BASE_URL = os.environ.get("R2_PUBLIC_BASE_URL", "").rstrip("/")

    UPLOAD_URL_TTL_SECONDS = int(os.environ.get("UPLOAD_URL_TTL_SECONDS", "600"))
    # How long a browser has to finish all uploads and save the photo.
    UPLOAD_SESSION_TTL_SECONDS = int(os.environ.get("UPLOAD_SESSION_TTL_SECONDS", "3600"))
    # Unreferenced objects older than this are removed by `flask cleanup-uploads`.
    ORPHAN_MAX_AGE_HOURS = int(os.environ.get("ORPHAN_MAX_AGE_HOURS", "24"))

    # Originals: kept as uploaded and offered through the Download button.
    # Turn off to store only the thumb and detail images: far less storage
    # (a 10 MP photo: ~0.45 MB instead of ~12 MB), but Download then gives the
    # detail image and full-resolution zoom is gone for new uploads. An
    # original that already fits the detail size is still kept, as the detail.
    RETAIN_ORIGINALS = _bool("RETAIN_ORIGINALS", True)
    MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
    ALLOWED_CONTENT_TYPES = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
        # Accepted only if the browser can decode them to build the variants
        # (Safari can; Chrome and Firefox can't decode HEIC).
        "image/heic": "heic",
        "image/heif": "heif",
        "image/avif": "avif",
    }

    # Browser-generated derivatives. Role names are fixed; sizes are not.
    IMAGE_VARIANTS = {
        "thumb": {
            "max_edge": int(os.environ.get("THUMB_MAX_EDGE", "640")),
            "quality": float(os.environ.get("THUMB_QUALITY", "0.85")),
        },
        "detail": {
            "max_edge": int(os.environ.get("DETAIL_MAX_EDGE", "2048")),
            "quality": float(os.environ.get("DETAIL_QUALITY", "0.90")),
        },
    }
    # Canvas encoders: WebP where the browser can encode it (Chromium, Firefox),
    # JPEG otherwise (Safari returns PNG for WebP requests).
    VARIANT_PREFERRED_TYPE = "image/webp"
    VARIANT_FALLBACK_TYPE = "image/jpeg"
    VARIANT_CONTENT_TYPES = {"image/webp": "webp", "image/jpeg": "jpg"}
    # When an original already fits a variant and re-encoding it would come
    # out larger, the variant points at the original instead, if it's one of
    # these. GIF is excluded so the photo page shows a still frame, and
    # HEIC/AVIF because not every browser displays them.
    REUSABLE_ORIGINAL_TYPES = ("image/jpeg", "image/png", "image/webp")
    MAX_VARIANT_BYTES = int(os.environ.get("MAX_VARIANT_BYTES", str(10 * 1024 * 1024)))

    # Authentik OIDC. Discovery URL looks like
    # https://auth.example.com/application/o/<app-slug>/.well-known/openid-configuration
    OIDC_DISCOVERY_URL = os.environ.get("OIDC_DISCOVERY_URL", "")
    OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "")
    OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "")

    # Local-only shortcut that skips Authentik. Refused unless Flask debug is on.
    DEV_LOGIN = _bool("DEV_LOGIN", False)
