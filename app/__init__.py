from dotenv import load_dotenv
from flask import Flask, render_template
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

from . import auth, db, storage, uploads, views
from .config import PLACEHOLDER_SECRET_KEYS, Config

csrf = CSRFProtect()


def create_app(config_overrides: dict | None = None) -> Flask:
    load_dotenv()
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(Config)
    if config_overrides:
        app.config.update(config_overrides)
    _check_production_config(app)
    # Trust X-Forwarded-* from one reverse proxy (Cloudflare tunnel, nginx, etc.)
    # so url_for(_external=True) builds https:// callback URLs.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

    csrf.init_app(app)
    db.init_app(app)
    storage.init_app(app)
    csrf.exempt(storage.local_bp)  # guarded by its own signed token
    auth.init_app(app)
    app.register_blueprint(views.bp)
    app.register_blueprint(uploads.bp)

    for code in (403, 404, 503):
        app.register_error_handler(
            code, lambda e, code=code: (render_template("error.html", error=e), code)
        )
    return app


def _check_production_config(app: Flask) -> None:
    """Refuse to serve real traffic with development settings."""
    if app.debug or app.testing:
        return
    if app.config["SECRET_KEY"] in PLACEHOLDER_SECRET_KEYS or len(app.config["SECRET_KEY"]) < 32:
        raise RuntimeError(
            "SECRET_KEY is unset or too short. It signs sessions and upload tokens; "
            "generate one with: python -c 'import secrets; print(secrets.token_hex(32))'"
        )
    if app.config["DB_BACKEND"] == "sqlite" or app.config["STORAGE_BACKEND"] == "local":
        app.logger.warning(
            "Running without debug on development backends (DB_BACKEND=%s, STORAGE_BACKEND=%s). "
            "Set DB_BACKEND=d1 and STORAGE_BACKEND=r2 for production.",
            app.config["DB_BACKEND"], app.config["STORAGE_BACKEND"],
        )
