"""`foundry backup`: it copies only a Foundry database that exists, and says what it saved.

Every command here names its database with --db, except the default-path test, which first
changes to an empty temporary folder. No test runs a command without --db from the repository.
"""

import os
import shutil
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from foundry import cli
from foundry.models import BusinessCreate
from foundry.service import Service
from foundry.store import Store

PRIVATE = " This private backup includes credential hashes and business data."


def tree(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def refused(capsys, *argv):
    with pytest.raises(SystemExit) as stopped:
        cli.main(list(argv))
    captured = capsys.readouterr()
    assert captured.out == ""
    return stopped.value.code, captured.err


def real(path: Path) -> str:
    """The absolute path with every link followed, written without Path.resolve."""
    return os.path.realpath(path)


def database(path: Path, businesses: int) -> tuple[Store, Service, dict]:
    store = Store(path)
    owner = store.authenticate(store.bootstrap()["token"])
    service = Service(store)
    for number in range(businesses):
        add_business(service, owner, f"Synthetic business {number + 1}")
    return store, service, owner


def add_business(service, owner, name):
    payload = BusinessCreate(name=name, concept="A synthetic business for backup tests.")
    return service.create_business(owner, str(uuid4()), payload.model_dump(mode="json"))["id"]


def names_in(path: Path) -> list[str]:
    con = sqlite3.connect(path)
    try:
        return sorted(row[0] for row in con.execute("SELECT json_extract(data, '$.name') FROM businesses"))
    finally:
        con.close()


# R1: a missing source ------------------------------------------------------------------------


def test_a_named_database_that_does_not_exist_is_refused_and_nothing_is_created(tmp_path, capsys):
    work = tmp_path / "work"
    work.mkdir()
    code, err = refused(
        capsys, "--db", str(work / "absent" / "foundry.db"), "backup", str(work / "out" / "backup.db")
    )
    assert code == 1
    assert err == (
        f"Foundry: No database at {real(work)}/absent/foundry.db. Name the database to back up with --db.\n"
    )
    assert tree(work) == []


def test_the_default_database_is_refused_when_absent_and_not_created(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("FOUNDRY_DB", raising=False)
    monkeypatch.chdir(tmp_path)  # never the repository folder
    code, err = refused(capsys, "backup", str(tmp_path / "out" / "backup.db"))
    assert code == 1
    assert err == (
        f"Foundry: No database at {real(tmp_path)}/.foundry/foundry.db. "
        "Name the database to back up with --db.\n"
    )
    assert tree(tmp_path) == []


def test_a_database_named_by_the_environment_is_refused_when_absent(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("FOUNDRY_DB", str(tmp_path / "env" / "foundry.db"))
    code, err = refused(capsys, "backup", str(tmp_path / "out" / "backup.db"))
    assert code == 1
    assert err == (
        f"Foundry: No database at {real(tmp_path)}/env/foundry.db. Name the database to back up with --db.\n"
    )
    assert tree(tmp_path) == []


# R2: a source that is not a Foundry database ------------------------------------------------


def other_sqlite(path: Path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE notes(body TEXT)")
    con.commit()
    con.close()


SOURCES = {
    "empty file": lambda path: path.write_bytes(b""),
    "text file": lambda path: path.write_text("Synthetic notes, not a database.\n"),
    "directory": lambda path: path.mkdir(),
    "another SQLite database": other_sqlite,
}


def contents(path: Path):
    return sorted(os.listdir(path)) if path.is_dir() else path.read_bytes()


@pytest.mark.parametrize("kind", SOURCES)
def test_a_source_that_is_not_a_foundry_database_is_refused_and_left_as_it_was(tmp_path, capsys, kind):
    folder = tmp_path / "source"
    folder.mkdir()
    source = folder / "foundry.db"
    SOURCES[kind](source)
    before, listing = contents(source), os.listdir(folder)
    expected = f"Foundry: {real(source)} is not a Foundry database.\n"

    destination = tmp_path / "out" / "new" / "backup.db"
    assert refused(capsys, "--db", str(source), "backup", str(destination)) == (1, expected)
    assert contents(source) == before and os.listdir(folder) == listing
    assert not (tmp_path / "out").exists()  # no file and no folder it created
    kept = tmp_path / "kept"
    kept.mkdir()
    assert refused(capsys, "--db", str(source), "backup", str(kept / "backup.db")) == (1, expected)
    assert kept.is_dir() and os.listdir(kept) == []  # a folder that was already there stays

    restored = tmp_path / "restored" / "foundry.db"
    assert refused(capsys, "--db", str(restored), "restore", str(source)) == (1, expected)
    assert contents(source) == before and os.listdir(folder) == listing
    assert not restored.exists()


# R3: the success line -----------------------------------------------------------------------


@pytest.mark.parametrize("businesses, words", [(0, "0 businesses"), (1, "1 business"), (2, "2 businesses")])
def test_the_success_line_names_the_copy_the_source_and_the_businesses_it_holds(
    tmp_path, capsys, businesses, words
):
    source = tmp_path / "source" / "foundry.db"
    database(source, businesses)
    destination = tmp_path / "out" / "backup.db"
    cli.main(["--db", str(source), "backup", str(destination)])
    assert capsys.readouterr().out == (
        f"Saved {real(tmp_path)}/out/backup.db: a copy of {real(tmp_path)}/source/foundry.db "
        f"holding {words}. This private backup includes credential hashes and business data.\n"
    )
    assert len(names_in(destination)) == businesses


def test_the_count_is_read_from_the_finished_backup_not_from_the_source(tmp_path, capsys, monkeypatch):
    # The source gains a business just before the copy and another just after it: the copy holds
    # two, the source held one before and three after.
    source = tmp_path / "source" / "foundry.db"
    _, service, owner = database(source, 1)
    copy = cli.backup_database

    def busy_backup(src, dst):
        add_business(service, owner, "Added before the copy")
        try:
            return copy(src, dst)
        finally:
            add_business(service, owner, "Added after the copy")

    monkeypatch.setattr(cli, "backup_database", busy_backup)
    destination = tmp_path / "out" / "backup.db"
    cli.main(["--db", str(source), "backup", str(destination)])
    assert capsys.readouterr().out == (
        f"Saved {real(destination)}: a copy of {real(source)} holding 2 businesses.{PRIVATE}\n"
    )
    assert names_in(destination) == ["Added before the copy", "Synthetic business 1"]
    assert len(names_in(source)) == 3


# R4: a database in use, and one left by a stopped server ------------------------------------


def wal_only(path: Path, scratch: Path) -> bool:
    """Whether the business row is only in the -wal file: the main file alone holds none."""
    alone = scratch / "main-file-alone.db"
    shutil.copyfile(path, alone)
    try:
        return names_in(alone) == []
    finally:
        alone.unlink()


def test_a_backup_of_a_database_in_use_holds_its_committed_uncheckpointed_business(tmp_path, capsys):
    source = tmp_path / "source" / "foundry.db"
    _, service, owner = database(source, 0)
    holder = sqlite3.connect(source)  # another process's open connection: no checkpoint on close
    try:
        holder.execute("SELECT COUNT(*) FROM businesses").fetchone()
        add_business(service, owner, "Committed, in the WAL")
        assert wal_only(source, tmp_path)
        destination = tmp_path / "out" / "backup.db"
        cli.main(["--db", str(source), "backup", str(destination)])
    finally:
        holder.close()
    assert "holding 1 business." in capsys.readouterr().out
    assert names_in(destination) == ["Committed, in the WAL"]


def test_a_backup_of_a_crash_left_database_and_wal_holds_the_committed_business(tmp_path, capsys):
    source = tmp_path / "source" / "foundry.db"
    _, service, owner = database(source, 0)
    holder = sqlite3.connect(source)
    try:
        holder.execute("SELECT COUNT(*) FROM businesses").fetchone()
        add_business(service, owner, "Committed before the crash")
        left = tmp_path / "left"
        left.mkdir()
        # The database file and its -wal file, with no connection open, as a crash leaves them.
        shutil.copyfile(source, left / "foundry.db")
        shutil.copyfile(source.with_name("foundry.db-wal"), left / "foundry.db-wal")
    finally:
        holder.close()
    assert wal_only(left / "foundry.db", tmp_path)
    before = (left / "foundry.db").read_bytes()
    destination = tmp_path / "out" / "backup.db"
    cli.main(["--db", str(left / "foundry.db"), "backup", str(destination)])
    assert "holding 1 business." in capsys.readouterr().out
    assert names_in(destination) == ["Committed before the crash"]
    assert (left / "foundry.db").read_bytes() == before
