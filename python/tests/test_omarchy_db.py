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

import psycopg

from omarchy_db import LiveDatabaseRefused, Project, connect, normalise_url, resolve_url, url_for
from omarchy_db.migrations import check_migrations, current, drift, upgrade
from omarchy_db.scratch import admin_url, drop_pg, pg_available, pg_url, scratch_database

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
    assert resolve_url("bh") == "postgresql+psycopg:///omarchy?options=-csearch_path%3Dbh"      # one database, a schema per app
    assert resolve_url("bh", "faces") == "postgresql+psycopg:///omarchy?options=-csearch_path%3Dfaces"
    monkeypatch.setenv("BH_DATABASE_URL", "postgresql:///other")
    assert resolve_url("bh") == "postgresql+psycopg:///other?options=-csearch_path%3Dbh"   # same schema as migrations use
    monkeypatch.setenv("BH_DATABASE_URL", "postgresql://u@h/db?sslmode=disable")
    assert resolve_url("bh") == "postgresql+psycopg://u@h/db?sslmode=disable&options=-csearch_path%3Dbh"
    monkeypatch.setenv("BH_DATABASE_URL", "postgresql:///db?options=-csearch_path%3Dx")
    assert resolve_url("bh") == "postgresql+psycopg:///db?options=-csearch_path%3Dx"          # an explicit choice is kept
    monkeypatch.setenv("OMARCHY_DB_URL", "data/x.db")
    assert url_for("bh") == "sqlite:///" + os.path.abspath("data/x.db")
    assert normalise_url("sqlite:///:memory:") == "sqlite:///:memory:"


def test_guard_refuses_real_databases_in_tests(monkeypatch):
    """Under pytest the shared database is refused, even with OMARCHY_DB_LIVE=1 and whichever way it is named."""
    monkeypatch.delenv("OMARCHY_DB_URL", raising=False)
    monkeypatch.delenv("BH_DATABASE_URL", raising=False)
    monkeypatch.setenv("OMARCHY_DB_LIVE", "1")
    for attempt in (lambda: url_for("bh"), lambda: connect("bh"), lambda: connect("postgresql:///omarchy"),
                    lambda: connect("postgresql://u:p@db.example/prod")):
        with pytest.raises(LiveDatabaseRefused, match="during tests"):
            attempt()
    with pytest.raises(LiveDatabaseRefused):                                      # ?dbname= wins in psycopg
        connect("postgresql:///omdb_x?dbname=omarchy")
    monkeypatch.setenv("BH_DATABASE_URL", "postgresql:///omdb_test_bh_1")        # a throwaway one is fine
    assert url_for("bh").startswith("postgresql+psycopg:///omdb_test_bh_1")
    with pytest.raises(ValueError, match="throwaway"):
        drop_pg("omarchy")


@needs_pg
def test_guard_covers_raw_drivers():
    """create_engine() and psycopg.connect() straight to a real database are refused too (the strata-models
    incident went through the app's own engine code, not a fixture)."""
    name = f"realdb_{uuid.uuid4().hex[:8]}"
    adm = create_engine(admin_url(), isolation_level="AUTOCOMMIT")
    with adm.connect() as c: c.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        url = pg_url(name)
        with pytest.raises(LiveDatabaseRefused):
            create_engine(url).connect()
        with pytest.raises(LiveDatabaseRefused):
            psycopg.connect(url.replace("postgresql+psycopg", "postgresql"))
        r = subprocess.run([sys.executable, "-c", f"from sqlalchemy import create_engine; create_engine({url!r}).connect()"],
                           capture_output=True, text=True)                        # not guarded: no plugin loaded...
        assert r.returncode == 0, r.stderr                                         # ...but connect() is (below)
        r = subprocess.run([sys.executable, "-c", f"import omarchy_db; omarchy_db.connect({url!r}).connect()"],
                           capture_output=True, text=True)
        assert "LiveDatabaseRefused" in r.stderr and "during tests" in r.stderr    # OMARCHY_DB_TESTING inherited
    finally:
        with adm.connect() as c: c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        adm.dispose()


