# database

Tooling for running many small apps against **one shared PostgreSQL database, with a schema per app**: each app owns its schema (through a role of its own), and anything can join across apps when it needs to. Tests run on SQLite (Python) or PGlite (TypeScript) by default and on a real Postgres on demand.

- **Python** ([python/](python/), package `omarchy-db`): `connect(app)`, SQLAlchemy 2.0 dataclass models (`make_base()`), Alembic migrations with no per-app boilerplate, `dbtool` (`init`, `new-app`, `revision`, `migrate`, `check`, `current`) and a pytest plugin (`db_session`, `db_engine`, `db_url`; `pytest --pg` for Postgres). Example: [examples/notes](examples/notes).
- **TypeScript** ([ts/](ts/), package `@omarchy/db`): Drizzle schemas (`pgSchema(app)` for Postgres, `sqliteTable` for Cloudflare D1), generated migrations, `omarchy-db` (`generate`, `check`, `migrate`, `new-app`) and vitest helpers for PGlite, Postgres, SQLite and D1 (Miniflare). Examples: [examples/ts-notes](examples/ts-notes), [examples/ts-worker](examples/ts-worker).

How an app adopts it: [docs/ADOPTING.md](docs/ADOPTING.md).

**Conventions.** The shared database is `omarchy` unless `OMARCHY_DB_DATABASE` says otherwise; an app's schema and role are its name. By default apps connect over the local Unix socket with peer authentication; `OMARCHY_DB_URL` or `<APP>_DATABASE_URL` point elsewhere. The login that runs `dbtool new-app` needs CREATEDB and CREATEROLE, and the server should set `createrole_self_grant = 'set, inherit'` so that login can act as the app roles it creates.

**Tests:** `pytest python` and the examples (`pytest`, `pytest --pg`, `dbtool check`); `npm test` in `ts/` and the TS examples with `OMARCHY_DB_TEST_ENGINE=pglite|postgres|sqlite|d1`. CI runs all of them against a PostgreSQL 18 service container.

Licence: MIT.
