/**
 * Throwaway Postgres databases, through an admin connection on a throwaway server, never the real one:
 * OMARCHY_DB_TEST_ADMIN_URL (CI: the service container), else a private server started for the process
 * (testserver.ts). A server that holds the shared database (`omarchy`) is refused: that is the real one.
 * Same variable and rules as the Python library.
 */
import { randomBytes } from "node:crypto";
import { DATABASE } from "./config.js";
import { isThrowaway, testing } from "./guard.js";
import { testServerUrl } from "./testserver.js";

export class RealServerRefused extends Error {
  override name = "RealServerRefused";
}

const pgUrl = (u: string) => u.replace(/^postgresql\+psycopg:/, "postgresql:");

/** pg config for the throwaway admin connection (starts the private server if OMARCHY_DB_TEST_ADMIN_URL is unset). */
export function adminConfig(): any {
  return { connectionString: pgUrl(process.env.OMARCHY_DB_TEST_ADMIN_URL || testServerUrl()) };
}

/** pg config for `omarchy-db new-app` on the real server: OMARCHY_DB_ADMIN_URL, else you over the local socket.
 *  Under tests it is the throwaway server instead. */
export function liveAdminConfig(): any {
  if (testing()) return adminConfig();
  const url = process.env.OMARCHY_DB_ADMIN_URL;
  return url ? { connectionString: pgUrl(url) } : { host: "/run/postgresql", database: "postgres" };
}

export function withDatabase(cfg: any, database: string): any {
  if (!cfg.connectionString) return { ...cfg, database };
  const u = new URL(cfg.connectionString); u.pathname = "/" + database;
  return { connectionString: u.toString() };
}

const checked = new Set<string>();

async function admin<T>(fn: (q: (s: string) => Promise<any>) => Promise<T>, cfg: any = adminConfig()): Promise<T> {
  const pg = (await import("pg")).default;
  const c = new pg.Client(cfg);
  await c.connect();
  try {
    if (cfg.connectionString && !checked.has(cfg.connectionString)) {
      if ((await c.query("SELECT 1 FROM pg_database WHERE datname = $1", [DATABASE])).rowCount)
        throw new RealServerRefused(`OMARCHY_DB_TEST_ADMIN_URL points at a server that holds the shared database "${DATABASE}": ` +
          "test and scratch databases never go on the real server. Unset it (a private throwaway server is started) or point it at a throwaway one.");
      checked.add(cfg.connectionString);
    }
    return await fn((s) => c.query(s));
  } finally { await c.end(); }
}

/** A throwaway Postgres can be had: OMARCHY_DB_TEST_ADMIN_URL reachable, or server binaries for a private one. */
export async function pgAvailable(): Promise<boolean> {
  const { available } = await import("./testserver.js");
  if (!process.env.OMARCHY_DB_TEST_ADMIN_URL && !available()) return false;
  try { await admin((q) => q("SELECT 1")); return true; } catch (e) { if (e instanceof RealServerRefused) throw e; return false; }
}

export function uniqueName(prefix: string): string {
  return `${prefix}_${process.pid}_${randomBytes(4).toString("hex")}`.toLowerCase().replace(/[^a-z0-9_]/g, "_");
}

export async function createDatabase(name: string, template?: string) {
  await admin((q) => q(`CREATE DATABASE "${name}"${template ? ` TEMPLATE "${template}"` : ""}`));
  return withDatabase(adminConfig(), name);
}

export async function dropDatabase(name: string) {
  if (!isThrowaway(name)) throw new Error(`dropDatabase only drops throwaway databases (omdb_*), not "${name}"`);
  await admin((q) => q(`DROP DATABASE IF EXISTS "${name}" WITH (FORCE)`));
}

/** Role + schema for an app in the shared database (as `dbtool new-app` in the Python package): role NAME
 *  (NOLOGIN; you are a member, so you can use and own its objects), schema NAME in the shared database owned by it,
 *  no USAGE for PUBLIC, so other app roles cannot see it. Creates the shared database if it is missing. */
export async function newApp(name: string, database = DATABASE): Promise<string[]> {
  if (!/^[a-z][a-z0-9_]*$/.test(name)) throw new Error("app names: lower case letters, digits and _");
  const done: string[] = [];
  const live = liveAdminConfig();
  const pg = (await import("pg")).default;
  const a = new pg.Client(live); await a.connect();
  const q = (s: string) => a.query(s);
  try {
    if (!(await q(`SELECT 1 FROM pg_roles WHERE rolname = '${name}'`)).rowCount) { await q(`CREATE ROLE "${name}" NOLOGIN`); done.push(`created role ${name}`); }
    if (!(await q(`SELECT 1 FROM pg_database WHERE datname = '${database}'`)).rowCount) { await q(`CREATE DATABASE "${database}"`); done.push(`created database ${database}`); }
  } finally { await a.end(); }
  const c = new pg.Client(withDatabase(live, database));
  await c.connect();
  try {
    if (!(await c.query(`SELECT 1 FROM pg_namespace WHERE nspname = $1`, [name])).rowCount) {
      await c.query(`CREATE SCHEMA "${name}" AUTHORIZATION "${name}"`);
      await c.query(`REVOKE ALL ON SCHEMA "${name}" FROM PUBLIC`);
      done.push(`created schema ${database}.${name} (owner ${name}, no PUBLIC access)`);
    }
  } finally { await c.end(); }
  return done;
}
