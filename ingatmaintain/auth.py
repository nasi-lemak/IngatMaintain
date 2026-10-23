"""Login, first-run setup, CSRF protection and access decorators."""

import functools
import hmac
import secrets

from flask import (
    Blueprint,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash

from .db import create_user, get_db

bp = Blueprint("auth", __name__)

PUBLIC_ENDPOINTS = {"auth.login", "auth.setup", "admin.calendar_feed", "static"}
# Share-link pages check their own token instead of a login.
PUBLIC_PREFIXES = ("shared.", "agent_api.")
# The agent API authenticates with device tokens, not cookies, so CSRF doesn't apply.
CSRF_EXEMPT_PREFIXES = ("agent_api.",)


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def login_required(view):
    @functools.wraps(view)
    def wrapped(**kwargs):
        if g.user is None:
            return redirect(url_for("auth.login", next=request.full_path))
        return view(**kwargs)

    return wrapped


def admin_required(view):
    @functools.wraps(view)
    def wrapped(**kwargs):
        if g.user is None:
            return redirect(url_for("auth.login", next=request.full_path))
        if not g.user["is_admin"]:
            abort(403)
        return view(**kwargs)

    return wrapped


def display_name(user=None):
    user = user or g.user
    return user["full_name"] or user["username"]


def _safe_next(target):
    # Only allow local redirects.
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("dashboard.index")


@bp.route("/login", methods=["GET", "POST"])
def login():
    conn = get_db()
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        return redirect(url_for("auth.setup"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = conn.execute("SELECT * FROM users WHERE username = ? AND active = 1", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session.permanent = True
            session["user_id"] = user["id"]
            return redirect(_safe_next(request.args.get("next")))
        flash("Wrong username or password.", "error")
    return render_template("auth/login.html")


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    """Create the first admin account. Only available while there are no users."""
    conn = get_db()
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not username or len(password) < 8:
            flash("Choose a username and a password of at least 8 characters.", "error")
        else:
            user_id = create_user(
                conn, username, password, request.form.get("full_name", ""), request.form.get("email", ""), True
            )
            session.clear()
            session.permanent = True
            session["user_id"] = user_id
            flash("Admin account created. Welcome to IngatMaintain!", "success")
            return redirect(url_for("dashboard.index"))
    return render_template("auth/setup.html")


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


def init_app(app):
    app.register_blueprint(bp)

    @app.before_request
    def load_user_and_check_csrf():
        user_id = session.get("user_id")
        g.user = None
        if user_id is not None:
            g.user = get_db().execute("SELECT * FROM users WHERE id = ? AND active = 1", (user_id,)).fetchone()
            if g.user is None:
                session.clear()

        if request.method == "POST" and not (request.endpoint or "").startswith(CSRF_EXEMPT_PREFIXES):
            sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
            expected = session.get("csrf") or ""
            if not expected or not hmac.compare_digest(sent, expected):
                abort(400, "Your session expired - please reload the page and try again.")

        endpoint = request.endpoint
        if g.user is None and endpoint is not None and endpoint not in PUBLIC_ENDPOINTS \
                and not endpoint.startswith(PUBLIC_PREFIXES):
            return redirect(url_for("auth.login", next=request.full_path))

    # A global (not a context variable) so imported macros can use it too.
    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.context_processor
    def inject_user():
        return {"current_user": g.get("user")}
