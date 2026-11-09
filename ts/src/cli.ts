#!/usr/bin/env node
/**
 * omarchy-db: the app-facing command line for TypeScript apps (run inside the app; config in package.json).
 *
 *   omarchy-db generate -m "add pinned"   write the next migration if the schema changed (review it, commit it)
 *   omarchy-db check                      drift guard: schema vs migrations, migrations from empty on every engine
 *   omarchy-db migrate --live [--url URL] postgresql apps: apply pending migrations to the app's database
 *                                         (--live, or OMARCHY_DB_LIVE=1: you mean the real database; refused under tests)
 *   omarchy-db new-app [NAME]             postgresql apps: role + database on the shared server (no sudo)
 *
 * D1 apps apply migrations in production with `wrangler d1 migrations apply <db> --remote`
 * (wrangler.toml: migrations_dir = "migrations"); the folder format is compatible.
 */
import { loadProject, loadSchema, postgresConfig, schemaExports } from "./config.js";
import { generate } from "./generate.js";
import { check } from "./check.js";
import { migratePostgres } from "./migrate.js";
import { newApp } from "./scratch.js";
import { allowLive, checkConfig } from "./guard.js";
import { relative } from "node:path";

function arg(name: string): string | undefined {
  const i = process.argv.indexOf(name);
  return i > 0 ? process.argv[i + 1] : undefined;
}

async function main() {
  const cmd = process.argv[2];
  if (cmd === "new-app") {
    const name = (process.argv[3] && !process.argv[3].startsWith("-")) ? process.argv[3] : loadProject().app;
    for (const l of await newApp(name)) console.log(l);
    console.log(`${name}: schema ${name} in the shared database is ready (omarchy-db migrate creates the tables)`);
    return;
  }
  const p = loadProject();
  if (cmd === "generate") {
    const msg = arg("-m") ?? arg("--message");
    if (!msg) throw new Error('usage: omarchy-db generate -m "message"');
    const schema = await loadSchema(p);
    const tables = schemaExports(schema);
    const f = await generate(p.dialect, tables, p.migrationsDir, msg);
    console.log(f ? `wrote ${relative(p.root, f)}: review it, then commit it` : "no changes: the schema matches the migrations");
  } else if (cmd === "check") {
    const results = await check(p);
    for (const r of results) console.log(`${r.ok ? "ok  " : "FAIL"} [${r.engine}] ${r.detail}`);
    if (results.some((r) => !r.ok)) process.exit(1);
  } else if (cmd === "migrate") {
    if (p.dialect !== "postgresql") throw new Error("sqlite/D1 apps: wrangler d1 migrations apply <db> --remote");
    const pg = (await import("pg")).default;
    if (process.argv.includes("--live")) allowLive();
    const url = arg("--url");
    const pool = new pg.Pool(url ? checkConfig({ connectionString: url }, "migrate --url") : postgresConfig(p.app));
    try {
      const { applied } = await migratePostgres(pool, p.migrationsDir, { schema: p.app, ownerRole: p.app });
      console.log(applied.length ? `applied: ${applied.join(", ")}` : "up to date");
    } finally { await pool.end(); }
  } else {
    console.log("usage: omarchy-db generate -m MSG | check | migrate --live [--url URL] | new-app [NAME]");
    process.exit(cmd ? 1 : 0);
  }
}

main().catch((e) => { console.error(`omarchy-db: ${e?.message ?? e}`); process.exit(1); });
