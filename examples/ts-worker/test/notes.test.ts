// Runs on SQLite (better-sqlite3, default) or D1 (Miniflare: OMARCHY_DB_TEST_ENGINE=d1).
import { afterAll, afterEach, beforeEach, describe, expect, it } from "vitest";
import { freshDb, dropTemplates, type TestDb } from "@omarchy/db/testing";
import * as schema from "../src/schema";

let t: TestDb;
beforeEach(async () => { t = await freshDb(schema); });
afterEach(async () => { await t.close(); });
afterAll(dropTemplates);

describe(`worker notes on ${process.env.OMARCHY_DB_TEST_ENGINE ?? "sqlite"}`, () => {
  it("round-trips records with defaults", async () => {
    const [ada] = await t.db.insert(schema.authors).values({ name: "Ada" }).returning();
    await t.db.insert(schema.notes).values([{ text: "first", authorId: ada.id }, { text: "second", authorId: ada.id, pinned: true }]);
    const rows = await t.db.select().from(schema.notes).orderBy(schema.notes.id);
    expect(rows.map((r: any) => [r.text, r.pinned])).toEqual([["first", false], ["second", true]]);
    expect(rows[0].createdAt).toBeInstanceOf(Date);
  });

  it("enforces unique and foreign keys", async () => {
    await t.db.insert(schema.authors).values({ name: "Bob" });
    await expect(t.db.insert(schema.authors).values({ name: "Bob" })).rejects.toThrow();
    await expect(t.db.insert(schema.notes).values({ text: "x", authorId: 999 })).rejects.toThrow();
  });

  it("gives each test a fresh database", async () => {
    expect(await t.db.select().from(schema.notes)).toEqual([]);
  });
});
