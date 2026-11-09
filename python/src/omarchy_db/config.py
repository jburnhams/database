"""Where an app's data is.

Layout on the shared server: ONE database (default name `omarchy`; set OMARCHY_DB_DATABASE) with a schema per app,
owned by the app's role, so apps can query each other's tables (`select ... from notes.notes join photos.albums ...`).
Order: OMARCHY_DB_URL (any app; handy in tests and one-off runs), then <APP>_DATABASE_URL (e.g. NOTES_DATABASE_URL),
then the default: postgresql+psycopg:///<database>?options=-csearch_path%3D<schema> over the Unix socket with peer auth
(you connect as your OS user, a member of the app's owner role). The search_path makes the app's unqualified table
names resolve to its schema. A plain path ("data/app.db", "~/x.sqlite") means SQLite, which has no schemas.

A real Postgres database is opt-in (see omarchy_db.guard): url_for() and connect() refuse one unless the process
has OMARCHY_DB_LIVE=1, and refuse it always during tests.
"""
import os
import re


def normalise_url(url: str) -> str:
    """postgresql:///x -> postgresql+psycopg:///x (psycopg 3); a bare path -> sqlite:///abs/path."""
    if url.startswith(("postgresql://", "postgres://")):
        return "postgresql+psycopg://" + url.split("://", 1)[1]
    if "://" not in url and not url.startswith("sqlite:"):
        return "sqlite:///" + os.path.abspath(os.path.expanduser(url))
    return url


def env_name(app: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", app.upper()) + "_DATABASE_URL"


DATABASE = os.environ.get("OMARCHY_DB_DATABASE", "omarchy")   # the shared database


def schema_url(schema: str, database: str = DATABASE) -> str:
    return f"postgresql+psycopg:///{database}?options=-csearch_path%3D{schema}"


def with_search_path(url: str, schema: str) -> str:
    """A Postgres URL that resolves unqualified names in `schema`, unless it already sets search_path itself.
    Without this, a URL from the environment would read and write a different schema (public) from the one
    Project.upgrade() migrates (the app's)."""
    if not url.startswith("postgresql") or "search_path" in url:
        return url
    return url + ("&" if "?" in url else "?") + f"options=-csearch_path%3D{schema}"


def resolve_url(app: str, schema: str | None = None) -> str:
    """Where `app`'s data is (see the module docstring), without the live-database check."""
    for var in ("OMARCHY_DB_URL", env_name(app)):
        if os.environ.get(var):
            return with_search_path(normalise_url(os.environ[var]), schema or app)
    return schema_url(schema or app)


def url_for(app: str, schema: str | None = None) -> str:
    """resolve_url(), refused (guard.LiveDatabaseRefused) when it is a real Postgres database and the process has
    not opted in with OMARCHY_DB_LIVE=1, or is running tests."""
    from .guard import check_url
    url = resolve_url(app, schema)
    check_url(url, f"url_for({app!r})")
    return url
