/**
 * Apply an app's migrations (generate.ts format) to a database, in order, each once, recorded in omarchy_migrations.
 * Safe at every app start. Each migration runs in one transaction (D1: one batch, which is a transaction).
 *
 *   Postgres (node-postgres Pool/Client)  advisory lock, optional SET LOCAL ROLE <owner role>
 *   PGlite                               as Postgres, single connection
 *   SQLite (better-sqlite3)              foreign keys switched OFF around the transaction (the pragma does nothing
 *                                        inside one): a table rebuild drops the old table, and with enforcement on
 *                                        that deletes-then-counts every child row as a violation, which deferring
 *                                        does not undo. PRAGMA foreign_key_check must be clean before COMMIT.
 *   D1 (binding)                         the same SQL in one batch (atomic). D1 cannot switch foreign keys off, so a
 *                                        migration that rebuilds a table other tables' rows reference FAILS there
 *                                        (rolled back, data intact): split it (rebuild the children too, or add a
 *                                        new table and copy) or avoid the rebuild (see docs/ADOPTING.md).
 */
import { listMigrations, type MigrationFile } from "./generate.js";

const TABLE = "omarchy_migrations";

type PgQuery = (sql: string, params?: unknown[]) => Promise<{ rows: any[] }>;

export interface MigrateResult { applied: string[] }

function pending(all: MigrationFile[], done: Set<string>): MigrationFile[] {
  return all.filter((m) => !done.has(m.name));
}

/** Postgres via node-postgres (pg.Pool or pg.Client). */
export async function migratePostgres(pool: { connect?: () => Promise<any>; query: PgQuery }, dir: string,
                                      opts: { schema: string; ownerRole?: string }): Promise<MigrateResult> {
  const isPool = typeof (pool as any).totalCount === "number";          // pg.Pool: check out one client; pg.Client: use it
  const client: any = isPool ? await pool.connect!() : pool;
  try {
    return await migratePgLike((s, p) => client.query(s, p), dir, opts, true);
  } finally {
    if (isPool) client.release();
  }
}

/** PGlite (in-process Postgres, tests). */
export async function migratePglite(pg: { query: PgQuery }, dir: string, opts: { schema: string }): Promise<MigrateResult> {
  return migratePgLike((s, p) => pg.query(s, p), dir, opts, false);
}

async function migratePgLike(q: PgQuery, dir: string, opts: { schema: string; ownerRole?: string }, server: boolean): Promise<MigrateResult> {
  const table = `"${opts.schema}".${TABLE}`;               // bookkeeping lives in the app's own schema
  const all = listMigrations(dir), applied: string[] = [];
  // One transaction PER MIGRATION, so a value added to an enum in one file is committed before a later file uses
  // it (Postgres refuses to use an enum value added in the same transaction). Two processes migrating at once are
  // still serialised: a session-level advisory lock is held from the first file to the last.
  const lockKey = "hashtext('omarchy_db:' || current_database() || ':' || $1)";
  if (server) await q(`SELECT pg_advisory_lock(${lockKey})`, [opts.schema]);
  const tx = async (fn: () => Promise<void>) => {
    await q("BEGIN");
    try {
      if (role) await q(`SET LOCAL ROLE "${role}"`);
      await fn();
      await q("COMMIT");
    } catch (e) { await q("ROLLBACK").catch(() => {}); throw e; }
  };
  let role: string | undefined;
  try {
    role = opts.ownerRole &&
      (await q("SELECT pg_has_role(current_user, $1, 'SET') AS ok FROM pg_roles WHERE rolname = $1", [opts.ownerRole])).rows[0]?.ok
      ? opts.ownerRole : undefined;
    // Create the schema (if new-app has not) as ourselves, owned by the app role: the app role has no CREATE on
    // the database, so it could not create it, and even CREATE SCHEMA IF NOT EXISTS needs that privilege.
    if (!(await q("SELECT 1 FROM pg_namespace WHERE nspname = $1", [opts.schema])).rows.length) {
      await q("BEGIN");
      try {
        await q(`CREATE SCHEMA "${opts.schema}"${role ? ` AUTHORIZATION "${role}"` : ""}`);
        await q(`REVOKE ALL ON SCHEMA "${opts.schema}" FROM PUBLIC`);
        await q("COMMIT");
      } catch (e) { await q("ROLLBACK").catch(() => {}); throw e; }
    }
    const ownSchema = new RegExp(`^CREATE SCHEMA IF NOT EXISTS "${opts.schema}";?$`);
    let done = new Set<string>();
    await tx(async () => {
      await q(`CREATE TABLE IF NOT EXISTS ${table} (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())`);
      done = new Set((await q(`SELECT name FROM ${table}`)).rows.map((r) => r.name));
    });
    for (const m of pending(all, done)) {
      await tx(async () => {
        for (const s of m.statements) if (!ownSchema.test(s.trim())) await q(s);     // the schema exists by now
        await q(`INSERT INTO ${table} (name) VALUES ($1)`, [m.name]);
      });
      applied.push(m.name);
    }
  } finally {
    if (server) await q(`SELECT pg_advisory_unlock(${lockKey})`, [opts.schema]).catch(() => {});
  }
  return { applied };
}

