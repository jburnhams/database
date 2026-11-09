"""Alembic without per-app boilerplate: one shared env.py (omarchy_db/_alembic), the app's revisions in its own
migrations/ folder, the connection and MetaData passed in from here.

upgrade() is safe to call at every app start: on Postgres it takes an advisory lock so two processes starting at
once do not both migrate, sets search_path to the app's schema (creating the schema if needed), so the tables and
Alembic's version table land there whatever the connection's search_path, and runs as the app's owner role (if it
exists and you are a member) so the app role owns what it creates.
"""
import os
import zlib
from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, inspect, text
from sqlalchemy.engine import Connection, Engine

ENV_DIR = Path(__file__).parent / "_alembic"


def make_config(migrations_dir, metadata: MetaData, connection: Connection, **attributes) -> Config:
    migrations_dir = Path(migrations_dir)
    migrations_dir.mkdir(parents=True, exist_ok=True)
    cfg = Config()
    cfg.set_main_option("script_location", str(ENV_DIR))
    cfg.set_main_option("version_locations", str(migrations_dir))
    cfg.set_main_option("path_separator", "os")
    cfg.set_main_option("file_template", "%%(year)d%%(month).2d%%(day).2d_%%(rev)s_%%(slug)s")
    cfg.set_main_option("truncate_slug_length", "40")
    cfg.attributes.update(connection=connection, metadata=metadata, **attributes)
    return cfg


def _prepare(conn: Connection, owner_role: str | None, schema: str | None = None):
    if conn.dialect.name != "postgresql":
        return
    key = zlib.crc32(f"omarchy_db:{conn.engine.url.database}:{schema}".encode())
    conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})
    role = owner_role if owner_role and conn.execute(
        text("SELECT pg_has_role(current_user, :r, 'SET') FROM pg_roles WHERE rolname = :r"), {"r": owner_role}).scalar() else None
    if schema:
        if not conn.execute(text("SELECT 1 FROM pg_namespace WHERE nspname = :s"), {"s": schema}).scalar():
            conn.execute(text(f'CREATE SCHEMA "{schema}"' + (f' AUTHORIZATION "{role}"' if role else "")))
            conn.execute(text(f'REVOKE ALL ON SCHEMA "{schema}" FROM PUBLIC'))
        conn.execute(text(f'SET LOCAL search_path TO "{schema}"'))
    if role:
        conn.execute(text(f'SET LOCAL ROLE "{role}"'))


def _run(engine: Engine, owner_role: str | None, fn, schema: str | None = None):
    """Run fn(conn) in one transaction. On SQLite, foreign-key enforcement is switched off around it (the pragma
    only takes effect outside a transaction): batch mode recreates a table by copy, drop and rename, and dropping a
    table that other rows reference would fail or cascade. Before committing, PRAGMA foreign_key_check must come
    back empty, otherwise everything is rolled back; enforcement is switched back on either way."""
    with engine.connect() as conn:
        sqlite = conn.dialect.name == "sqlite"
        if sqlite:
            conn.exec_driver_sql("PRAGMA foreign_keys = OFF"); conn.commit()
        try:
            with conn.begin():
                _prepare(conn, owner_role, schema)
                fn(conn)
                if sqlite and (bad := conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()):
                    raise RuntimeError(f"migration left foreign key violations (table, rowid, parent, fk): {bad[:10]}; rolled back")
        finally:
            if sqlite:
                conn.exec_driver_sql("PRAGMA foreign_keys = ON"); conn.commit()


def upgrade(engine: Engine, migrations_dir, metadata: MetaData, revision: str = "head", owner_role: str | None = None,
            schema: str | None = None):
    _run(engine, owner_role, lambda conn: command.upgrade(make_config(migrations_dir, metadata, conn), revision), schema)


def downgrade(engine: Engine, migrations_dir, metadata: MetaData, revision: str = "base", owner_role: str | None = None,
              schema: str | None = None):
    _run(engine, owner_role, lambda conn: command.downgrade(make_config(migrations_dir, metadata, conn), revision), schema)


def current(engine: Engine, schema: str | None = None) -> str | None:
    with engine.connect() as conn:
        if schema and conn.dialect.name == "postgresql":
            conn.execute(text(f'SET search_path TO "{schema}"'))
        return MigrationContext.configure(conn).get_current_revision()


