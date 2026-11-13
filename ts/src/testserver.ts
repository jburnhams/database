/**
 * A private, throwaway PostgreSQL server for tests and scratch databases, so they never touch the real one
 * (same as the Python library's omarchy_db.testserver).
 *
 * When OMARCHY_DB_TEST_ADMIN_URL is not set, the first Postgres test database a process needs starts its own
 * cluster: `initdb` into a new temp directory, `postgres` listening only on a Unix socket in that directory (no TCP),
 * fsync off. About a second, once per process (each vitest worker gets its own). It is stopped and deleted when the
 * process exits; a small watchdog does the same if the process is killed instead, and the next start removes
 * anything a watchdog could not. Binaries come from PATH, `pg_config --bindir`, or /usr/lib/postgresql/<n>/bin.
 *
 * The URL is exported as OMARCHY_DB_TEST_ADMIN_URL, so subprocesses reuse it. CI sets OMARCHY_DB_TEST_ADMIN_URL to a
 * service container instead, and nothing is started.
 */
import { execFileSync, spawn, spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir, userInfo } from "node:os";
import { dirname, join } from "node:path";

const PREFIX = "omdb-pg-";
let url: string | undefined;

function which(cmd: string): string | undefined {
  for (const d of (process.env.PATH ?? "").split(":")) if (d && existsSync(join(d, cmd))) return join(d, cmd);
  return undefined;
}

function bindir(): string | undefined {
  const initdb = which("initdb");
  if (initdb && which("pg_ctl")) return dirname(initdb);
  if (which("pg_config")) {
    try {
      const d = execFileSync("pg_config", ["--bindir"], { encoding: "utf8" }).trim();
      if (existsSync(join(d, "initdb"))) return d;
    } catch { /* fall through */ }
  }
  const base = "/usr/lib/postgresql";
  if (existsSync(base)) {
    const v = readdirSync(base).filter((n) => /^\d+$/.test(n) && existsSync(join(base, n, "bin/initdb"))).sort((a, b) => +a - +b);
    if (v.length) return join(base, v[v.length - 1], "bin");
  }
  return undefined;
}

export function available(): boolean {
  return bindir() !== undefined;
}

// A Unix socket path must fit in ~107 bytes: a long TMPDIR falls back to /tmp.
const tmpRoot = () => (tmpdir().length < 60 ? tmpdir() : "/tmp");

function alive(pid: number): boolean {
  try { process.kill(pid, 0); return true; } catch (e: any) { return e.code === "EPERM"; }
}

function stop(d: string, bin: string | undefined) {
  const data = join(d, "data");
  if (bin && existsSync(join(data, "postmaster.pid"))) spawnSync(join(bin, "pg_ctl"), ["-D", data, "-m", "immediate", "-w", "stop"], { stdio: "ignore" });
  rmSync(d, { recursive: true, force: true });
}

/** Stop and delete private servers whose owning process has gone (left by a killed run). */
export function reap(bin = bindir()) {
  for (const n of readdirSync(tmpRoot())) {
    if (!n.startsWith(PREFIX)) continue;
    const d = join(tmpRoot(), n);
    let owner: number;
    try { owner = parseInt(readFileSync(join(d, "owner"), "utf8"), 10); } catch { continue; }
    if (owner && !alive(owner)) stop(d, bin);
  }
}

/** Admin URL of this process's private server, starting it on first use. */
export function testServerUrl(): string {
  if (url) return url;
  const bin = bindir();
  if (!bin) throw new Error("no PostgreSQL server binaries (initdb, pg_ctl) found for a private test server: install the " +
    "PostgreSQL server package, or set OMARCHY_DB_TEST_ADMIN_URL to a throwaway server");
  reap(bin);
  const d = mkdtempSync(join(tmpRoot(), PREFIX));
  writeFileSync(join(d, "owner"), String(process.pid));
  const data = join(d, "data"), user = userInfo().username;
  const initdb = ["-D", data, "-A", "trust", "-U", user, "--no-sync", "-E", "UTF8"];
  // Same collation as the real server (builtin C.UTF-8, PostgreSQL 17+); older initdb falls back to the default.
  let r = spawnSync(join(bin, "initdb"), [...initdb, "--locale-provider=builtin", "--builtin-locale=C.UTF-8"], { encoding: "utf8" });
  if (r.status !== 0) { rmSync(data, { recursive: true, force: true }); r = spawnSync(join(bin, "initdb"), initdb, { encoding: "utf8" }); }
  if (r.status !== 0) { rmSync(d, { recursive: true, force: true }); throw new Error(`initdb failed for the private test server:\n${r.stderr}`); }
  const opts = `-k ${d} -c listen_addresses='' -c fsync=off -c synchronous_commit=off -c full_page_writes=off -c max_connections=200`;
  r = spawnSync(join(bin, "pg_ctl"), ["-D", data, "-l", join(d, "log"), "-o", opts, "-w", "start"], { encoding: "utf8" });
  if (r.status !== 0) {
    const log = existsSync(join(d, "log")) ? readFileSync(join(d, "log"), "utf8") : "";
    stop(d, bin);
    throw new Error(`could not start the private test server:\n${r.stderr}\n${log}`);
  }
  // Watchdog: when this process dies however it dies, stop the server and delete the directory.
  spawn("sh", ["-c", 'while kill -0 "$1" 2>/dev/null; do sleep 2; done; "$2/pg_ctl" -D "$3/data" -m immediate stop >/dev/null 2>&1; rm -rf "$3"',
    "omdb-pg-watchdog", String(process.pid), bin, d], { detached: true, stdio: "ignore" }).unref();
  process.on("exit", () => stop(d, bin));
  url = `postgresql://${encodeURIComponent(user)}@localhost/postgres?host=${encodeURIComponent(d)}`;
  process.env.OMARCHY_DB_TEST_ADMIN_URL = url;
  return url;
}
