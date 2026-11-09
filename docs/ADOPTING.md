# Adopting the database (Python apps)

How an app gets **its own schema in one shared PostgreSQL database** (default name `omarchy`, set `OMARCHY_DB_DATABASE`), so any app can join any other app's tables later (e.g. `notes.notes` with `photos.albums`), with its schema written as typed Python records, migrations generated from them, and tests on SQLite (fast, default) and Postgres. Working example: [examples/notes](../examples/notes). Library: [python/](../python) (`omarchy_db`). TypeScript (Drizzle) follows later.

**Where things live:** each app's **models and migrations live in the app's own repo**, next to its code. This repo provides the libraries, `dbtool` / `omarchy-db` and the test fixtures; running the server and backing it up is up to you.

## 1. Install
```
uv add "omarchy-db @ git+https://github.com/jburnhams/database#subdirectory=python"
```
(or `pip install -e path/to/database/python` for local work).

## 2. Models: typed records
```python
# myapp/models.py
from datetime import datetime
from sqlalchemy import ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from omarchy_db import make_base

Base = make_base()                       # SQLAlchemy 2.0 declarative + dataclasses, stable constraint names

class Face(Base):
    __tablename__ = "faces"
    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    photo: Mapped[str] = mapped_column(index=True)
    child: Mapped[str | None] = mapped_column(default=None)
    decided_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.current_timestamp(), init=False)
```
`Face(photo="x.jpg")` works like a dataclass. Optional fields are `X | None`; `init=False` for database-filled ones.
**Rules for portable models** (they must run on SQLite and Postgres):
- Use SQLAlchemy's generic types (`str`, `int`, `float`, `bool`, `datetime`, `date`, `Decimal`, `bytes`, `sa.JSON`), not dialect types (`JSONB`, `ARRAY`, `INET`).
- A new NOT NULL column on an existing table needs a `server_default` (`server_default=sa.false()`, `"red"`, `func.current_timestamp()`), so the migration can fill existing rows.
- If the app really needs Postgres features (JSONB queries, full-text, PostGIS), say `engine = "postgres"` in its config (below): `dbtool` and the tests then use Postgres only.

