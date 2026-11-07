"""End-to-end tests of omarchy_db on a temporary app project: dbtool init / revision / check / migrate / new-app,
upgrade at start, URL config. Postgres tests run when a server is reachable (OMARCHY_DB_TEST_ADMIN_URL) and are
skipped otherwise; CI runs them against a service container."""
import os
import subprocess
import sys
import textwrap
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text

from omarchy_db import Project, connect, normalise_url, url_for
from omarchy_db.migrations import check_migrations, current, drift, upgrade
from omarchy_db.scratch import admin_url, pg_available, scratch_database

PG = pg_available()
needs_pg = pytest.mark.skipif(not PG, reason="no Postgres server reachable")

MODELS_V1 = '''
from sqlalchemy.orm import Mapped, mapped_column
from omarchy_db import make_base
Base = make_base()

class Thing(Base):
    __tablename__ = "things"
    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    name: Mapped[str]
'''
MODELS_V2 = MODELS_V1 + '''    size: Mapped[int | None] = mapped_column(default=None)
    colour: Mapped[str] = mapped_column(server_default="red", default="red")
'''


def dbtool(cwd, *args, check=True):
    r = subprocess.run([sys.executable, "-m", "omarchy_db.cli", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode: raise AssertionError(f"dbtool {args} failed:\n{r.stdout}\n{r.stderr}")
    return r


@pytest.fixture
def app(tmp_path, monkeypatch):
    """A fresh project: things/models.py plus [tool.omarchy-db] written by dbtool init."""
    name = f"t{uuid.uuid4().hex[:8]}"
    (tmp_path / "things").mkdir()
    (tmp_path / "things" / "__init__.py").write_text("")
    (tmp_path / "things" / "models.py").write_text(textwrap.dedent(MODELS_V1))
    dbtool(tmp_path, "init", "--app", name, "--models", "things.models:Base")
    monkeypatch.syspath_prepend(str(tmp_path))
    return tmp_path


def test_url_config(monkeypatch):
    monkeypatch.delenv("OMARCHY_DB_URL", raising=False)
    monkeypatch.delenv("BH_DATABASE_URL", raising=False)
    assert url_for("bh") == "postgresql+psycopg:///omarchy?options=-csearch_path%3Dbh"      # one database, a schema per app
    assert url_for("bh", "faces") == "postgresql+psycopg:///omarchy?options=-csearch_path%3Dfaces"
    monkeypatch.setenv("BH_DATABASE_URL", "postgresql:///other")
    assert url_for("bh") == "postgresql+psycopg:///other?options=-csearch_path%3Dbh"   # same schema as migrations use
    monkeypatch.setenv("BH_DATABASE_URL", "postgresql://u@h/db?sslmode=disable")
    assert url_for("bh") == "postgresql+psycopg://u@h/db?sslmode=disable&options=-csearch_path%3Dbh"
    monkeypatch.setenv("BH_DATABASE_URL", "postgresql:///db?options=-csearch_path%3Dx")
    assert url_for("bh") == "postgresql+psycopg:///db?options=-csearch_path%3Dx"          # an explicit choice is kept
    monkeypatch.setenv("OMARCHY_DB_URL", "data/x.db")
    assert url_for("bh") == "sqlite:///" + os.path.abspath("data/x.db")
    assert normalise_url("sqlite:///:memory:") == "sqlite:///:memory:"


def test_connect_sqlite_pragmas(tmp_path):
    eng = connect(str(tmp_path / "a.db"))
    with eng.connect() as c:
        assert c.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert c.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
    eng.dispose()


def test_init_writes_config(app):
    p = Project.load(app)
    assert p.app.startswith("t") and p.migrations_dir == app / "migrations" and p.engine == "portable"
    assert "things" in p.metadata.tables
    assert dbtool(app, "init", "--app", "x", "--models", "m:B", check=False).returncode != 0   # not twice


@pytest.mark.parametrize("backend", ["sqlite", pytest.param("postgres", marks=needs_pg)])
def test_autogenerate_lifecycle(app, backend):
    flag = "--pg" if backend == "postgres" else "--sqlite"
    out = dbtool(app, "revision", "-m", "initial", flag).stdout
    assert "wrote migrations/" in out
    assert "no changes" in dbtool(app, "revision", "-m", "again", flag).stdout        # nothing written twice
    assert len(list((app / "migrations").glob("*.py"))) == 1
    dbtool(app, "check", flag)
    # Change the model without a revision: check must fail ...
    (app / "things" / "models.py").write_text(textwrap.dedent(MODELS_V2))
    r = dbtool(app, "check", flag, check=False)
    assert r.returncode == 1 and "FAIL" in r.stdout
    # ... until the revision is generated: an ALTER that runs on both engines
    out = dbtool(app, "revision", "-m", "add size and colour", flag).stdout
    rev = next(f for f in (app / "migrations").glob("*add_size*.py")).read_text()
    assert "batch_alter_table" in rev and "add_column" in rev
    r = dbtool(app, "check", check=False)                                               # both engines when reachable
    assert r.returncode == 0, r.stdout + r.stderr


def test_migrate_and_upgrade_at_start(app, tmp_path):
    dbtool(app, "revision", "-m", "initial", "--sqlite")
    url = f"sqlite:///{tmp_path / 'live.db'}"
    out = dbtool(app, "migrate", "--url", url).stdout
    assert "empty ->" in out
    eng = connect(url)
    p = Project.load(app)
    p.upgrade(eng)                                    # calling again at every start is harmless
    assert current(eng) is not None and drift(eng, p.metadata) == []
    assert dbtool(app, "current", "--url", url).stdout.strip() == current(eng)
    eng.dispose()


@needs_pg
def test_new_app_schema_ownership_isolation_and_cross_schema_reads(tmp_path, monkeypatch):
    """Two apps in one shared database: each gets a role and a schema; migrations land in the app's schema owned by
    its role; neither app role can see the other's schema; the admin (member of both) can join across them."""
    db = f"omdb_shared_{uuid.uuid4().hex[:8]}"
    apps = [f"a{uuid.uuid4().hex[:6]}", f"b{uuid.uuid4().hex[:6]}"]
    adm = create_engine(admin_url(), isolation_level="AUTOCOMMIT")
    try:
        for i, name in enumerate(apps):
            root = tmp_path / name
            (root / "things").mkdir(parents=True)
            (root / "things" / "__init__.py").write_text("")
            (root / "things" / "models.py").write_text(textwrap.dedent(MODELS_V1))
            dbtool(root, "init", "--app", name, "--models", "things.models:Base")
            dbtool(root, "revision", "-m", "initial", "--pg")
            out = dbtool(root, "new-app", "--database", db).stdout
            assert f"created schema {db}.{name}" in out and f"created role {name}" in out
            assert "created" not in dbtool(root, "new-app", "--database", db).stdout             # idempotent
            url = create_engine(admin_url()).url.set(database=db).render_as_string(hide_password=False)
            dbtool(root, "migrate", "--url", url)                                               # no search_path in the URL
            assert dbtool(root, "current", "--url", url).stdout.strip() != "empty"
            eng = create_engine(url)
            with eng.begin() as c:
                c.execute(text(f'INSERT INTO "{name}".things (name) VALUES (:n)'), {"n": f"from {name}"})
            eng.dispose()
        eng = create_engine(create_engine(admin_url()).url.set(database=db).render_as_string(hide_password=False))
        with eng.connect() as c:
            a, b = apps
            owners = dict(c.execute(text("SELECT schemaname, tableowner FROM pg_tables WHERE schemaname = ANY(:s) AND tablename = 'things'"), {"s": apps}).all())
            assert owners == {a: a, b: b}
            versions = c.execute(text("SELECT schemaname FROM pg_tables WHERE tablename = 'alembic_version' ORDER BY 1")).scalars().all()
            assert versions == sorted(apps)                                                     # version table per schema
            assert c.execute(text("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")).scalar() == 0
            # cross-schema read as the admin (member of both roles)
            rows = c.execute(text(f'SELECT x.name, y.name FROM "{a}".things x CROSS JOIN "{b}".things y')).one()
            assert rows == (f"from {a}", f"from {b}")
            # isolation: neither app role may use the other's schema
            assert c.execute(text("SELECT has_schema_privilege(:r, :s, 'USAGE')"), {"r": a, "s": b}).scalar() is False
            assert c.execute(text("SELECT has_schema_privilege(:r, :s, 'USAGE')"), {"r": b, "s": a}).scalar() is False
            c.rollback()
            with c.begin():
                c.execute(text(f'SET LOCAL ROLE "{a}"'))
                with pytest.raises(Exception, match="permission denied"):
                    c.execute(text(f'SELECT * FROM "{b}".things'))
        eng.dispose()
    finally:
        with adm.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)'))
            for r in apps: c.execute(text(f'DROP ROLE IF EXISTS "{r}"'))
        adm.dispose()


@pytest.mark.parametrize("backend", ["sqlite", pytest.param("postgres", marks=needs_pg)])
def test_check_migrations_catches_a_broken_downgrade(app, backend):
    dbtool(app, "revision", "-m", "initial", "--sqlite")
    rev = next((app / "migrations").glob("*.py"))
    rev.write_text(rev.read_text().replace("op.drop_table('things')", "pass"))      # downgrade forgets the table
    p = Project.load(app)
    with scratch_database(backend == "postgres") as url:
        eng = connect(url) if url.startswith("sqlite") else create_engine(url)
        problems = check_migrations(eng, p.migrations_dir, p.metadata)
        eng.dispose()
    assert any("downgrade to base left tables" in x for x in problems)


PARENT_CHILD_V1 = '''
from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column
from omarchy_db import make_base
Base = make_base()

class Author(Base):
    __tablename__ = "authors"
    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    name: Mapped[str] = mapped_column(String(50))

class Note(Base):
    __tablename__ = "notes"
    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    author_id: Mapped[int] = mapped_column(ForeignKey("authors.id"))
'''


@pytest.mark.parametrize("backend", ["sqlite", pytest.param("postgres", marks=needs_pg)])
def test_batch_migration_of_a_referenced_table_with_data(app, backend):
    """Recreating a parent table (SQLite batch mode) while child rows point at it must not trip the foreign keys,
    and the data must survive. The scratch-database checks have no rows, so this needs its own test."""
    models = app / "things" / "models.py"
    models.write_text(textwrap.dedent(PARENT_CHILD_V1))
    flag = "--pg" if backend == "postgres" else "--sqlite"
    dbtool(app, "revision", "-m", "initial", flag)
    with scratch_database(backend == "postgres") as url:
        dbtool(app, "migrate", "--url", url)
        if not url.startswith("sqlite"):                       # dbtool migrate put the tables in the app's schema
            url += "?options=-csearch_path%3D" + Project.load(app).schema
        eng = connect(url) if url.startswith("sqlite") else create_engine(url)
        with eng.begin() as c:
            c.exec_driver_sql("INSERT INTO authors (id, name) VALUES (1, 'Ada')")
            c.exec_driver_sql("INSERT INTO notes (id, author_id) VALUES (1, 1)")
        eng.dispose()
        models.write_text(textwrap.dedent(PARENT_CHILD_V1).replace("String(50)", "String(200)"))
        dbtool(app, "revision", "-m", "longer names", flag)
        r = dbtool(app, "migrate", "--url", url, check=False)
        assert r.returncode == 0, r.stderr[-2000:]
        eng = connect(url) if url.startswith("sqlite") else create_engine(url)
        with eng.connect() as c:
            assert c.exec_driver_sql("SELECT a.name FROM notes n JOIN authors a ON a.id = n.author_id").scalar() == "Ada"
            if backend == "sqlite":
                assert c.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1          # enforcement is back on
        eng.dispose()


def test_migration_that_breaks_foreign_keys_is_rolled_back(app, tmp_path):
    """With enforcement off during the migration, a migration that leaves dangling references must fail and roll back."""
    (app / "things" / "models.py").write_text(textwrap.dedent(PARENT_CHILD_V1))
    dbtool(app, "revision", "-m", "initial", "--sqlite")
    url = f"sqlite:///{tmp_path / 'fk.db'}"
    dbtool(app, "migrate", "--url", url)
    eng = connect(url)
    with eng.begin() as c:
        c.exec_driver_sql("INSERT INTO authors (id, name) VALUES (1, 'Ada')")
        c.exec_driver_sql("INSERT INTO notes (id, author_id) VALUES (1, 1)")
    p = Project.load(app)
    bad = p.migrations_dir / "zz_bad.py"
    head = current(eng)
    bad.write_text(textwrap.dedent(f'''
        from alembic import op
        revision = "bad0001"; down_revision = "{head}"; branch_labels = None; depends_on = None
        def upgrade(): op.execute("DELETE FROM authors")
        def downgrade(): pass
    '''))
    with pytest.raises(Exception, match="foreign key"):
        p.upgrade(eng)
    assert current(eng) == head
    with eng.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM authors").scalar() == 1
    eng.dispose()


@needs_pg
def test_env_url_without_search_path_uses_the_migrated_schema(app, monkeypatch):
    """MYAPP_DATABASE_URL without a search_path: connect(app) must see the tables that Project.upgrade() made."""
    p = Project.load(app)
    dbtool(app, "revision", "-m", "initial", "--pg")
    with scratch_database(True) as url:
        monkeypatch.setenv("OMARCHY_DB_URL", url)                         # no search_path in it
        eng = connect(p.app)
        p.upgrade(eng)
        with eng.begin() as c:
            c.execute(text("INSERT INTO things (name) VALUES ('x')"))     # unqualified, as app code is
            assert c.execute(text("SELECT current_schema()")).scalar() == p.schema
        eng.dispose()
