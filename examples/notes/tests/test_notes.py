"""The example app's tests. They run on SQLite by default and on a throwaway Postgres with `pytest --pg`;
the db_session / db_engine / db_url fixtures come from the omarchy_db pytest plugin."""
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from omarchy_db import check_migrations, connect, drift, session
from notes.models import Author, Base, Note


def test_records_round_trip(db_session):
    ada = Author(name="Ada")
    db_session.add_all([Note(text="first", author=ada), Note(text="second", author=ada, pinned=True)])
    db_session.commit()
    rows = db_session.scalars(select(Note).order_by(Note.id)).all()
    assert [(n.text, n.pinned, n.author.name) for n in rows] == [("first", False, "Ada"), ("second", True, "Ada")]
    assert rows[0].created_at is not None                       # server default


def test_server_default_fills_rows_written_without_the_column(db_engine):
    with db_engine.begin() as c:                                 # as old code or a raw insert would
        c.exec_driver_sql("INSERT INTO notes (text) VALUES ('raw')")
    with session(db_engine) as s:
        assert s.scalars(select(Note.pinned)).one() is False


def test_constraints_are_enforced(db_session):
    db_session.add_all([Author(name="Bob"), Author(name="Bob")])
    with pytest.raises(IntegrityError):                          # unique
        db_session.commit()
    db_session.rollback()
    with db_session.bind.begin() as c, pytest.raises(IntegrityError):   # foreign key (SQLite: PRAGMA foreign_keys)
        c.exec_driver_sql("INSERT INTO notes (text, author_id) VALUES ('x', 999)")


def test_each_test_gets_a_fresh_database(db_session):
    assert db_session.scalars(select(Note)).all() == []


def test_own_engine_from_url(db_url):
    eng = connect(db_url)
    with session(eng) as s:
        s.add(Note(text="via url")); s.commit()
        assert s.scalars(select(Note.text)).all() == ["via url"]
    eng.dispose()


def test_app_code_that_connects_by_itself(db_env):
    """db_env points the app's own connect("notes") at this test's database (as an app's CLI or store would)."""
    eng = connect("notes")
    with session(eng) as s:
        s.add(Note(text="via app")); s.commit()
        assert s.scalars(select(Note.text)).all() == ["via app"]
    eng.dispose()


def test_without_db_env_the_shared_database_is_refused(monkeypatch):
    from omarchy_db import LiveDatabaseRefused
    monkeypatch.delenv("OMARCHY_DB_URL", raising=False)
    monkeypatch.delenv("NOTES_DATABASE_URL", raising=False)
    with pytest.raises(LiveDatabaseRefused):
        connect("notes")


def test_schema_matches_models(db_engine):
    assert drift(db_engine, Base.metadata) == []


def test_migrations_from_empty_and_back(db_backend, db_project, tmp_path):
    """Upgrade an empty database to head, compare with the models, downgrade to base, upgrade again."""
    from omarchy_db.scratch import scratch_database
    from sqlalchemy import create_engine
    with scratch_database(db_backend == "postgres") as url:
        eng = connect(url) if url.startswith("sqlite") else create_engine(url)
        assert check_migrations(eng, db_project.migrations_dir, Base.metadata) == []
        eng.dispose()
