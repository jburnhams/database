/**
 * Drizzle databases on each engine. The drivers are optional peer dependencies: install the ones the app uses
 * (pg for the shared Postgres, @electric-sql/pglite / better-sqlite3 / miniflare for tests).
 *
 *   const { db, close } = await connectPostgres("notes", schema, { migrate: "migrations" });   // the shared Postgres
 *   const db = connectD1(env.DB, schema);                                                   // in a Worker
 */
import { postgresConfig } from "./config.js";
import { migrateD1, migratePglite, migratePostgres, migrateSqlite } from "./migrate.js";

type Schema = Record<string, unknown>;

/** The shared Postgres database (or OMARCHY_DB_URL / <APP>_DATABASE_URL). The app's tables are in its schema
 *  (pgSchema(app) in the schema file), so queries are qualified and other apps' schemas can be joined.
 *  With `migrate`, pending migrations from that folder are applied first, as the app's owner role. */
export async function connectPostgres<S extends Schema>(app: string, schema: S, opts: { migrate?: string; max?: number } = {}) {
  const pg = (await import("pg")).default;
  const { drizzle } = await import("drizzle-orm/node-postgres");
  const pool = new pg.Pool({ ...postgresConfig(app), max: opts.max ?? 5 });
  if (opts.migrate) await migratePostgres(pool, opts.migrate, { schema: app, ownerRole: app });
  return { db: drizzle(pool, { schema }), pool, close: () => pool.end() };
}

/** In-process Postgres (PGlite), empty unless `migrate` is given. */
export async function connectPglite<S extends Schema>(schema: S, opts: { app: string; migrate?: string; pglite?: any }) {
  const { PGlite } = await import("@electric-sql/pglite");
  const { drizzle } = await import("drizzle-orm/pglite");
  const client = opts.pglite ?? new PGlite();
  if (opts.migrate) await migratePglite(client, opts.migrate, { schema: opts.app });
  return { db: drizzle(client, { schema }), client, close: () => client.close() };
}

/** SQLite file or ":memory:" via better-sqlite3, foreign keys on. */
export async function connectSqlite<S extends Schema>(path: string, schema: S, opts: { migrate?: string; database?: any } = {}) {
  const Database = (await import("better-sqlite3")).default;
  const { drizzle } = await import("drizzle-orm/better-sqlite3");
  const client = opts.database ?? new Database(path);
  client.pragma("foreign_keys = ON");
  if (path !== ":memory:" && !opts.database) client.pragma("journal_mode = WAL");
  if (opts.migrate) migrateSqlite(client, opts.migrate);
  return { db: drizzle(client, { schema }), client, close: () => client.close() };
}

/** A D1 binding (Worker env.DB). */
import { drizzle as d1Drizzle } from "drizzle-orm/d1";
export function connectD1<S extends Schema>(binding: D1Database, schema: S) {
  return d1Drizzle(binding, { schema });
}
export { migrateD1 };
