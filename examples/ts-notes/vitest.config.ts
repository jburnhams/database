import { defineConfig } from "vitest/config";
// Postgres tests create and drop databases on the server (CREATE DATABASE ... TEMPLATE): allow more than 5 s.
export default defineConfig({ test: { testTimeout: 60_000, hookTimeout: 60_000 } });