def test_live_database_is_opt_in(tmp_path):
    """Outside tests there is no default database: the shared one needs OMARCHY_DB_LIVE=1 (or dbtool --live)."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("OMARCHY_DB_TESTING", "PYTEST_CURRENT_TEST", "OMARCHY_DB_URL", "BH_DATABASE_URL", "OMARCHY_DB_LIVE")}
    code = "from omarchy_db import url_for; print(url_for('bh'))"
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode and "OMARCHY_DB_LIVE=1" in r.stderr
    r = subprocess.run([sys.executable, "-c", code], env={**env, "OMARCHY_DB_LIVE": "1"}, capture_output=True, text=True)
    assert r.stdout.strip() == "postgresql+psycopg:///omarchy?options=-csearch_path%3Dbh"
    r = subprocess.run([sys.executable, "-c", code], env={**env, "BH_DATABASE_URL": "data/bh.db"}, capture_output=True,
                       text=True, cwd=tmp_path)
    assert r.returncode == 0                                                         # SQLite is not restricted


def test_dbtool_migrate_refuses_the_shared_database_in_tests(app):
    r = dbtool(app, "migrate", "--live", check=False)
    assert r.returncode and "during tests" in r.stderr


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
            url += ("&" if "?" in url else "?") + "options=-csearch_path%3D" + Project.load(app).schema
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


def test_lint_revisions_catches_two_heads(app):
    """Two revisions written in parallel on the same parent: `upgrade head` would fail at app start."""
    from omarchy_db import lint_revisions
    dbtool(app, "revision", "-m", "initial", "--sqlite")
    assert lint_revisions(app / "migrations") == []
    first = next((app / "migrations").glob("*.py"))
    parent = first.read_text().split("revision: str = '")[1].split("'")[0]
    for rid in ("aaaa00000001", "bbbb00000002"):
        (app / "migrations" / f"x_{rid}.py").write_text(
            f"revision = '{rid}'\ndown_revision = '{parent}'\nbranch_labels = None\ndepends_on = None\n"
            "def upgrade(): pass\ndef downgrade(): pass\n")
    problems = lint_revisions(app / "migrations")
    assert len(problems) == 1 and "2 heads" in problems[0] and "aaaa00000001" in problems[0]
    r = dbtool(app, "check", "--sqlite", check=False)
    assert r.returncode == 1 and "2 heads" in r.stdout


@pytest.mark.parametrize("backend", ["sqlite", pytest.param("postgres", marks=needs_pg)])
def test_stairway_catches_a_revision_that_cannot_be_reapplied(app, backend):
    """The second revision's downgrade forgets its index. Downgrading to base still leaves an empty database (the
    first revision drops the table and the index with it), but re-applying the second revision after its own
    downgrade fails: only the stairway sees that."""
    dbtool(app, "revision", "-m", "initial", "--sqlite")
    parent = next((app / "migrations").glob("*.py")).read_text().split("revision: str = '")[1].split("'")[0]
    (app / "migrations" / "x_cccc00000003.py").write_text(
        f"from alembic import op\nrevision = 'cccc00000003'\ndown_revision = '{parent}'\nbranch_labels = None\n"
        "depends_on = None\n"
        "def upgrade(): op.create_index('ix_things_name_extra', 'things', ['name'])\n"
        "def downgrade(): pass\n")
    p = Project.load(app)
    with scratch_database(backend == "postgres") as url:
        eng = connect(url) if url.startswith("sqlite") else create_engine(url)
        problems = check_migrations(eng, p.migrations_dir, p.metadata)
        eng.dispose()
    assert not any("left tables" in x for x in problems)
    assert any(x.startswith("stairway: upgrade again to cccc00000003") and "ix_things_name_extra" in x for x in problems), problems


def test_check_loads_revisions_that_use_op_at_module_level(app):
    """`op.f()` at module level is fine during a migration; loading the files for the checks must not break on it."""
    from omarchy_db import lint_revisions
    dbtool(app, "revision", "-m", "initial", "--sqlite")
    rev = next((app / "migrations").glob("*.py"))
    rev.write_text(rev.read_text() + "\nNAME = op.f('ix_module_level')\n")
    assert lint_revisions(app / "migrations") == []
    r = dbtool(app, "check", "--sqlite", check=False)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("backend", ["sqlite", pytest.param("postgres", marks=needs_pg)])
def test_stairway_handles_branches_and_a_merge(app, backend):
    """Two sibling revisions on the same parent, joined by a merge revision: a valid history, one head."""
    dbtool(app, "revision", "-m", "initial", "--sqlite")
    parent = next((app / "migrations").glob("*.py")).read_text().split("revision: str = '")[1].split("'")[0]
    for rid, table in (("dddd00000001", "left_t"), ("eeee00000002", "right_t")):
        (app / "migrations" / f"x_{rid}.py").write_text(
            f"from alembic import op\nimport sqlalchemy as sa\nrevision = '{rid}'\ndown_revision = '{parent}'\n"
            "branch_labels = None\ndepends_on = None\n"
            f"def upgrade(): op.create_table('{table}', sa.Column('id', sa.Integer, primary_key=True))\n"
            f"def downgrade(): op.drop_table('{table}')\n")
    (app / "migrations" / "x_ffff00000003.py").write_text(
        "revision = 'ffff00000003'\ndown_revision = ('dddd00000001', 'eeee00000002')\nbranch_labels = None\n"
        "depends_on = None\ndef upgrade(): pass\ndef downgrade(): pass\n")
    from omarchy_db import lint_revisions
    assert lint_revisions(app / "migrations") == []
    p = Project.load(app)
    with scratch_database(backend == "postgres") as url:
        eng = connect(url) if url.startswith("sqlite") else create_engine(url)
        problems = check_migrations(eng, p.migrations_dir, p.metadata)
        assert set(inspect(eng).get_table_names()) >= {"left_t", "right_t", "things"}        # ends at head
        eng.dispose()
    assert not [x for x in problems if x.startswith("stairway") or "left tables" in x], problems


@needs_pg
def test_scratch_refuses_a_server_holding_the_shared_database():
    """Test and scratch databases never go on the real server: one that holds the shared database is refused."""
    from omarchy_db.scratch import RealServerRefused, _refuse_real_server
    adm = create_engine(admin_url(), isolation_level="AUTOCOMMIT")
    with adm.connect() as c: c.execute(text('CREATE DATABASE "omarchy"'))
    try:
        with pytest.raises(RealServerRefused):
            _refuse_real_server(admin_url())
    finally:
        with adm.connect() as c: c.execute(text('DROP DATABASE IF EXISTS "omarchy" WITH (FORCE)'))
        adm.dispose()


def test_private_server_when_no_admin_url():
    """Without OMARCHY_DB_TEST_ADMIN_URL a process gets its own server in a temp dir, deleted when it exits."""
    from omarchy_db import testserver
    if not testserver.available():
        pytest.skip("no PostgreSQL server binaries")
    env = {k: v for k, v in os.environ.items() if k != "OMARCHY_DB_TEST_ADMIN_URL"}
    code = ("import os; from omarchy_db.scratch import admin_url, scratch_database; u = admin_url(); "
            "d = u.split('host=')[1].replace('%2F', '/'); print(d); assert os.path.isdir(d)\n"
            "with scratch_database(True) as s: assert 'omdb_scratch' in s and d.replace('/', '%2F') in s")
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    d = r.stdout.strip()
    assert os.path.basename(d).startswith(testserver.PREFIX) and not os.path.exists(d)
