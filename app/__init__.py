from dotenv import load_dotenv
from flask import Flask, render_template
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

from . import auth, db, storage, uploads, views
from .config import Config

csrf = CSRFProtect()


def create_app(config_overrides: dict | None = None) -> Flask:
    load_dotenv()
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(Config)
    if config_overrides:
        app.config.update(config_overrides)
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
