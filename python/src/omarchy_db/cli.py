"""dbtool: the app-facing command line (run from anywhere inside the app's repo).

  dbtool init --app NAME --models pkg.models:Base [--engine portable|postgres]
                              add [tool.omarchy-db] to pyproject.toml and create migrations/
  dbtool new-app [NAME]       on the shared server: owner role NAME (NOLOGIN, you are a member) and schema NAME in
                              the shared database, owned by it, no USAGE for PUBLIC (other app roles cannot see it).
                              No sudo (your role has CREATEDB CREATEROLE). --database for another database (tests)
  dbtool revision -m MSG      autogenerate a migration: a scratch database is migrated to head and compared with
                              the models; nothing is written if there is no change. --empty for a hand-written one
  dbtool migrate [--url URL]  upgrade the app's schema (default URL: config.url_for(app)) to head (or --to REV)
  dbtool check                on a scratch database: migrate from empty, compare with the models, downgrade to
                              base, migrate again. Exit 1 on any problem (models changed without a revision, ...)
  dbtool current [--url URL]  revision the database is at
Scratch databases: `revision` uses Postgres when a server is reachable (OMARCHY_DB_TEST_ADMIN_URL), because
Alembic renders defaults in the scratch database's dialect and Postgres's form (e.g. `false`) also works on SQLite,
while SQLite's (`0`) breaks Postgres; --sqlite forces SQLite. `check` runs on SQLite and, when reachable, Postgres
(engine = "postgres" projects: Postgres only).
"""
import argparse
import re
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

from . import migrations as m
from .config import DATABASE, normalise_url, url_for
from .engine import connect
from .project import Project
from .scratch import admin_url, pg_available, scratch_database


def _scratch_engine(url):
    return connect(url) if url.startswith("sqlite") else create_engine(url)


def cmd_init(a):
    pp = Path.cwd() / "pyproject.toml"
    body = pp.read_text() if pp.exists() else f'[project]\nname = "{a.app}"\nversion = "0.1.0"\n'
    if "[tool.omarchy-db]" in body:
        sys.exit(f"{pp} already has [tool.omarchy-db]")
    body = body.rstrip("\n") + f'\n\n[tool.omarchy-db]\napp = "{a.app}"\nmodels = "{a.models}"\nmigrations = "migrations"\n' \
           + (f'engine = "{a.engine}"\n' if a.engine != "portable" else "")
    pp.write_text(body)
    (Path.cwd() / "migrations").mkdir(exist_ok=True)
    print(f"wrote [tool.omarchy-db] to {pp}; migrations/ ready. Next: dbtool revision -m 'initial'")


def cmd_new_app(a):
    try: p = Project.load()
    except FileNotFoundError: p = None
    name = a.name or (p.app if p else None)
    schema = a.schema or (p.schema if p and not a.name else name)
    if not name or not re.fullmatch(r"[a-z][a-z0-9_]*", name) or not re.fullmatch(r"[a-z][a-z0-9_]*", schema):
        sys.exit("app and schema names: lower case letters, digits and _ (they are role and schema names)")
    adm = create_engine(admin_url(), isolation_level="AUTOCOMMIT")
    with adm.connect() as c:
        if not c.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :n"), {"n": name}).scalar():
            c.execute(text(f'CREATE ROLE "{name}" NOLOGIN')); print(f"created role {name}")
        if not c.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": a.database}).scalar():
            c.execute(text(f'CREATE DATABASE "{a.database}"')); print(f"created database {a.database}")
    adm.dispose()
    eng = create_engine(adm.url.set(database=a.database), isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        if not c.execute(text("SELECT 1 FROM pg_namespace WHERE nspname = :s"), {"s": schema}).scalar():
            c.execute(text(f'CREATE SCHEMA "{schema}" AUTHORIZATION "{name}"'))
            c.execute(text(f'REVOKE ALL ON SCHEMA "{schema}" FROM PUBLIC'))
            print(f"created schema {a.database}.{schema} (owner {name}, no PUBLIC access)")
    eng.dispose()
    print(f"{name}: schema {schema} in {a.database}; URL {url_for(name, schema) if a.database == DATABASE else '(--database given)'}"
          "  (dbtool migrate creates the tables)")


def cmd_revision(a):
    p = Project.load()
    pg = p.postgres_only or a.pg or (not a.sqlite and pg_available())
    print(f"autogenerating against a scratch {'Postgres' if pg else 'SQLite'} database")
    with scratch_database(pg) as url:
        eng = _scratch_engine(url)
        m.upgrade(eng, p.migrations_dir, p.metadata)
        path = m.revision(eng, p.migrations_dir, p.metadata, a.message, autogenerate=not a.empty)
        eng.dispose()
    if path:
        print(f"wrote {Path(path).relative_to(p.root)}: review it, then commit it")
    else:
        print("no changes: the models match the migrations")


def cmd_migrate(a):
    p = Project.load()
    eng = connect(normalise_url(a.url) if a.url else p.url())
    schema = p.schema if eng.dialect.name == "postgresql" else None
    before = m.current(eng, schema)
    p.upgrade(eng, a.to)
    print(f"{p.app}: {before or 'empty'} -> {m.current(eng, schema)}")


def cmd_check(a):
    p = Project.load()
    engines = ["postgres"] if p.postgres_only or a.pg else ["sqlite"] + (["postgres"] if not a.sqlite and pg_available() else [])
    failed = False
    for e in engines:
        with scratch_database(e == "postgres") as url:
            eng = _scratch_engine(url)
            try: problems = m.check_migrations(eng, p.migrations_dir, p.metadata)
            except Exception as ex: problems = [f"{type(ex).__name__}: {ex}".split("\n")[0]]
            eng.dispose()
        for x in problems: print(f"FAIL [{e}] {x}")
        failed |= bool(problems)
        if not problems: print(f"ok [{e}]: {p.app} migrations run from empty, match the models, and downgrade cleanly")
    if "postgres" not in engines and not p.postgres_only: print("note: Postgres not reachable, checked SQLite only")
    if failed: sys.exit(1)


def cmd_current(a):
    p = Project.load()
    print(m.current(connect(normalise_url(a.url) if a.url else p.url()), p.schema) or "empty")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dbtool", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init"); s.add_argument("--app", required=True); s.add_argument("--models", required=True)
    s.add_argument("--engine", choices=["portable", "postgres"], default="portable"); s.set_defaults(f=cmd_init)
    s = sub.add_parser("new-app"); s.add_argument("name", nargs="?"); s.add_argument("--schema")
    s.add_argument("--database", default=DATABASE); s.set_defaults(f=cmd_new_app)
    s = sub.add_parser("revision"); s.add_argument("-m", "--message", required=True); s.add_argument("--empty", action="store_true")
    s.add_argument("--pg", action="store_true"); s.add_argument("--sqlite", action="store_true"); s.set_defaults(f=cmd_revision)
    s = sub.add_parser("migrate"); s.add_argument("--url"); s.add_argument("--to", default="head"); s.set_defaults(f=cmd_migrate)
    s = sub.add_parser("check"); s.add_argument("--pg", action="store_true", help="Postgres only")
    s.add_argument("--sqlite", action="store_true", help="SQLite only"); s.set_defaults(f=cmd_check)
    s = sub.add_parser("current"); s.add_argument("--url"); s.set_defaults(f=cmd_current)
    a = ap.parse_args(argv)
    a.f(a)


if __name__ == "__main__":
    main()