def drift(engine: Engine, metadata: MetaData) -> list:
    """Differences between the database's schema and the models (empty = in sync). Alembic's own comparison."""
    with engine.connect() as conn:
        mc = MigrationContext.configure(conn, opts={
            "compare_type": True, "compare_server_default": conn.dialect.name != "sqlite",   # see _alembic/env.py "render_as_batch": True,
            "include_name": lambda name, type_, _: not (type_ == "table" and name == "alembic_version")})
        return compare_metadata(mc, metadata)


def lint_revisions(migrations_dir) -> list[str]:
    """Checks on the revision files alone, no database: exactly one base and one head. Two branches that each added
    a revision on the same parent (two PRs written in parallel) leave two heads, and `upgrade head` then fails at app
    start; this catches it in the tests instead."""
    try:
        script = ScriptDirectory.from_config(make_config(migrations_dir, MetaData(), None))
        heads, bases = script.get_heads(), script.get_bases()
    except Exception as e:
        return [f"revision files do not form a chain: {type(e).__name__}: {str(e).splitlines()[0]}"]
    problems = []
    if len(heads) > 1:
        problems.append(f"{len(heads)} heads ({', '.join(sorted(heads))}): revisions written in parallel on the same "
                        "parent; set the newer one's down_revision to the other (or write a merge revision)")
    if len(bases) > 1:
        problems.append(f"{len(bases)} base revisions ({', '.join(sorted(bases))}): only the first may have down_revision = None")
    return problems


def stairway(engine: Engine, migrations_dir, metadata: MetaData) -> list[str]:
    """From base, one revision at a time: upgrade to it, downgrade one step, upgrade again. Catches a downgrade that
    only works from head, or a revision that cannot be re-applied after its own downgrade (a leftover index, type or
    sequence). Ends at head. Returns problems (empty = all good); stops at the first broken step."""
    script = ScriptDirectory.from_config(make_config(migrations_dir, metadata, None))
    for rev in reversed(list(script.walk_revisions())):
        name = f"{rev.revision} ({(rev.doc or '').splitlines()[0] if rev.doc else 'no message'})"
        for step, fn, target in (("upgrade", upgrade, rev.revision), ("downgrade one step from", downgrade, "-1"),
                                 ("upgrade again to", upgrade, rev.revision)):
            try: fn(engine, migrations_dir, metadata, target)
            except Exception as e:
                return [f"stairway: {step} {name} failed: {type(e).__name__}: {str(e).splitlines()[0]}"]
    return []


def check_migrations(engine: Engine, migrations_dir, metadata: MetaData) -> list[str]:
    """On an EMPTY database: one head and one base; upgrade from nothing to head and compare with the models;
    downgrade to base (every downgrade must run and leave no tables); then the stairway (each revision up, down one
    step, up again), which ends at head. Returns a list of problems (empty = all good)."""
    if problems := lint_revisions(migrations_dir):
        return problems
    if [t for t in inspect(engine).get_table_names() if t != "alembic_version"]:
        return ["check_migrations needs an empty database"]
    upgrade(engine, migrations_dir, metadata)
    if d := drift(engine, metadata):
        problems.append(f"after upgrade head the schema differs from the models (run dbtool revision): {d}")
    downgrade(engine, migrations_dir, metadata, "base")
    left = [t for t in inspect(engine).get_table_names() if t != "alembic_version"]
    if left:
        problems.append(f"downgrade to base left tables: {left}")
        return problems                       # upgrading again would only fail on the leftovers
    return problems + stairway(engine, migrations_dir, metadata)


def revision(engine: Engine, migrations_dir, metadata: MetaData, message: str, autogenerate: bool = True):
    """Write a new revision file. With autogenerate, `engine` must be a database at head (dbtool uses a scratch one)
    and nothing is written when there is no change. Returns the path written, or None."""
    written = []

    def directives(context, rev, dirs):
        script = dirs[0]
        if autogenerate and script.upgrade_ops.is_empty():
            dirs[:] = []

    with engine.begin() as conn:
        cfg = make_config(migrations_dir, metadata, conn, process_revision_directives=directives)
        scripts = command.revision(cfg, message=message, autogenerate=autogenerate)
    for s in (scripts if isinstance(scripts, list) else [scripts]):
        if s is not None: written.append(s.path)
    return written[0] if written else None
