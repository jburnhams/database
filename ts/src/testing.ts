/**
 * vitest (or any runner) helpers: a fresh, migrated database per test.
 *
 *   import { freshDb } from "@omarchy/db/testing";
 *   import * as schema from "../src/schema";
 *
 *   let t: Awaited<ReturnType<typeof freshDb<typeof schema>>>;
 *   beforeEach(async () => { t = await freshDb(schema); });
 *   afterEach(() => t.close());
 *   it("...", async () => { await t.db.insert(schema.notes).values({ text: "x" }); });
 *
 * Engine: OMARCHY_DB_TEST_ENGINE, else the dialect's default.
 *   postgresql apps: "pglite" (default: in-process Postgres; migrated once, then pg.clone() per test) or
 *                    "postgres" (the real server: CREATE DATABASE ... TEMPLATE of a migrated template, dropped after)
 *   sqlite apps:     "sqlite" (default: better-sqlite3 in memory; migrated once, then a copy via serialize()) or
 *                    "d1" (Miniflare's D1 simulator, migrated per test: slower, closest to production)
 */
import { loadProject, type Project } from "./config.js";
import { migrateD1, migratePglite, migratePostgres, migrateSqlite } from "./migrate.js";
import { createDatabase, dropDatabase, uniqueName } from "./scratch.js";

export type Engine = "pglite" | "postgres" | "sqlite" | "d1";
type Schema = Record<string, unknown>;

export function testEngine(p: Project = loadProject()): Engine {
  const e = process.env.OMARCHY_DB_TEST_ENGINE as Engine | undefined;
  const allowed: Engine[] = p.dialect === "postgresql" ? ["pglite", "postgres"] : ["sqlite", "d1"];
  if (e && !allowed.includes(e)) throw new Error(`OMARCHY_DB_TEST_ENGINE=${e} does not fit a ${p.dialect} app (${allowed.join(", ")})`);
  return e ?? allowed[0];
}

const templates = new Map<string, Promise<any>>();
function template<T>(key: string, make: () => Promise<T>): Promise<T> {
  if (!templates.has(key)) templates.set(key, make());
  return templates.get(key)!;
}

export interface TestDb<D = any> { db: D; engine: Engine; client: any; close: () => Promise<void> }

export async function freshDb<S extends Schema>(schema: S, opts: { engine?: Engine; project?: Project } = {}): Promise<TestDb> {
  const p = opts.project ?? loadProject();
  const engine = opts.engine ?? testEngine(p);
  const dir = p.migrationsDir;

  if (engine === "pglite") {
    const { PGlite } = await import("@electric-sql/pglite");
    const { drizzle } = await import("drizzle-orm/pglite");
    const tmpl = await template(`pglite:${dir}`, async () => { const pg = new PGlite(); await migratePglite(pg, dir, { schema: p.app }); return pg; });
    const client = (await tmpl.clone()) as any;
    return { db: drizzle(client, { schema }), engine, client, close: () => client.close() };
  }
  if (engine === "postgres") {
    const pg = (await import("pg")).default;
    const { drizzle } = await import("drizzle-orm/node-postgres");
    const tmplName = await template(`postgres:${dir}`, async () => {
      const name = uniqueName(`omdb_tmpl_${p.app}`);
      const c = new pg.Client(await createDatabase(name)); await c.connect();
      try { await migratePostgres(c, dir, { schema: p.app }); } finally { await c.end(); }
      registerTemplate(name);
      return name;
    });
    const name = uniqueName(`omdb_test_${p.app}`);
    const pool = new pg.Pool({ ...(await createDatabase(name, tmplName)), max: 2 });
    return { db: drizzle(pool, { schema }), engine, client: pool, close: async () => { await pool.end(); await dropDatabase(name); } };
  }
  if (engine === "sqlite") {
    const Database = (await import("better-sqlite3")).default;
    const { drizzle } = await import("drizzle-orm/better-sqlite3");
    const image: Buffer = await template(`sqlite:${dir}`, async () => {
      const db = new Database(":memory:"); migrateSqlite(db, dir); const b = db.serialize(); db.close(); return b;
    });
    const client = new Database(image);
    client.pragma("foreign_keys = ON");
    return { db: drizzle(client, { schema }), engine, client, close: async () => { client.close(); } };
  }
  // d1
  const { Miniflare } = await import("miniflare");
  const { drizzle } = await import("drizzle-orm/d1");
  const mf = new Miniflare({ modules: true, script: "export default { fetch() { return new Response('') } }", d1Databases: ["DB"] });
  const client = await mf.getD1Database("DB");
  await migrateD1(client as any, dir);
  return { db: drizzle(client as any, { schema }), engine, client, close: () => mf.dispose() };
}

const pgTemplates: string[] = [];
function registerTemplate(name: string) { pgTemplates.push(name); }

/** Drop the Postgres template databases this process made (call in afterAll, or a vitest globalSetup teardown). */
export async function dropTemplates() {
  for (const n of pgTemplates.splice(0)) await dropDatabase(n);
  for (const [k, v] of templates) {
    if (k.startsWith("pglite:")) (await v).close();
    templates.delete(k);
  }
}
