/** Worker-safe entry: nothing here touches node:fs or drivers. `import { connectD1 } from "@omarchy/db/d1"`. */
import { drizzle } from "drizzle-orm/d1";

export function connectD1<S extends Record<string, unknown>>(binding: D1Database, schema: S) {
  return drizzle(binding, { schema });
}
