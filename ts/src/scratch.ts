/**
 * Throwaway Postgres databases on the server, through an admin connection: OMARCHY_DB_TEST_ADMIN_URL
 * (default: you over the socket, database postgres; needs CREATEDB). Same variable as the Python library.
 */
import { randomBytes } from "node:crypto";
import { DATABASE } from "./config.js";

export function adminConfig(): any {
  const url = process.env.OMARCHY_DB_TEST_ADMIN_URL;
  return url ? { connectionString: url.replace(/^postgresql\+psycopg:/, "postgresql:") } : { host: "/run/postgresql", database: "postgres" };
}

export function withDatabase(cfg: any, database: string): any {
  if (!cfg.connectionString) return { ...cfg, database };
  const u = new URL(cfg.connectionString); u.pathname = "/" + database;
  return { connectionString: u.toString() };
}

async function admin<T>(fn: (q: (s: string) => Promise<any>) => Promise<T>): Promise<T> {
  const pg = (await import("pg")).default;
  const c = new pg.Client(adminConfig());
  await c.connect();
  try { return await fn((s) => c.query(s)); } finally { await c.end(); }
}

export async function pgAvailable(): Promise<boolean> {
  try { await admin((q) => q("SELECT 1")); return true; } catch { return false; }
}

export function uniqueName(prefix: string): string {
  return `${prefix}_${process.pid}_${randomBytes(4).toString("hex")}`.toLowerCase().replace(/[^a-z0-9_]/g, "_");
}

export async function createDatabase(name: string, template?: string) {
  await admin((q) => q(`CREATE DATABASE "${name}"${template ? ` TEMPLATE "${template}"` : ""}`));
  return withDatabase(adminConfig(), name);
}

export async function dropDatabase(name: string) {
  await admin((q) => q(`DROP DATABASE IF EXISTS "${name}" WITH (FORCE)`));
}

/** Role + schema for an app in the shared database (as `dbtool new-app` in the Python package): role NAME
 *  (NOLOGIN; you are a member, so you can use and own its objects), schema NAME in the shared database owned by it,
 *  no USAGE for PUBLIC, so other app roles cannot see it. Creates the shared database if it is missing. */
export async function newApp(name: string, database = DATABASE): Promise<string[]> {
  if (!/^[a-z][a-z0-9_]*$/.test(name)) throw new Error("app names: lower case letters, digits and _");
  const done: string[] = [];
  await admin(async (q) => {
    if (!(await q(`SELECT 1 FROM pg_roles WHERE rolname = '${name}'`)).rowCount) { await q(`CREATE ROLE "${name}" NOLOGIN`); done.push(`created role ${name}`); }
    if (!(await q(`SELECT 1 FROM pg_database WHERE datname = '${database}'`)).rowCount) { await q(`CREATE DATABASE "${database}"`); done.push(`created database ${database}`); }
  });
  const pg = (await import("pg")).default;
  const c = new pg.Client(withDatabase(adminConfig(), database));
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
