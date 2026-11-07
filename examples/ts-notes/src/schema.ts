// The app's tables, in its own schema `ts_notes` of the shared database (so other apps can join them,
// e.g. ts_notes.notes with photos.albums). Change them here, then `npm run db:generate -- "what changed"`.
import { boolean, index, integer, pgSchema, text, timestamp } from "drizzle-orm/pg-core";

export const app = pgSchema("ts_notes");

export const authors = app.table("authors", {
  id: integer().primaryKey().generatedByDefaultAsIdentity(),
  name: text().notNull().unique(),
});

export const notes = app.table("notes", {
  id: integer().primaryKey().generatedByDefaultAsIdentity(),
  text: text().notNull(),
  authorId: integer("author_id").references(() => authors.id),
  createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  pinned: boolean().notNull().default(false),        // added later: the default fills existing rows
}, (t) => [index("ix_notes_author_id").on(t.authorId)]);
