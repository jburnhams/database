"""An app's database settings, from its pyproject.toml:

    [tool.omarchy-db]
    app = "notes"                       # owner role name; also picks the URL (config.url_for)
    schema = "notes"                    # its schema in the shared database (default: the app name)
    models = "notes.models:Base"        # module:attribute with .metadata (a Base or a MetaData)
    migrations = "migrations"           # folder of Alembic revisions, relative to pyproject.toml
    engine = "portable"                 # or "postgres": uses Postgres-only features; tests and autogenerate then
                                        # need Postgres (dbtool and the pytest plugin follow this)

A repo that owns several schemas (one per platform, say) keeps the first app as above and adds the others as
sub-tables, each with its own models and migrations folder (same keys; `schema` defaults to the name):

    [tool.omarchy-db.apps.whatsapp]
    models = "pkg.whatsapp.models:Base"
    migrations = "migrations_whatsapp"

`Project.load(start, app="whatsapp")` picks one; without `app` it is the primary (or $OMARCHY_DB_APP), and a repo
with several apps and no primary refuses to guess. dbtool takes `-p APP`, the pytest fixtures the `db_app` fixture
or the `@pytest.mark.db_app("whatsapp")` marker.
"""
import importlib
import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import MetaData


@dataclass
class Project:
    root: Path
    app: str
    models: str
    migrations_dir: Path
    engine: str = "portable"
    schema: str = ""

    def __post_init__(self):
        self.schema = self.schema or self.app

    @staticmethod
    def _entries(cfg: dict) -> dict[str, dict]:
        """name -> settings of every app a [tool.omarchy-db] table declares: the primary one, then `apps.<name>`."""
        out = {cfg["app"]: cfg} if "app" in cfg else {}
        for name, sub in cfg.get("apps", {}).items():
            if name in out:
                raise ValueError(f"[tool.omarchy-db] declares app {name!r} twice")
            out[name] = {"app": name, **sub}
        return out

    @classmethod
    def load(cls, start: str | os.PathLike | None = None, app: str | None = None) -> "Project":
        """Find the nearest pyproject.toml with [tool.omarchy-db], from start (default: cwd) upwards. `app` (default
        $OMARCHY_DB_APP, else the primary app) says which of several apps declared there."""
        app = app or os.environ.get("OMARCHY_DB_APP") or None
        p = Path(start or os.getcwd()).resolve()
        for d in [p, *p.parents] if p.is_dir() else [p.parent, *p.parent.parents]:
            f = d / "pyproject.toml"
            if f.is_file():
                cfg = tomllib.loads(f.read_text()).get("tool", {}).get("omarchy-db")
                if cfg:
                    entries = cls._entries(cfg)
                    if app is None:
                        if "app" not in cfg and len(entries) != 1:
                            raise ValueError(f"{f} declares several apps ({', '.join(entries)}): say which (-p / app=)")
                        app = next(iter(entries))
                    if app not in entries:
                        raise ValueError(f"{f} has no app {app!r} (has: {', '.join(entries)})")
                    e = entries[app]
                    return cls(root=d, app=app, models=e["models"],
                               migrations_dir=(d / e.get("migrations", "migrations")).resolve(),
                               engine=e.get("engine", "portable"), schema=e.get("schema", app))
        raise FileNotFoundError(f"no pyproject.toml with [tool.omarchy-db] in {p} or above (dbtool init writes one)")

    @property
    def metadata(self) -> MetaData:
        for extra in (self.root / "src", self.root):
            if extra.is_dir() and str(extra) not in sys.path:
                sys.path.insert(0, str(extra))
        mod, _, attr = self.models.partition(":")
        obj = getattr(importlib.import_module(mod), attr or "Base")
        return obj if isinstance(obj, MetaData) else obj.metadata

    @property
    def postgres_only(self) -> bool:
        return self.engine == "postgres"

    def url(self) -> str:
        from .config import url_for
        return url_for(self.app, self.schema)

    def upgrade(self, engine, revision: str = "head"):
        """Migrate to head (safe at every start). On Postgres: in the app's schema, as the app's role."""
        from .migrations import upgrade
        upgrade(engine, self.migrations_dir, self.metadata, revision, owner_role=self.app, schema=self.schema)
