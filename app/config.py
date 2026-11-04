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
    MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
    ALLOWED_CONTENT_TYPES = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
    }

    # Authentik OIDC. Discovery URL looks like
    # https://auth.example.com/application/o/<app-slug>/.well-known/openid-configuration
    OIDC_DISCOVERY_URL = os.environ.get("OIDC_DISCOVERY_URL", "")
    OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "")
    OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "")

    # Local-only shortcut that skips Authentik. Refused unless Flask debug is on.
    DEV_LOGIN = _bool("DEV_LOGIN", False)