## 3. Config
```
cd myapp && dbtool init --app myapp --models myapp.models:Base
```
adds to `pyproject.toml`:
```toml
[tool.omarchy-db]
app = "myapp"                 # owner role name (lower case, digits, _)
# schema = "myapp"            # its schema in the shared database (default: the app name)
models = "myapp.models:Base"
migrations = "migrations"     # Alembic revisions, committed with the app
# engine = "postgres"         # only for Postgres-only apps
```
**Which database:** (a real one only with `OMARCHY_DB_LIVE=1`, see [Safety](#safety-real-data-is-opt-in-tests-never-touch-it)) `OMARCHY_DB_URL`, else `MYAPP_DATABASE_URL`, else `postgresql+psycopg:///omarchy?options=-csearch_path%3Dmyapp` (the shared database, `OMARCHY_DB_DATABASE` or `omarchy`, over the local socket with peer auth, no password; the search_path makes the models' unqualified table names resolve to the app's schema). Any of them may be a SQLite path (`data/app.db`); SQLite has no schemas, so nothing changes there. **Never one database per app**: Postgres cannot join across databases.

**Other apps' data:** read it with qualified names (`select ... from photos.albums`); a login that is a member of every app role (the one that ran `dbtool new-app`) can. Models stay unqualified (no `schema=` on tables), which keeps them portable to SQLite.

## 4. Migrations
```
dbtool revision -m "initial"      # autogenerate from the models; review the file, commit it
dbtool check                      # one head; from empty to head, compare with the models, downgrade, stairway: SQLite AND Postgres
dbtool migrate --live             # apply to the app's real database (or --url ...)
dbtool current --live
```
- `revision` migrates a **scratch** database to head and compares it with the models (Alembic autogenerate), so it never touches real data, and it writes nothing when there is no change. It uses a scratch Postgres when the server is reachable: Alembic writes defaults in the scratch database's dialect, and Postgres's form (`false`) works on SQLite too, while SQLite's (`0`) breaks Postgres (found while building this). Revisions always use batch ops, so the same file runs on both engines.
- **Review every generated file.** Autogenerate sees a **rename as drop + add** (data lost): edit it into `batch_op.alter_column(..., new_column_name=...)`. Data migrations (filling a new column from old ones) are written by hand into the revision (`op.execute(...)`).
- On SQLite, migrations run with foreign-key enforcement switched off (batch mode recreates tables by copy, drop and rename, which would trip references from other tables) and `PRAGMA foreign_key_check` must be clean before the commit, otherwise the whole migration rolls back.
- Changed a model and forgot the revision? `dbtool check` fails, and so does the app's `test_schema_matches_models` (below).
- What `dbtool check` (and the standard `test_migrations`) checks, on an empty scratch database:
  - **one head and one base**: two branches that each added a revision on the same parent leave two heads, and `upgrade head` would fail at app start. Fix by pointing the newer revision's `down_revision` at the other one.
  - upgrade to head, then **no drift** from the models;
  - **downgrade to base** leaves no tables;
  - **stairway**: each revision in turn goes up, down one step and up again, which catches a downgrade that forgets an index, type or sequence (it only shows when that revision is re-applied).
- Write revision ids the way `dbtool revision` does (random hex); hand-picked ids like `0001` collide when two branches pick the same one.

**At app start** (only in the long-running service, never on every connect or in CLI subcommands, so a stray process cannot migrate the schema under the running service), instead of running `dbtool migrate` by hand:
```python
from omarchy_db import Project, connect
engine = connect("myapp")
Project.load(__file__).upgrade(engine)          # no-op when already at head
```
On Postgres it holds an advisory lock (two processes starting together do not both migrate), sets `search_path` to the app's schema (creating it if missing), so the tables and Alembic's `alembic_version` land in that schema, and runs as the app's owner role, so the role owns them.

## 5. Schema on the shared server (once)
```
dbtool new-app myapp       # role myapp (NOLOGIN, you are a member), schema myapp in the shared database owned by it, no USAGE for PUBLIC
dbtool migrate --live
```
Other app roles cannot see the schema (no USAGE); you (a member of every app role) can read and join all of them.
Your login needs CREATEDB and CREATEROLE (not superuser). Roles it creates are granted back to it with SET and INHERIT if the server sets `createrole_self_grant = 'set, inherit'`, which migrations as the app role and the test fixtures rely on. Back up per schema (`pg_dump --schema=myapp`) so each app restores on its own.

## 6. Tests
Installing `omarchy-db` registers a pytest plugin. Fixtures:

| Fixture | Gives |
|---|---|
| `db_session` | a SQLAlchemy `Session` on a fresh, migrated database, per test |
| `db_engine` | the `Engine` behind it |
| `db_url` | a URL to a fresh migrated database, for code that builds its own engine (`connect(db_url)`) |
| `db_env` | points the app's **own** `connect("myapp")` at a fresh migrated test database (sets `OMARCHY_DB_URL` and `MYAPP_DATABASE_URL`), for app code that connects by itself (a CLI, a store) |
| `db_backend` | `"sqlite"` or `"postgres"` |
| `db_project` | the app's `Project` |

```python
def test_decide(db_session):
    db_session.add(Face(photo="a.jpg")); db_session.commit()
    ...
```
**Standard tests, in every app.** One line, in any test module of the database tests (e.g. `tests/unit/test_database.py`):
```python
from omarchy_db.testing import test_migrations, test_schema_matches_models  # noqa: F401
```
pytest collects imported tests like local ones: `test_schema_matches_models` (the migrated test database matches the models) and `test_migrations` (what `dbtool check` does, on the backend the run uses). Don't write your own copies.

**Never build the test schema with `Base.metadata.create_all()`**: the tests then pass while the migrations are broken or missing. Use the fixtures (they migrate), or `db_url` for code that makes its own engine.
- `pytest`: SQLite. Migrations run **once** into a template; each test gets an in-memory copy (the example app: 7 tests in 0.1 s). Foreign keys are on.
- `pytest --pg` (or `OMARCHY_DB_TEST=postgres`): each test gets `CREATE DATABASE ... TEMPLATE` from a migrated template on the real server, dropped afterwards (7 tests in about 3 s). Needs CREATEDB; the admin connection is `OMARCHY_DB_TEST_ADMIN_URL` (default: you over the socket).
- App code that calls `connect("myapp")` itself: use `db_env`, or make it apply to every test in `conftest.py`:
  ```python
  @pytest.fixture(autouse=True)
  def _test_db(db_env): ...
  ```
  Never write your own "unset the URL" or "block the network" guard: the library's guard (below) already refuses the real database, and a network block does not cover the local Unix socket.
- Run both before merging. **CI: call the reusable workflow** instead of copying a Postgres service setup. It runs `dbtool check`, then the given tests on SQLite (portable apps) and on a throwaway Postgres 18 with pgvector (`--pg`):
```yaml
# .github/workflows/database.yml
name: Database
on: { push: { branches: [main] }, pull_request: }
jobs:
  database:
    uses: jburnhams/database/.github/workflows/app-db.yml@main
    with:
      install: pip install -r requirements-test.txt   # the app's test deps; omarchy-db itself is installed by the workflow
      tests: tests/unit/db                            # the tests that touch the database
      # pythonpath: src   directory: .   python-version: "3.13"   pytest-args: "-o addopts="
```

## Safety: real data is opt-in, tests never touch it
Two rules, in both libraries (`omarchy_db.guard`, `@omarchy/db` `guard.ts`):

1. **Tests never open a real database.** Under a test run (pytest with `omarchy-db` installed; vitest, jest, `NODE_ENV=test`; or `OMARCHY_DB_TESTING=1`, which the pytest plugin sets so subprocesses inherit it) the only Postgres databases that can be opened are throwaway ones, named `omdb_*` (what the fixtures, `freshDb`, `scratch_database()` and `check` create), plus the admin database they are created through (`OMARCHY_DB_TEST_ADMIN_URL`, never the shared database). Anything else raises `LiveDatabaseRefused`, **even with `OMARCHY_DB_LIVE=1` or a URL in the environment**. It is enforced on every connection in the process, not only `connect()`: the pytest plugin wraps `psycopg.connect` / `psycopg2.connect` before `conftest.py` and the app are imported; importing `@omarchy/db` or `@omarchy/db/testing` under a test runner wraps `pg.Client.prototype.connect` (add `"@omarchy/db/guard"` to vitest `setupFiles` if your tests import neither). PGlite, SQLite and D1 are always fine. Test-only drops (`drop_pg`, `dropDatabase`) refuse anything that is not `omdb_*`.
2. **A real database is opt-in.** Outside tests, `connect(app)`, `url_for(app)`, `connect(url)` and `postgresConfig(app)` / `connectPostgres(app)` refuse a Postgres database that is not `omdb_*` unless the process has **`OMARCHY_DB_LIVE=1`**. Set it where the app is deployed (its systemd unit: `Environment=OMARCHY_DB_LIVE=1`, or its launcher), **not** in your shell profile, so scripts, REPLs and agents started from a shell do not get the real database by default. `dbtool migrate|current` and `omarchy-db migrate` take `--live`. SQLite paths are not restricted.

Upgrading an app to a library version with these rules: add `OMARCHY_DB_LIVE=1` to its service first, then upgrade (`uv lock --upgrade-package omarchy-db` / `npm update @omarchy/db`), then drop any home-made test guards.

## Moving an existing SQLite app
1. Write the models to match the existing tables (or what they should become), `dbtool revision -m initial`, `dbtool check`.
2. `dbtool new-app`, `dbtool migrate`. (An app with a Postgres database of its own: copy it into its schema, e.g. a scratch copy with `public` renamed to the app's schema, `pg_dump --schema` of it restored in one transaction, then compare every table.)
3. Copy the data in one transaction and verify it before committing (row counts and a hash of every row per table; roll back on any mismatch).
4. Point the app at it (`MYAPP_DATABASE_URL` or the default, with `OMARCHY_DB_LIVE=1` in its service), keep the SQLite file read-only for 30 days.

# TypeScript apps (`@omarchy/db`, Drizzle)

Library: [ts/](../ts). Examples: [examples/ts-notes](../examples/ts-notes) (an app on the shared Postgres) and [examples/ts-worker](../examples/ts-worker) (a Cloudflare Worker on D1). Same ideas as the Python side: the schema is written in code, migrations are generated from it, reviewed and committed, a check fails on drift, and each test gets a fresh migrated database.

**One dialect per app.** Drizzle's table types are per dialect, so an app picks one:
- **`postgresql`** (apps on the shared server): tables in the app's schema of the shared database via `pgSchema("<app>")`. Tests run on **PGlite** (real Postgres in-process: same SQL, no server, migrated once then cloned per test) or on the real server (`OMARCHY_DB_TEST_ENGINE=postgres`).
- **`sqlite`** (Workers on D1): `sqliteTable`. Tests run on **SQLite** (better-sqlite3 in memory, migrated once then copied per test) or on **D1** through Miniflare (`OMARCHY_DB_TEST_ENGINE=d1`).

(Unlike Python, Postgres apps are not tested on SQLite: PGlite gives the speed without a second dialect to keep in step.)

## Set up
```
npm install "@omarchy/db@file:path/to/database/ts" drizzle-orm pg   # a clone of this repo (the package is in ts/); add the test drivers as dev deps
```
`package.json`:
```json
"omarchy-db": { "app": "myapp", "dialect": "postgresql", "schemaFile": "src/schema.ts", "migrations": "migrations" }
```
`src/schema.ts`:
```ts
import { integer, pgSchema, text } from "drizzle-orm/pg-core";
export const app = pgSchema("myapp");
export const faces = app.table("faces", { id: integer().primaryKey().generatedByDefaultAsIdentity(), photo: text().notNull() });
```

## Migrations
```
npx omarchy-db generate -m "add faces"    # the next NNNN_name.sql (+ meta/ snapshot) if the schema changed; review, commit
npx omarchy-db check                      # schema vs migrations, and migrations from empty on every engine of the dialect
npx omarchy-db new-app                    # postgresql: role + schema in the shared database (as dbtool new-app)
npx omarchy-db migrate --live             # postgresql: apply pending migrations, as the app role, in its schema
```
At start (the service has `OMARCHY_DB_LIVE=1`): `const { db } = await connectPostgres("myapp", schema, { migrate: "migrations" })`. D1 in production: `wrangler d1 migrations apply <db> --remote` with `migrations_dir = "migrations"` (same files); in a Worker, `import { connectD1 } from "@omarchy/db/d1"`.
- As with Alembic, **a rename shows as drop + add**: edit the generated SQL.
- Postgres migrations commit **one file at a time** (an enum value added in one file can be used in the next), under a session advisory lock so two processes never migrate at once.
- Export enums (`pgEnum` / `app.enum`) from the schema file like tables; they are part of the schema.
- **SQLite table rebuilds** (some ALTERs make drizzle-kit copy a table to `__new_x`, drop and rename): on SQLite, `migrateSqlite` switches foreign keys off around the transaction and runs `PRAGMA foreign_key_check` before COMMIT. **D1 cannot switch foreign keys off**, so a rebuild of a table that other tables' rows reference is unsafe there (with `ON DELETE CASCADE` / `SET NULL` the drop would silently delete or rewrite the child rows). `migrateD1` therefore refuses such a migration before running anything when any referencing table has rows. Avoid it on D1: add a new table and copy, or rebuild the child tables in the same migration.

## Tests (vitest)
```ts
import { freshDb, dropTemplates, type TestDb } from "@omarchy/db/testing";
import * as schema from "../src/schema";
let t: TestDb;
beforeEach(async () => { t = await freshDb(schema); });
afterEach(() => t.close());
afterAll(dropTemplates);
```
Importing `@omarchy/db/testing` switches on the guard: any `pg` connection to a non-`omdb_*` database fails the test. Run `npx vitest run` (default engine) and with `OMARCHY_DB_TEST_ENGINE=postgres` or `=d1`; CI does all four (`.github/workflows/typescript.yml`). Give vitest `testTimeout: 60_000` for the server runs.
