"""A private, throwaway PostgreSQL server for tests and scratch databases, so they never touch the real one.

When OMARCHY_DB_TEST_ADMIN_URL is not set, the first test or scratch database a process needs starts its own
cluster: `initdb` into a new temp directory, `postgres` listening only on a Unix socket in that directory (no TCP),
fsync off. It costs about a second, once per process. It is stopped and deleted when the process exits; a small
watchdog does the same if the process is killed instead (SIGKILL, a crashed test run), and the next start removes
anything a watchdog could not.

The server binaries (initdb, pg_ctl, postgres) come from PATH, `pg_config --bindir`, or /usr/lib/postgresql/*/bin.
Extensions installed system-wide (pgvector) work as usual, and you are its superuser.

The URL is exported as OMARCHY_DB_TEST_ADMIN_URL, so subprocesses (dbtool, app scripts under test) reuse it.
CI sets OMARCHY_DB_TEST_ADMIN_URL to a service container instead, and nothing is started.
"""
import atexit
import getpass
import glob
import os
import shutil
import subprocess
import tempfile
from urllib.parse import quote

PREFIX = "omdb-pg-"
_url: str | None = None


def _bindir() -> str | None:
    if shutil.which("initdb") and shutil.which("pg_ctl"):
        return os.path.dirname(shutil.which("initdb"))
    if shutil.which("pg_config"):
        try:
            d = subprocess.run(["pg_config", "--bindir"], capture_output=True, text=True, check=True).stdout.strip()
            if os.path.exists(os.path.join(d, "initdb")):
                return d
        except (OSError, subprocess.CalledProcessError):
            pass
    found = sorted(glob.glob("/usr/lib/postgresql/*/bin/initdb"), key=lambda p: int(p.split("/")[4]) if p.split("/")[4].isdigit() else 0)
    return os.path.dirname(found[-1]) if found else None


def available() -> bool:
    return _bindir() is not None


def _tmp_root() -> str:
    # A Unix socket path must fit in ~107 bytes: a long TMPDIR falls back to /tmp.
    t = tempfile.gettempdir()
    return t if len(t) < 60 else "/tmp"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _stop(d: str, bindir: str | None):
    data = os.path.join(d, "data")
    if bindir and os.path.exists(os.path.join(data, "postmaster.pid")):
        subprocess.run([os.path.join(bindir, "pg_ctl"), "-D", data, "-m", "immediate", "-w", "stop"],
                       capture_output=True)
    shutil.rmtree(d, ignore_errors=True)


def reap(bindir: str | None = None):
    """Stop and delete private servers whose owning process has gone (left by a killed run)."""
    bindir = bindir or _bindir()
    for d in glob.glob(os.path.join(_tmp_root(), PREFIX + "*")):
        try:
            with open(os.path.join(d, "owner")) as f:
                owner = int(f.read().strip())
        except (OSError, ValueError):
            continue
        if not _alive(owner):
            _stop(d, bindir)


def url() -> str:
    """Admin URL of this process's private server, starting it on first use."""
    global _url
    if _url:
        return _url
    bindir = _bindir()
    if not bindir:
        raise RuntimeError("no PostgreSQL server binaries (initdb, pg_ctl) found for a private test server: install "
                           "the PostgreSQL server package, or set OMARCHY_DB_TEST_ADMIN_URL to a throwaway server")
    reap(bindir)
    d = tempfile.mkdtemp(prefix=PREFIX, dir=_tmp_root())
    with open(os.path.join(d, "owner"), "w") as f:
        f.write(str(os.getpid()))
    data, user = os.path.join(d, "data"), getpass.getuser()
    initdb = [os.path.join(bindir, "initdb"), "-D", data, "-A", "trust", "-U", user, "--no-sync", "-E", "UTF8"]
    # Same collation as the real server (builtin C.UTF-8, PostgreSQL 17+); older initdb falls back to the default.
    r = subprocess.run(initdb + ["--locale-provider=builtin", "--builtin-locale=C.UTF-8"], capture_output=True, text=True)
    if r.returncode:
        shutil.rmtree(data, ignore_errors=True)
        r = subprocess.run(initdb, capture_output=True, text=True)
    if r.returncode:
        shutil.rmtree(d, ignore_errors=True)
        raise RuntimeError(f"initdb failed for the private test server:\n{r.stderr}")
    opts = (f"-k {d} -c listen_addresses='' -c fsync=off -c synchronous_commit=off -c full_page_writes=off "
            "-c max_connections=200")
    r = subprocess.run([os.path.join(bindir, "pg_ctl"), "-D", data, "-l", os.path.join(d, "log"), "-o", opts, "-w", "start"],
                       capture_output=True, text=True)
    if r.returncode:
        log = open(os.path.join(d, "log")).read() if os.path.exists(os.path.join(d, "log")) else ""
        _stop(d, bindir)
        raise RuntimeError(f"could not start the private test server:\n{r.stderr}\n{log}")
    # Watchdog: when this process dies however it dies, stop the server and delete the directory.
    subprocess.Popen(["sh", "-c", 'while kill -0 "$1" 2>/dev/null; do sleep 2; done; '
                                  '"$2/pg_ctl" -D "$3/data" -m immediate stop >/dev/null 2>&1; rm -rf "$3"',
                      "omdb-pg-watchdog", str(os.getpid()), bindir, d],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    owner = os.getpid()
    atexit.register(lambda: os.getpid() == owner and _stop(d, bindir))      # not in forked children
    _url = f"postgresql+psycopg://{quote(user)}@localhost/postgres?host={quote(d, safe='')}"   # also parses as a WHATWG URL (TS)
    os.environ["OMARCHY_DB_TEST_ADMIN_URL"] = _url
    return _url