/** SQLite via better-sqlite3 (tests, local tools). */
export function migrateSqlite(db: { exec(sql: string): unknown; prepare(sql: string): { all(...p: unknown[]): any[]; run(...p: unknown[]): unknown } },
                              dir: string): MigrateResult {
  const all = listMigrations(dir), applied: string[] = [];
  db.exec(`CREATE TABLE IF NOT EXISTS ${TABLE} (name text PRIMARY KEY, applied_at text NOT NULL DEFAULT CURRENT_TIMESTAMP)`);
  const done = new Set(db.prepare(`SELECT name FROM ${TABLE}`).all().map((r) => r.name));
  db.exec("PRAGMA foreign_keys = OFF");
  try {
    for (const m of pending(all, done)) {
      db.exec("BEGIN");
      try {
        for (const s of m.statements) if (!/^PRAGMA defer_foreign_keys/i.test(s)) db.exec(s);
        const bad = db.prepare("PRAGMA foreign_key_check").all();
        if (bad.length) throw new Error(`${m.name} left foreign key violations: ${JSON.stringify(bad.slice(0, 10))}; rolled back`);
        db.prepare(`INSERT INTO ${TABLE} (name) VALUES (?)`).run(m.name);
        db.exec("COMMIT");
        applied.push(m.name);
      } catch (e) {
        db.exec("ROLLBACK");
        throw e;
      }
    }
  } finally {
    db.exec("PRAGMA foreign_keys = ON");
  }
  return { applied };
}

/** A rebuild (copy into __new_x, DROP TABLE x, rename) of a table that other tables' rows reference cannot be done
 *  safely on D1, which cannot switch foreign keys off: with ON DELETE CASCADE / SET NULL / SET DEFAULT the DROP
 *  silently deletes or rewrites the child rows and the batch COMMITS that; with NO ACTION / RESTRICT it fails.
 *  So refuse before executing anything when any referencing table has rows. */
async function refuseUnsafeD1Rebuild(d1: D1Database, m: MigrationFile) {
  const rebuilt = m.statements.map((s) => s.match(/^DROP TABLE [`"]?(\w+)[`"]?/i)?.[1])
    .filter((t): t is string => !!t && m.statements.some((s) => new RegExp(`__new_${t}\\b`).test(s)));
  if (!rebuilt.length) return;
  const tables = ((await d1.prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE '\\_cf\\_%' ESCAPE '\\' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'").all()).results as any[]).map((r) => r.name);
  const problems: string[] = [];
  for (const child of tables) {
    const fks = ((await d1.prepare(`SELECT "table" AS parent, on_delete FROM pragma_foreign_key_list(?)`).bind(child).all()).results as any[])
      .filter((fk) => rebuilt.includes(fk.parent));
    if (!fks.length) continue;
    const rows = ((await d1.prepare(`SELECT count(*) AS n FROM "${child}"`).first()) as any)?.n ?? 0;
    if (rows > 0) problems.push(`${child} (${rows} rows, ON DELETE ${[...new Set(fks.map((f) => f.on_delete))].join("/")}) references ${[...new Set(fks.map((f) => f.parent))].join(", ")}`);
  }
  if (problems.length)
    throw new Error(`${m.name} rebuilds ${rebuilt.join(", ")}, which existing rows reference: ${problems.join("; ")}. ` +
                    `D1 cannot switch foreign keys off, so the DROP TABLE would delete, null or reject those rows. Refused before ` +
                    `running anything. Split the change (see docs/ADOPTING.md).`);
}

/** Cloudflare D1 (a binding, or Miniflare's getD1Database in tests). Production D1 can also use
 *  `wrangler d1 migrations apply` on the same folder; it tracks its own d1_migrations table instead. */
export async function migrateD1(d1: D1Database, dir: string): Promise<MigrateResult> {
  const all = listMigrations(dir), applied: string[] = [];
  await d1.prepare(`CREATE TABLE IF NOT EXISTS ${TABLE} (name text PRIMARY KEY, applied_at text NOT NULL DEFAULT CURRENT_TIMESTAMP)`).run();
  const done = new Set(((await d1.prepare(`SELECT name FROM ${TABLE}`).all()).results as any[]).map((r) => r.name));
  for (const m of pending(all, done)) {
    await refuseUnsafeD1Rebuild(d1, m);
    try {
      await d1.batch([...m.statements.map((s) => d1.prepare(s)), d1.prepare(`INSERT INTO ${TABLE} (name) VALUES (?)`).bind(m.name)]);
    } catch (e: any) {
      if (/FOREIGN KEY/i.test(String(e?.message)) && m.statements.some((s) => /__new_/.test(s)))
        throw new Error(`${m.name} rebuilds a table that existing rows reference; D1 cannot switch foreign keys off, so it was ` +
                        `rolled back (data unchanged). Split the change (see docs/ADOPTING.md). D1: ${e.message}`);
      throw e;
    }
    applied.push(m.name);
  }
  return { applied };
}
