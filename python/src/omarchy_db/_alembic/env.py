# Shared Alembic environment for every app: apps have no env.py or alembic.ini of their own.
# omarchy_db.migrations passes the connection and the app's MetaData in config.attributes.
from alembic import context

cfg = context.config
connection = cfg.attributes["connection"]
metadata = cfg.attributes["metadata"]


def include_name(name, type_, parent_names):
    # Only the app's own tables count; ignore Alembic's bookkeeping table.
    return not (type_ == "table" and name == "alembic_version")


context.configure(
    connection=connection,
    target_metadata=metadata,
    # Always render batch ops: on SQLite (limited ALTER) they recreate-and-copy, on Postgres they run plain ALTERs,
    # so a revision generated against either engine runs on both.
    render_as_batch=True,
    compare_type=True,
    # SQLite reflects defaults as raw text ('false' vs the model's false() compiled as 0), so comparing them there
    # reports drift that is not real; Postgres compares them properly, and dbtool check runs on both.
    compare_server_default=connection.dialect.name != "sqlite",
    include_name=include_name,
    process_revision_directives=cfg.attributes.get("process_revision_directives"),
)
with context.begin_transaction():
    context.run_migrations()
