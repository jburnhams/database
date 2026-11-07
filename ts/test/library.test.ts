// Tests the built package (npm run build first). Library tests on temporary projects: generate / drift check, SQLite table rebuilds under foreign keys (SQLite and D1),
// violation rollback, and Postgres new-app (schema + role in a shared database, isolation, cross-schema reads).
import { mkdtempSync, mkdirSync, writeFileSync, readdirSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import { check, diff, generate, loadProject, loadSchema, migrateSqlite, migrateD1, newApp, schemaExports } from "../dist/index.js";
import { dropTemplates } from "../dist/testing.js";
import { adminConfig, createDatabase, dropDatabase, pgAvailable, uniqueName, withDatabase } from "../dist/scratch.js";

const ROOT = join(import.meta.dirname, "..");
const PG = await pgAvailable();
afterAll(dropTemplates);

/** Write a new schema version under a new file name (ES modules are cached by URL, so rewriting one file would not
 *  be seen again in the same process) and point package.json at it. */
let version = 0;
function setSchema(dir: string, name: string, dialect: string, src: string) {
  const file = `schema_${++version}.ts`;
  writeFileSync(join(dir, file), src.replaceAll("APP", name));
  writeFileSync(join(dir, "package.json"), JSON.stringify({ name, type: "module", "omarchy-db": { app: name, dialect, schemaFile: file, migrations: "migrations" } }));
}

function project(name: string, dialect: "postgresql" | "sqlite", schemaSrc: string) {
  const dir = mkdtempSync(join(tmpdir(), "omdb-ts-"));
  setSchema(dir, name, dialect, schemaSrc);
  return dir;
}

const SQLITE_V1 = `import { integer, sqliteTable, text } from "${join(ROOT, "node_modules/drizzle-orm/sqlite-core/index.js")}";
export const authors = sqliteTable("authors", { id: integer().primaryKey(), name: text() });
export const notes = sqliteTable("notes", { id: integer().primaryKey(), authorId: integer("author_id").notNull().references(() => authors.id) });
`;
const SQLITE_V2 = SQLITE_V1.replace("name: text() }", "name: text().notNull() }");   // NOT NULL on SQLite = table rebuild

async function tables(dir: string) {
  return schemaExports(await loadSchema(loadProject(dir)));      // what the CLI passes to drizzle-kit
}

describe("generate and drift", () => {
  it("writes a migration only when the schema changed, and check catches a missing one", async () => {
    const dir = project("drift_app", "sqlite", SQLITE_V1);
    const p = loadProject(dir);
    expect(await generate("sqlite", await tables(dir), p.migrationsDir, "initial")).toMatch(/0001_initial\.sql$/);
    expect(await generate("sqlite", await tables(dir), p.migrationsDir, "again")).toBeUndefined();
    expect((await check(p, ["sqlite"])).every((r) => r.ok)).toBe(true);
    setSchema(dir, "drift_app", "sqlite", SQLITE_V2);
    const res = await check(loadProject(dir), ["sqlite"]);
    expect(res.find((r) => r.engine === "schema")?.ok).toBe(false);
  });
});

describe("SQLite table rebuild with foreign keys and data", () => {
  async function setup(name: string) {
    const dir = project(name, "sqlite", SQLITE_V1);
    const p = loadProject(dir);
    await generate("sqlite", await tables(dir), p.migrationsDir, "initial");
    setSchema(dir, name, "sqlite", SQLITE_V2);
    const f = await generate("sqlite", await tables(dir), loadProject(dir).migrationsDir, "name not null");
    const sql = readFileSync(f!, "utf8");
    expect(sql).toMatch(/__new_authors/);                                        // drizzle-kit rebuilds the parent table
    expect(sql).toMatch(/^PRAGMA defer_foreign_keys = on;/m);                     // our fix ...
    expect(sql).not.toMatch(/PRAGMA foreign_keys/);                               // ... instead of a no-op inside a transaction
    return p;
  }

  it("keeps the data on SQLite (better-sqlite3)", async () => {
    const p = await setup("rebuild_sqlite");
    const Database = (await import("better-sqlite3")).default;
    const db = new Database(":memory:");
    const only = mkdtempSync(join(tmpdir(), "omdb-mig-"));
    const files = readdirSync(p.migrationsDir).filter((f) => f.endsWith(".sql")).sort();
    writeFileSync(join(only, files[0]), readFileSync(join(p.migrationsDir, files[0])));
    migrateSqlite(db, only);
    db.exec("INSERT INTO authors (id, name) VALUES (1, 'Ada'); INSERT INTO notes (id, author_id) VALUES (1, 1);");
    expect(migrateSqlite(db, p.migrationsDir).applied).toEqual([files[1].replace(".sql", "")]);
    expect(db.prepare("SELECT a.name FROM notes n JOIN authors a ON a.id = n.author_id").get()).toEqual({ name: "Ada" });
    expect(db.pragma("foreign_keys", { simple: true })).toBe(1);
  });

  it("fails atomically on D1 (Miniflare), which cannot switch foreign keys off, with the data intact", async () => {
    const p = await setup("rebuild_d1");
    const { Miniflare } = await import("miniflare");
    const mf = new Miniflare({ modules: true, script: "export default { fetch() { return new Response('') } }", d1Databases: ["DB"] });
    try {
      const d1: any = await mf.getD1Database("DB");
      const only = mkdtempSync(join(tmpdir(), "omdb-mig-"));
      const files = readdirSync(p.migrationsDir).filter((f) => f.endsWith(".sql")).sort();
      writeFileSync(join(only, files[0]), readFileSync(join(p.migrationsDir, files[0])));
      await migrateD1(d1, only);
      await d1.batch([d1.prepare("INSERT INTO authors (id, name) VALUES (1, 'Ada')"), d1.prepare("INSERT INTO notes (id, author_id) VALUES (1, 1)")]);
      await expect(migrateD1(d1, p.migrationsDir)).rejects.toThrow(/rebuilds authors, which existing rows reference.*ON DELETE NO ACTION/);
      expect(await d1.prepare("SELECT a.name FROM notes n JOIN authors a ON a.id = n.author_id").first()).toEqual({ name: "Ada" });
      expect((await d1.prepare("SELECT name FROM omarchy_migrations").all()).results).toHaveLength(1);
    } finally { await mf.dispose(); }
  });

  it("rolls back a migration that leaves dangling references", async () => {
    const dir = project("dangling", "sqlite", SQLITE_V1);
    const p = loadProject(dir);
    await generate("sqlite", await tables(dir), p.migrationsDir, "initial");
    const Database = (await import("better-sqlite3")).default;
    const db = new Database(":memory:");
    migrateSqlite(db, p.migrationsDir);
    db.exec("INSERT INTO authors (id, name) VALUES (1, 'Ada'); INSERT INTO notes (id, author_id) VALUES (1, 1);");
    writeFileSync(join(p.migrationsDir, "0002_bad.sql"), "PRAGMA defer_foreign_keys = on;\n--> statement-breakpoint\nDELETE FROM authors;");
    expect(() => migrateSqlite(db, p.migrationsDir)).toThrow(/foreign key/);
    expect(db.prepare("SELECT count(*) AS n FROM authors").get()).toEqual({ n: 1 });
  });
});

const PG_SCHEMA = `import { integer, pgSchema, text } from "${join(ROOT, "node_modules/drizzle-orm/pg-core/index.js")}";
export const app = pgSchema("APP");
export const things = app.table("things", { id: integer().primaryKey().generatedByDefaultAsIdentity(), name: text().notNull() });
`;

describe.runIf(PG)("Postgres: schema per app in one shared database", () => {
  it("new-app makes role + schema; migrations land there, owned by the role; apps are isolated; admin joins across", async () => {
    const pg = (await import("pg")).default;
    const db = uniqueName("omdb_shared_ts");
    const apps = [uniqueName("tsa").slice(0, 20), uniqueName("tsb").slice(0, 20)];
    await createDatabase(db);
    const admin = new pg.Client(withDatabase(adminConfig(), db));
    try {
      for (const a of apps) {
        const dir = project(a, "postgresql", PG_SCHEMA);
        const p = loadProject(dir);
        await generate("postgresql", await tables(dir), p.migrationsDir, "initial");
        expect((await newApp(a, db)).join(" ")).toMatch(new RegExp(`created schema ${db}\\.${a}`));
        expect(await newApp(a, db)).toEqual([]);
        const { migratePostgres } = await import("../dist/index.js");
        const c = new pg.Client(withDatabase(adminConfig(), db)); await c.connect();
        try { await migratePostgres(c, p.migrationsDir, { schema: a, ownerRole: a }); await c.query(`INSERT INTO "${a}".things (name) VALUES ('from ${a}')`); }
        finally { await c.end(); }
      }
      await admin.connect();
      const [a, b] = apps;
      const owners = (await admin.query(`SELECT schemaname, tableowner FROM pg_tables WHERE tablename = 'things' ORDER BY 1`)).rows;
      expect(owners).toEqual(apps.slice().sort().map((s) => ({ schemaname: s, tableowner: s })));
      expect((await admin.query(`SELECT x.name AS x, y.name AS y FROM "${a}".things x CROSS JOIN "${b}".things y`)).rows).toEqual([{ x: `from ${a}`, y: `from ${b}` }]);
      expect((await admin.query(`SELECT has_schema_privilege($1, $2, 'USAGE') AS u`, [a, b])).rows[0].u).toBe(false);
      await admin.query("BEGIN"); await admin.query(`SET LOCAL ROLE "${a}"`);
      await expect(admin.query(`SELECT * FROM "${b}".things`)).rejects.toThrow(/permission denied/);
      await admin.query("ROLLBACK");
    } finally {
      await admin.end().catch(() => {});
      await dropDatabase(db);
      const c = new pg.Client(adminConfig()); await c.connect();
      for (const r of apps) await c.query(`DROP ROLE IF EXISTS "${r}"`);
      await c.end();
    }
  });
});

const CASCADE_V1 = `import { integer, sqliteTable, text } from "${join(ROOT, "node_modules/drizzle-orm/sqlite-core/index.js")}";
export const authors = sqliteTable("authors", { id: integer().primaryKey(), name: text() });
export const notes = sqliteTable("notes", { id: integer().primaryKey(), authorId: integer("author_id").references(() => authors.id, { onDelete: "cascade" }) });
`;

describe("D1 refuses a rebuild that would cascade into child rows", () => {
  it("refuses before running anything; the child rows and the migration record are unchanged", async () => {
    const dir = project("cascade_d1", "sqlite", CASCADE_V1);
    const p = loadProject(dir);
    await generate("sqlite", await tables(dir), p.migrationsDir, "initial");
    setSchema(dir, "cascade_d1", "sqlite", CASCADE_V1.replace("name: text() }", "name: text().notNull() }"));
    await generate("sqlite", await tables(dir), loadProject(dir).migrationsDir, "name not null");
    const { Miniflare } = await import("miniflare");
    const mf = new Miniflare({ modules: true, script: "export default { fetch() { return new Response('') } }", d1Databases: ["DB"] });
    try {
      const d1: any = await mf.getD1Database("DB");
      const only = mkdtempSync(join(tmpdir(), "omdb-mig-"));
      const files = readdirSync(p.migrationsDir).filter((f) => f.endsWith(".sql")).sort();
      writeFileSync(join(only, files[0]), readFileSync(join(p.migrationsDir, files[0])));
      await migrateD1(d1, only);
      await d1.batch([d1.prepare("INSERT INTO authors (id, name) VALUES (1, 'Ada')"), d1.prepare("INSERT INTO notes (id, author_id) VALUES (1, 1)")]);
      await expect(migrateD1(d1, p.migrationsDir)).rejects.toThrow(/notes \(1 rows, ON DELETE CASCADE\) references authors.*Refused before running anything/);
      expect(await d1.prepare("SELECT count(*) AS n FROM notes").first()).toEqual({ n: 1 });
      expect((await d1.prepare("SELECT name FROM omarchy_migrations").all()).results).toHaveLength(1);
      // with no child rows the same rebuild is fine
      await d1.prepare("DELETE FROM notes").run();
      await migrateD1(d1, p.migrationsDir);
      expect((await d1.prepare("SELECT name FROM omarchy_migrations").all()).results).toHaveLength(2);
    } finally { await mf.dispose(); }
  });
});

const ENUM_V1 = `import { integer, pgSchema } from "${join(ROOT, "node_modules/drizzle-orm/pg-core/index.js")}";
export const app = pgSchema("APP");
export const mood = app.enum("mood", ["happy", "sad"]);
export const days = app.table("days", { id: integer().primaryKey().generatedByDefaultAsIdentity(), mood: mood().notNull() });
`;

describe("Postgres enums", () => {
  async function enumProject(name: string) {
    const dir = project(name, "postgresql", ENUM_V1);
    const p = loadProject(dir);
    const f1 = await generate("postgresql", await tables(dir), p.migrationsDir, "initial");
    expect(readFileSync(f1!, "utf8")).toMatch(/CREATE TYPE "[a-z_]+"\."mood" AS ENUM\('happy', 'sad'\)/);   // enum export kept
    setSchema(dir, name, "postgresql", ENUM_V1.replace('["happy", "sad"]', '["happy", "sad", "meh"]'));
    expect((await check(loadProject(dir), ["pglite"])).find((r) => r.engine === "schema")?.ok).toBe(false); // drift seen
    const f2 = await generate("postgresql", await tables(dir), p.migrationsDir, "add meh");
    expect(readFileSync(f2!, "utf8")).toMatch(/ALTER TYPE "[a-z_]+"\."mood" ADD VALUE 'meh'/);
    // a later migration that uses the new value: only works if the previous one was committed first
    writeFileSync(join(p.migrationsDir, "0003_use_meh.sql"), `INSERT INTO "${name}"."days" (mood) VALUES ('meh');`);
    return loadProject(dir);
  }

  it("generates CREATE TYPE / ADD VALUE and applies an add-then-use pair in one run (PGlite)", async () => {
    const p = await enumProject("enum_pglite");
    const { PGlite } = await import("@electric-sql/pglite");
    const { migratePglite } = await import("../dist/index.js");
    const pg = new PGlite();
    try {
      expect((await migratePglite(pg, p.migrationsDir, { schema: p.app })).applied).toHaveLength(3);
      expect((await pg.query(`SELECT mood FROM "${p.app}".days`)).rows).toEqual([{ mood: "meh" }]);
    } finally { await pg.close(); }
  });

  it.runIf(PG)("the same on the real server, serialised by the advisory lock", async () => {
    const p = await enumProject("enum_pg");
    const pg = (await import("pg")).default;
    const { migratePostgres } = await import("../dist/index.js");
    const name = uniqueName("omdb_enum");
    const cfg = await createDatabase(name);
    try {
      const [a, b] = await Promise.all([0, 1].map(async () => {                  // two migrators at once
        const pool = new pg.Pool({ ...cfg, max: 1 });
        try { return (await migratePostgres(pool, p.migrationsDir, { schema: p.app })).applied.length; } finally { await pool.end(); }
      }));
      expect(a + b).toBe(3);                                                       // each file applied exactly once
      const c = new pg.Client(cfg); await c.connect();
      try { expect((await c.query(`SELECT mood FROM "${p.app}".days`)).rows).toEqual([{ mood: "meh" }]); } finally { await c.end(); }
    } finally { await dropDatabase(name); }
  });
});
