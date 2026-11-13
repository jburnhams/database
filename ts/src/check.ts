/**
 * The drift guard (as `dbtool check` in Python):
 *  1. the schema has no change that is missing a migration (diff against the newest snapshot is empty);
 *  2. on every engine of the app's dialect, the migrations run from an empty database, and the result matches the
 *     schema (drizzle-kit's push diff against the migrated database is empty), which catches hand edits that broke
 *     a migration or a snapshot that does not match its SQL.
 * postgresql apps: PGlite, plus a throwaway PostgreSQL server (testserver.ts) when one can be had. sqlite apps: SQLite, plus D1 (Miniflare) when installed.
 */
import { loadSchema, schemaExports, type Project } from "./config.js";
import { diff } from "./generate.js";
import { freshDb, dropTemplates, type Engine } from "./testing.js";
import { pgAvailable } from "./scratch.js";

export interface CheckResult { engine: Engine | "schema"; ok: boolean; detail: string }

/** drizzle-kit's push prints a progress spinner; keep the check's output to its own lines. */
async function quiet<T>(fn: () => Promise<T>): Promise<T> {
  const write = process.stdout.write.bind(process.stdout), err = process.stderr.write.bind(process.stderr);
  (process.stdout as any).write = () => true; (process.stderr as any).write = () => true;
  try { return await fn(); } finally { process.stdout.write = write; process.stderr.write = err; }
}

async function hasMiniflare() {
  try { await import("miniflare"); return true; } catch { return false; }
}

export async function check(p: Project, only?: Engine[]): Promise<CheckResult[]> {
  const schema = await loadSchema(p);
  const tables = schemaExports(schema);
  const out: CheckResult[] = [];
  const { statements } = await diff(p.dialect, tables, p.migrationsDir);
  out.push(statements.length
    ? { engine: "schema", ok: false, detail: `the schema has changes without a migration (run omarchy-db generate):\n  ${statements.join("\n  ")}` }
    : { engine: "schema", ok: true, detail: "every schema change has a migration" });

  const engines: Engine[] = only ?? (p.dialect === "postgresql"
    ? ["pglite", ...((await pgAvailable()) ? ["postgres" as Engine] : [])]
    : ["sqlite", ...((await hasMiniflare()) ? ["d1" as Engine] : [])]);
  const k: any = await import("drizzle-kit/api");
  for (const engine of engines) {
    let t: Awaited<ReturnType<typeof freshDb>> | undefined;
    try {
      t = await freshDb(tables, { engine, project: p });
      let left: string[] = [];
      if (p.dialect === "postgresql") left = (await quiet<any>(() => k.pushSchema(tables, t!.db, [p.app]))).statementsToExecute;
      else if (engine === "sqlite") left = (await quiet<any>(() => k.pushSQLiteSchema(tables, t!.db))).statementsToExecute;
      left = left.filter((s) => !s.includes("omarchy_migrations"));        // our bookkeeping table is not in the schema
      out.push(left.length
        ? { engine, ok: false, detail: `after migrating from empty the database differs from the schema:\n  ${left.join("\n  ")}` }
        : { engine, ok: true, detail: engine === "d1" ? "migrations run from empty" : "migrations run from empty and match the schema" });
    } catch (e: any) {
      out.push({ engine, ok: false, detail: `migrating from empty failed: ${e?.message ?? e}` });
    } finally {
      await t?.close();
    }
  }
  await dropTemplates();
  return out;
}
