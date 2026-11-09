"""Keeping tests and stray processes off real data.

Two rules:

1. **Tests never open a real database.** During a test run (pytest with this package installed, which loads the
   plugin; or OMARCHY_DB_TESTING=1, which the plugin sets so that subprocesses inherit it; or PYTEST_CURRENT_TEST)
   the only Postgres databases that can be opened are throwaway ones: names starting `omdb_` (what the fixtures,
   scratch_database() and dbtool's scratch runs create) and the admin database they are created through
   (OMARCHY_DB_TEST_ADMIN_URL). Anything else raises LiveDatabaseRefused, whatever the environment says, including
   OMARCHY_DB_LIVE. Under pytest this is enforced on every psycopg (and psycopg2) connection in the process, so it
   also covers code that calls create_engine() or psycopg.connect() itself, not only connect().
2. **A real database is opt-in.** Outside tests, connect() and url_for() open a Postgres database that is not a
   throwaway one only when the process says so: OMARCHY_DB_LIVE=1 in its environment (set it in the app's systemd
   unit or launcher, not in your shell profile), or dbtool's --live. Without it there is no default database.

SQLite is never restricted: a file path is always deliberate, and tests use in-memory or temporary copies.
"""
import os

THROWAWAY_PREFIX = "omdb_"


class LiveDatabaseRefused(RuntimeError):
    """A connection to a real (non-throwaway) database was refused: under tests always, otherwise without
    OMARCHY_DB_LIVE=1."""


_testing = False


def testing() -> bool:
    return _testing or os.environ.get("OMARCHY_DB_TESTING") == "1" or "PYTEST_CURRENT_TEST" in os.environ


def live_allowed() -> bool:
    return os.environ.get("OMARCHY_DB_LIVE") == "1" and not testing()


def allow_live():
    """Opt this process in to real databases (dbtool --live). Has no effect under tests."""
    os.environ["OMARCHY_DB_LIVE"] = "1"


def is_throwaway(dbname: str | None) -> bool:
    return bool(dbname) and dbname.startswith(THROWAWAY_PREFIX)


def _admin_database() -> str | None:
    from sqlalchemy.engine import make_url
    from .config import DATABASE
    from .scratch import admin_url
    name = make_url(admin_url()).database or None
    return None if name == DATABASE else name          # the shared database is never an admin database


def check_database(dbname: str | None, where: str = "connect"):
    """Raise LiveDatabaseRefused unless the Postgres database `dbname` may be opened by this process now."""
    if is_throwaway(dbname):
        return
    if testing():
        if dbname and dbname == _admin_database():
            return
        raise LiveDatabaseRefused(
            f"{where}: refused Postgres database {dbname!r} during tests. Tests may only use throwaway "
            f"{THROWAWAY_PREFIX}* databases: use the db_env / db_url / db_engine / db_session fixtures "
            "(db_env points the app's own connect(app) at a fresh test database).")
    if not live_allowed():
        raise LiveDatabaseRefused(
            f"{where}: {dbname!r} is a real database and this process has not opted in. Set OMARCHY_DB_LIVE=1 "
            "in the app's service environment (systemd unit, launcher), or pass --live to dbtool.")


def check_url(url: str, where: str = "connect"):
    """check_database() for a SQLAlchemy URL; SQLite and other non-Postgres URLs pass."""
    from sqlalchemy.engine import make_url
    u = make_url(url)
    if u.get_backend_name() == "postgresql":
        check_database(u.database or os.environ.get("PGDATABASE") or u.username or os.environ.get("USER"), where)


def enter_test_mode():
    """Called by the pytest plugin as it loads (before conftest.py and app modules are imported)."""
    global _testing
    _testing = True
    os.environ["OMARCHY_DB_TESTING"] = "1"
    _guard_drivers()


def _guard_drivers():
    """Check the database of every new psycopg / psycopg2 connection (so raw create_engine and driver calls are
    covered too). The check runs right after connecting, before any statement; a refused connection is closed."""
    try:
        import psycopg
    except ImportError:
        psycopg = None
    if psycopg is not None and not getattr(psycopg, "_omdb_guarded", False):
        sync_connect = psycopg.Connection.connect.__func__
        async_connect = psycopg.AsyncConnection.connect.__func__

        def connect(cls, *a, **k):
            conn = sync_connect(cls, *a, **k)
            try: check_database(conn.info.dbname, "psycopg.connect")
            except LiveDatabaseRefused: conn.close(); raise
            return conn

        async def aconnect(cls, *a, **k):
            conn = await async_connect(cls, *a, **k)
            try: check_database(conn.info.dbname, "psycopg.AsyncConnection.connect")
            except LiveDatabaseRefused: await conn.close(); raise
            return conn

        psycopg.Connection.connect = classmethod(connect)
        psycopg.AsyncConnection.connect = classmethod(aconnect)
        psycopg.connect = psycopg.Connection.connect          # the module-level alias SQLAlchemy calls
        psycopg._omdb_guarded = True
    try:
        import psycopg2
    except ImportError:
        return
    if not getattr(psycopg2, "_omdb_guarded", False):
        orig = psycopg2.connect

        def connect2(*a, **k):
            conn = orig(*a, **k)
            try: check_database(conn.info.dbname, "psycopg2.connect")
            except LiveDatabaseRefused: conn.close(); raise
            return conn

        psycopg2.connect = connect2
        psycopg2._omdb_guarded = True
