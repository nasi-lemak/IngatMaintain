import secrets
import sqlite3
from importlib import resources

import click
from flask import current_app, g
from werkzeug.security import generate_password_hash


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db(conn):
    schema = resources.files("ingatmaintain").joinpath("schema.sql").read_text()
    conn.executescript(schema)
    conn.commit()


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def calendar_token(conn):
    token = get_setting(conn, "calendar_token")
    if not token:
        token = secrets.token_urlsafe(24)
        set_setting(conn, "calendar_token", token)
    return token


def create_user(conn, username, password, full_name="", email="", is_admin=False):
    cur = conn.execute(
        "INSERT INTO users (username, full_name, email, password_hash, is_admin) VALUES (?, ?, ?, ?, ?)",
        (username.strip(), full_name.strip(), email.strip(), generate_password_hash(password), int(is_admin)),
    )
    conn.commit()
    return cur.lastrowid


def init_app(app):
    app.teardown_appcontext(close_db)

    conn = connect(app.config["DATABASE"])
    try:
        init_db(conn)
    finally:
        conn.close()

    @app.cli.command("create-user")
    @click.argument("username")
    @click.option("--name", default="", help="Full name")
    @click.option("--email", default="", help="Email for reminders")
    @click.option("--admin", is_flag=True, help="Give admin rights")
    @click.password_option()
    def create_user_command(username, name, email, admin, password):
        """Create a login."""
        try:
            create_user(get_db(), username, password, name, email, admin)
        except sqlite3.IntegrityError:
            raise click.ClickException(f"User {username} already exists")
        click.echo(f"Created user {username}")

    @app.cli.command("seed-example")
    def seed_example_command():
        """Load the NHG Khatib Polyclinic SSL renewal example."""
        from .seed import seed_example

        click.echo(seed_example(get_db()))

    @app.cli.command("send-reminders")
    @click.option("--dry-run", is_flag=True, help="Show what would be sent without sending")
    def send_reminders_command(dry_run):
        """Send any reminders that are due today (safe to run repeatedly)."""
        from .reminders import run_reminders

        result = run_reminders(get_db(), current_app.config, dry_run=dry_run)
        click.echo(result["message"])
        for line in result.get("lines", []):
            click.echo("  " + line)
