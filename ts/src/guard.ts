/**
 * Keeping tests and stray processes off real data (same rules as the Python library's omarchy_db.guard).
 *
 * 1. Tests never open a real database. Under a test runner (vitest sets VITEST, jest JEST_WORKER_ID; also
 *    NODE_ENV=test or OMARCHY_DB_TESTING=1) the only Postgres databases that can be opened are throwaway ones:
 *    names starting `omdb_` (what freshDb and check create) and the admin database they are created through
 *    (OMARCHY_DB_TEST_ADMIN_URL). Anything else throws LiveDatabaseRefused, even with OMARCHY_DB_LIVE=1.
 *    postgresConfig()/connectPostgres() check it; importing "@omarchy/db/testing" or "@omarchy/db/guard" (e.g. as a
 *    vitest setupFile) also checks every pg.Client / pg.Pool connection in the process, so an app's own
 *    `new pg.Pool(...)` is covered too. PGlite, SQLite and Miniflare's D1 are always fine.
 * 2. A real database is opt-in. Outside tests, postgresConfig()/connectPostgres() return a real database only when
 *    the process has OMARCHY_DB_LIVE=1 (set it in the app's systemd unit or launcher), or `omarchy-db migrate --live`.
 */
export const THROWAWAY_PREFIX = "omdb_";

export class LiveDatabaseRefused extends Error {
  override name = "LiveDatabaseRefused";
}

const env = (): Record<string, string | undefined> => (typeof process === "undefined" ? {} : process.env);

export function testing(): boolean {
  const e = env();
  return e.OMARCHY_DB_TESTING === "1" || !!e.VITEST || !!e.JEST_WORKER_ID || e.NODE_ENV === "test";
}

export function liveAllowed(): boolean {
  return env().OMARCHY_DB_LIVE === "1" && !testing();
}

/** Opt this process in to real databases (omarchy-db migrate --live). No effect under tests. */
export function allowLive() {
  process.env.OMARCHY_DB_LIVE = "1";
}

export function isThrowaway(name: string | undefined): boolean {
  return !!name && name.startsWith(THROWAWAY_PREFIX);
}

/** The database a pg config ({ connectionString } or { host, database, ... }) would open, as pg resolves it. */
export function databaseOf(cfg: any): string | undefined {
  const e = env();
  if (cfg?.database) return cfg.database;
  if (cfg?.connectionString) {
    const u = new URL(cfg.connectionString.replace(/^postgres(ql)?(\+\w+)?:/, "postgresql:"));
    const db = decodeURIComponent(u.pathname.replace(/^\//, ""));     // pg ignores ?dbname=
    if (db) return db;
    return e.PGDATABASE ?? (u.username ? decodeURIComponent(u.username) : undefined) ?? e.PGUSER ?? e.USER;
  }
  return e.PGDATABASE ?? cfg?.user ?? e.PGUSER ?? e.USER;
}

function adminDatabase(): string | undefined {
  const url = env().OMARCHY_DB_TEST_ADMIN_URL;
  const name = url ? databaseOf({ connectionString: url }) : "postgres";
  return name === (env().OMARCHY_DB_DATABASE ?? "omarchy") ? undefined : name;           // the shared database is never an admin database
}

/** Throw LiveDatabaseRefused unless the Postgres database `name` may be opened by this process now. */
export function checkDatabase(name: string | undefined, where = "connect") {
  if (isThrowaway(name)) return;
  if (testing()) {
    if (name && name === adminDatabase()) return;
    throw new LiveDatabaseRefused(
      `${where}: refused Postgres database "${name}" during tests. Tests may only use throwaway ${THROWAWAY_PREFIX}* ` +
      `databases: use freshDb() from "@omarchy/db/testing" (PGlite by default).`);
  }
  if (!liveAllowed()) {
    throw new LiveDatabaseRefused(
      `${where}: "${name}" is a real database and this process has not opted in. Set OMARCHY_DB_LIVE=1 in the ` +
      `app's service environment (systemd unit, launcher), or pass --live to omarchy-db.`);
  }
}

export function checkConfig<T>(cfg: T, where = "connect"): T {
  checkDatabase(databaseOf(cfg), where);
  return cfg;
}

let guarded = false;
/** Under tests: check the database of every pg.Client (and so pg.Pool) connection in the process before it
 *  connects. Called by "@omarchy/db/testing" and "@omarchy/db/guard"; does nothing outside tests or without pg. */
export async function guardDrivers() {
  if (guarded || !testing()) return;
  let pg: any;
  try { pg = (await import("pg")).default; } catch { return; }
  const proto = pg.Client.prototype;
  if (proto.__omdbGuarded) { guarded = true; return; }
  const connect = proto.connect;
  proto.connect = function (this: any, cb?: (err: Error | null) => void) {
    try { checkDatabase(this.database, "pg.Client.connect"); }
    catch (e) { if (cb) { process.nextTick(() => cb(e as Error)); return; } return Promise.reject(e); }
    return connect.call(this, cb);
  };
  proto.__omdbGuarded = true;
  guarded = true;
}

await guardDrivers();     // importing @omarchy/db (or /testing, /guard) under a test runner switches it on
