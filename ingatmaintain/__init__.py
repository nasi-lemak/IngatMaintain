"""IngatMaintain - maintenance reminders and site checklists for on-prem systems."""

import os
import secrets
from datetime import timedelta

from flask import Flask


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _secret_key(data_dir):
    # Kept next to the database so it survives container rebuilds.
    path = os.path.join(data_dir, "secret_key")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(path, "w") as f:
        f.write(key)
    return key


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    os.makedirs(app.instance_path, exist_ok=True)

    app.config.from_mapping(
        DATABASE=os.environ.get("DATABASE_PATH") or os.path.join(app.instance_path, "ingatmaintain.sqlite3"),
        TIMEZONE=os.environ.get("APP_TIMEZONE", "Asia/Singapore"),
        BASE_URL=os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/"),
        # Email reminders (optional)
        SMTP_HOST=os.environ.get("SMTP_HOST", ""),
        SMTP_PORT=int(os.environ.get("SMTP_PORT", "587")),
        SMTP_USER=os.environ.get("SMTP_USER", ""),
        SMTP_PASSWORD=os.environ.get("SMTP_PASSWORD", ""),
        SMTP_FROM=os.environ.get("SMTP_FROM", ""),
        SMTP_STARTTLS=env_bool("SMTP_STARTTLS", True),
        SMTP_SSL=env_bool("SMTP_SSL", False),
        # Chat reminders (optional)
        TELEGRAM_BOT_TOKEN=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        TELEGRAM_CHAT_ID=os.environ.get("TELEGRAM_CHAT_ID", ""),
        WEBHOOK_URL=os.environ.get("WEBHOOK_URL", ""),
        # Built-in daily scheduler (used by run.py)
        REMINDER_SCHEDULER=env_bool("REMINDER_SCHEDULER", True),
        REMINDER_HOUR=int(os.environ.get("REMINDER_HOUR", "8")),
        # Remind again every N days while a site is behind on software (0 = first notice only).
        OUTDATED_REMINDER_DAYS=int(os.environ.get("OUTDATED_REMINDER_DAYS", "30")),
        # Fleet monitoring (agents on internet-connected client computers)
        FLEET_MONITOR=env_bool("FLEET_MONITOR", True),
        FLEET_CHECKIN_MINUTES=int(os.environ.get("FLEET_CHECKIN_MINUTES", "5")),
        FLEET_CHECK_MINUTES=int(os.environ.get("FLEET_CHECK_MINUTES", "5")),
        FLEET_OFFLINE_HOURS=int(os.environ.get("FLEET_OFFLINE_HOURS", "24")),
        FLEET_CERT_WARN_DAYS=int(os.environ.get("FLEET_CERT_WARN_DAYS", "30")),
        FLEET_ENROLL_DAYS=int(os.environ.get("FLEET_ENROLL_DAYS", "7")),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=env_bool("SESSION_COOKIE_SECURE", False),
        # How long a device stays logged in after its last visit.
        PERMANENT_SESSION_LIFETIME=timedelta(days=int(os.environ.get("SESSION_LIFETIME_DAYS", "365"))),
        # How long a job's share link works (it also stops when the job is completed or cancelled).
        SHARE_LINK_DAYS=int(os.environ.get("SHARE_LINK_DAYS", "30")),
    )
    if test_config:
        app.config.update(test_config)
    data_dir = os.path.dirname(os.path.abspath(app.config["DATABASE"]))
    os.makedirs(data_dir, exist_ok=True)
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or _secret_key(data_dir)

    if env_bool("BEHIND_PROXY", False):
        # Behind nginx / Caddy / IIS doing HTTPS: trust one proxy's X-Forwarded-* headers.
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    from . import auth, db, util
    from .views import admin, clients, dashboard, fleet, items, jobs, procedures, shared, software

    db.init_app(app)
    auth.init_app(app)
    for module in (dashboard, clients, items, procedures, jobs, admin, shared, software, fleet):
        app.register_blueprint(module.bp)
    app.register_blueprint(fleet.api)

    app.jinja_env.filters["fmt_date"] = util.fmt_date

    @app.context_processor
    def inject_helpers():
        return {"today": util.today()}

    return app
