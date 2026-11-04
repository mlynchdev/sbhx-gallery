"""Sign-in through Authentik (OpenID Connect)."""

from __future__ import annotations

from functools import wraps
from urllib.parse import urlencode

from authlib.integrations.flask_client import OAuth
from flask import (
    Blueprint,
    Flask,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from . import models

oauth = OAuth()
bp = Blueprint("auth", __name__, url_prefix="/auth")


def init_app(app: Flask) -> None:
    oauth.init_app(app)
    if app.config["OIDC_DISCOVERY_URL"]:
        oauth.register(
            "authentik",
            server_metadata_url=app.config["OIDC_DISCOVERY_URL"],
            client_id=app.config["OIDC_CLIENT_ID"],
            client_secret=app.config["OIDC_CLIENT_SECRET"],
            client_kwargs={"scope": "openid profile email"},
        )
    app.register_blueprint(bp)

    @app.before_request
    def load_user() -> None:
        uid = session.get("user_id")
        g.user = models.get_user(uid) if uid else None
        if uid and g.user is None:
            session.clear()

    @app.context_processor
    def inject_user():
        return {"current_user": g.get("user"), "dev_login": _dev_login_enabled()}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("auth.login", next=request.full_path))
        return view(*args, **kwargs)

    return wrapped


def _safe_next(target: str | None) -> str:
    # Only allow same-site relative paths, never "//evil.com" or absolute URLs.
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("main.index")


def _sign_in(user: dict) -> None:
    next_url = session.get("next")
    session.clear()
    session["user_id"] = user["id"]
    session["next"] = next_url


def _dev_login_enabled() -> bool:
    return current_app.config["DEV_LOGIN"] and current_app.debug


@bp.get("/login")
def login():
    session["next"] = _safe_next(request.args.get("next"))
    client = oauth.create_client("authentik")
    if client is None:
        if _dev_login_enabled():
            return redirect(url_for("auth.dev_login"))
        abort(503, "Sign-in is not configured (set OIDC_DISCOVERY_URL).")
    return client.authorize_redirect(url_for("auth.callback", _external=True))


@bp.get("/callback")
def callback():
    client = oauth.create_client("authentik")
    if client is None:
        abort(404)
    token = client.authorize_access_token()
    info = token.get("userinfo") or client.userinfo()
    user = models.upsert_oidc_user(
        sub=info["sub"],
        preferred_username=info.get("preferred_username", ""),
        name=info.get("name", ""),
        email=info.get("email"),
    )
    _sign_in(user)
    session["id_token"] = token.get("id_token")
    return redirect(_safe_next(session.pop("next", None)))


@bp.post("/logout")
def logout():
    id_token = session.get("id_token")
    session.clear()
    client = oauth.create_client("authentik")
    if client is not None:
        end_session = client.load_server_metadata().get("end_session_endpoint")
        if end_session:
            params = {"post_logout_redirect_uri": url_for("main.index", _external=True)}
            if id_token:
                params["id_token_hint"] = id_token
            return redirect(f"{end_session}?{urlencode(params)}")
    return redirect(url_for("main.index"))


@bp.route("/dev-login", methods=["GET", "POST"])
def dev_login():
    """Pick any username and sign in. Exists only with DEV_LOGIN=1 and debug on."""
    if not _dev_login_enabled():
        abort(404)
    if request.method == "POST":
        name = request.form.get("username", "").strip() or "maker"
        user = models.upsert_oidc_user(f"dev:{name}", name, name, None)
        _sign_in(user)
        flash(f"Signed in as {user['username']} (dev login).", "info")
        return redirect(_safe_next(session.pop("next", None)))
    return render_template("auth/dev_login.html")
