/**
 * An app's database settings, from its package.json:
 *
 *   "omarchy-db": {
 *     "app": "notes",                 // the app's Postgres schema and owner role (lower case, digits, _)
 *     "dialect": "postgresql",        // or "sqlite" (Cloudflare D1 apps)
 *     "schemaFile": "src/schema.ts",  // module whose exported tables are the app's tables
 *     "migrations": "migrations"      // NNNN_name.sql + meta/NNNN_name.json snapshots, committed with the app
 *   }
 *
 * Layout on the shared server: ONE database (default name `omarchy`; set OMARCHY_DB_DATABASE) with a schema per app (tables `notes.notes`, ...), so apps
 * can query each other's data. postgresql apps declare their tables in their schema:
 *   export const app = pgSchema("notes");  export const notes = app.table("notes", { ... });
 * Which server: OMARCHY_DB_URL, else <APP>_DATABASE_URL, else the shared database over the Unix socket
 * (/run/postgresql, peer auth as your OS user).
 */
import { readFileSync, existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

export type Dialect = "postgresql" | "sqlite";

/** The one shared database; each app has a schema in it. */
export const DATABASE = process.env.OMARCHY_DB_DATABASE ?? "omarchy";

export interface Project {
  root: string;
  app: string;
  dialect: Dialect;
  schemaPath: string;
  migrationsDir: string;
}

export function loadProject(start: string = process.cwd()): Project {
  let dir = resolve(start);
  for (;;) {
    const f = join(dir, "package.json");
    if (existsSync(f)) {
      const cfg = JSON.parse(readFileSync(f, "utf8"))["omarchy-db"];
      if (cfg) {
        if (!["postgresql", "sqlite"].includes(cfg.dialect)) throw new Error(`${f}: omarchy-db.dialect must be "postgresql" or "sqlite"`);
        if (!/^[a-z][a-z0-9_]*$/.test(cfg.app ?? "")) throw new Error(`${f}: omarchy-db.app must be lower case letters, digits and _ (it is a schema and role name)`);
        return {
          root: dir, app: cfg.app, dialect: cfg.dialect,
          schemaPath: resolve(dir, cfg.schemaFile ?? cfg.schema ?? "src/schema.ts"),
          migrationsDir: resolve(dir, cfg.migrations ?? "migrations"),
        };
      }
    }
    const up = dirname(dir);
    if (up === dir) throw new Error(`no package.json with an "omarchy-db" section in ${start} or above`);
    dir = up;
  }
}

/** The schema module's exports (tables, enums, relations), loaded from TypeScript source. */
export async function loadSchema(p: Project): Promise<Record<string, unknown>> {
  try {
    return await import(pathToFileURL(p.schemaPath).href);          // already inside vitest / tsx / bun
  } catch (e) {
    if (!/Unknown file extension|ERR_UNKNOWN_FILE_EXTENSION|Cannot find module/.test(String(e))) throw e;
    const { tsImport } = await import("tsx/esm/api");
    return await tsImport(p.schemaPath, import.meta.url);
  }
}

/** The parts of a schema module drizzle-kit needs: tables (objects) and enums (pgEnum()/app.enum() return
 *  functions carrying enumName), and schemas/relations (objects). Anything else exported is ignored. */
export function schemaExports(mod: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(mod).filter(([, v]) =>
    v != null && (typeof v === "object" || (typeof v === "function" && "enumName" in (v as any)))));
}

export function envName(app: string): string {
  return app.toUpperCase().replace(/[^A-Z0-9]/g, "_") + "_DATABASE_URL";
}

/** Connection settings for an app's Postgres database: a URL from the environment, or the local socket. */
export function postgresConfig(app: string): { connectionString: string } | { host: string; database: string } {
  for (const v of ["OMARCHY_DB_URL", envName(app)]) {
    const url = process.env[v];
    if (url) return { connectionString: url };
  }
  return { host: "/run/postgresql", database: DATABASE };
}
