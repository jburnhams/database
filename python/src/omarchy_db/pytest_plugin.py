"""pytest fixtures for apps that use omarchy_db (loaded automatically once omarchy-db is installed).

  db_engine    an Engine on a fresh database with the app's migrations applied, per test
  db_session   a Session on it (closed afterwards)
  db_url       a URL to a fresh migrated database, per test, for code that makes its own engine (connect(url))
  db_env       for app code that connects by itself (connect("myapp")): points OMARCHY_DB_URL and <APP>_DATABASE_URL
               at db_url for the test. Make it autouse in the app's conftest.py to cover every test
  db_backend   "sqlite" or "postgres"
  db_project   the app's Project ([tool.omarchy-db] in pyproject.toml)

Default backend: SQLite. The migrations run ONCE per session into a template; each test gets a copy (in memory for
db_engine, a file for db_url), so a test costs a copy, not a migration.
`pytest --pg` (or OMARCHY_DB_TEST=postgres, or engine = "postgres" in the project) uses a real Postgres instead:
a template database is migrated once and each test gets `CREATE DATABASE ... TEMPLATE` (a file copy on the server,
milliseconds), dropped afterwards. Admin connection: OMARCHY_DB_TEST_ADMIN_URL (see omarchy_db.scratch).

Loading this plugin switches on the test guard (omarchy_db.guard) before conftest.py or any app module is imported:
for the whole run, every Postgres connection in the process (connect(), create_engine(), psycopg.connect()) and in
its subprocesses (OMARCHY_DB_TESTING=1) is refused unless it is a throwaway omdb_* database. A test that would
reach the shared database fails with LiveDatabaseRefused instead.
"""
import os
import shutil
import tempfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from . import guard
from . import migrations as m
from .config import env_name
from .engine import _sqlite_pragmas, connect
from .project import Project
from .scratch import create_pg, drop_pg, sqlite_copy_to_memory, unique

guard.enter_test_mode()          # at import: before conftest.py and the app's modules are loaded


def pytest_addoption(parser):
    parser.addoption("--pg", action="store_true", help="run omarchy_db fixtures on a throwaway PostgreSQL database")


@pytest.fixture(scope="session")
def db_project(request) -> Project:
    return Project.load(request.config.rootpath)


@pytest.fixture(scope="session")
def db_backend(request, db_project) -> str:
    pg = request.config.getoption("--pg") or os.environ.get("OMARCHY_DB_TEST") == "postgres" or db_project.postgres_only
    return "postgres" if pg else "sqlite"


@pytest.fixture(scope="session")
def _db_template(db_project, db_backend):
    if db_backend == "postgres":
        name = unique(f"omdb_tmpl_{db_project.app}")
        url = create_pg(name)
        eng = create_engine(url)
        m.upgrade(eng, db_project.migrations_dir, db_project.metadata)
        eng.dispose()                          # a template must have no open connections when it is copied
        yield name
        drop_pg(name)
    else:
        d = tempfile.mkdtemp(prefix="omdb_")
        path = os.path.join(d, "template.db")
        eng = create_engine("sqlite:///" + path)      # no WAL: the template must be one self-contained file
        _sqlite_pragmas(eng, wal=False)
        m.upgrade(eng, db_project.migrations_dir, db_project.metadata)
        eng.dispose()
        yield path
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def db_engine(_db_template, db_backend, db_project):
    if db_backend == "postgres":
        name = unique(f"omdb_test_{db_project.app}")
        eng = create_engine(create_pg(name, template=_db_template))
        yield eng
        eng.dispose(); drop_pg(name)
    else:
        mem = sqlite_copy_to_memory(_db_template)
        eng = create_engine("sqlite://", creator=lambda: mem, poolclass=StaticPool)
        _sqlite_pragmas(eng, wal=False)
        yield eng
        eng.dispose(); mem.close()


@pytest.fixture
def db_session(db_engine):
    with Session(db_engine) as s:
        yield s


@pytest.fixture
def db_url(_db_template, db_backend, db_project, tmp_path):
    if db_backend == "postgres":
        name = unique(f"omdb_test_{db_project.app}")
        yield create_pg(name, template=_db_template)
        drop_pg(name)
    else:
        path = tmp_path / "test.db"
        shutil.copyfile(_db_template, path)
        yield f"sqlite:///{path}"


@pytest.fixture
def db_env(db_url, db_backend, db_project, monkeypatch) -> str:
    """Points the app's own connect("<app>") at a fresh migrated test database for this test (OMARCHY_DB_URL and
    <APP>_DATABASE_URL). On Postgres the fixtures' tables are in public, so the URL says search_path=public."""
    url = db_url + ("&" if "?" in db_url else "?") + "options=-csearch_path%3Dpublic" if db_backend == "postgres" else db_url
    monkeypatch.setenv("OMARCHY_DB_URL", url)
    monkeypatch.setenv(env_name(db_project.app), url)
    return url
