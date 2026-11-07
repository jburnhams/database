// A D1 app's tables (sqlite dialect). Same workflow: edit, `npm run db:generate -- "..."`, review, commit;
// production: `wrangler d1 migrations apply <db> --remote` with migrations_dir = "migrations".
import { sql } from "drizzle-orm";
import { index, integer, sqliteTable, text } from "drizzle-orm/sqlite-core";

export const authors = sqliteTable("authors", {
  id: integer().primaryKey({ autoIncrement: true }),
  name: text().notNull().unique(),
});

export const notes = sqliteTable("notes", {
  id: integer().primaryKey({ autoIncrement: true }),
  text: text().notNull(),
  authorId: integer("author_id").references(() => authors.id),
  createdAt: integer("created_at", { mode: "timestamp_ms" }).notNull().default(sql`(unixepoch('subsec') * 1000)`),
  pinned: integer({ mode: "boolean" }).notNull().default(false),   // added later
}, (t) => [index("ix_notes_author_id").on(t.authorId)]);
