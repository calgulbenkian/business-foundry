"""`foundry backup`: it copies only a Foundry database that exists, and says what it saved.

Every command here names its database with --db, except the default-path test, which first
changes to an empty temporary folder. No test runs a command without --db from the repository.
"""

import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from conftest import fake_uv

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


def test_a_wal_mode_database_that_is_not_foundrys_is_refused_and_only_sqlite_adds_files(tmp_path, capsys):
    # Opening a database in WAL mode to read it makes SQLite add its own -wal and -shm files
    # beside it (measured on SQLite 3.50.4 and 3.53.1). Foundry adds nothing, and the database
    # file itself is not changed.
    source = tmp_path / "source" / "foundry.db"
    source.parent.mkdir()
    con = sqlite3.connect(source)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE notes(body TEXT)")
    con.execute("INSERT INTO notes VALUES ('Synthetic note.')")
    con.commit()
    con.close()  # closed cleanly: SQLite removes its -wal and -shm files
    assert tree(tmp_path) == ["source", "source/foundry.db"]
    before = source.read_bytes()

    destination = tmp_path / "out" / "new" / "backup.db"
    assert refused(capsys, "--db", str(source), "backup", str(destination)) == (
        1,
        f"Foundry: {real(source)} is not a Foundry database.\n",
    )
    assert tree(tmp_path) == ["source", "source/foundry.db", "source/foundry.db-shm", "source/foundry.db-wal"]
    assert source.read_bytes() == before


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


class _Patched:
    """A module with some attributes replaced; every other attribute is the real module's."""

    def __init__(self, real, **replaced):
        self._real, self._replaced = real, replaced

    def __getattr__(self, name):
        return self._replaced[name] if name in self._replaced else getattr(self._real, name)


def test_backup_database_counts_the_businesses_in_the_copy_it_made(tmp_path, capsys, monkeypatch):
    # The source loses a business through another connection just before the copy is made, and
    # another just after. The copy holds one: the source held two before the copy and none after.
    source = tmp_path / "source" / "foundry.db"
    database(source, 2)

    def remove_one():
        other = sqlite3.connect(source)
        try:
            other.execute("DELETE FROM businesses WHERE rowid = (SELECT MIN(rowid) FROM businesses)")
            other.commit()
        finally:
            other.close()

    class Busy(sqlite3.Connection):
        def backup(self, target, *args, **kwargs):
            remove_one()
            try:
                return super().backup(target, *args, **kwargs)
            finally:
                remove_one()

    def connect(*args, **kwargs):
        return sqlite3.connect(*args, factory=Busy, **kwargs)

    monkeypatch.setattr(cli, "sqlite3", _Patched(sqlite3, connect=connect))
    destination = tmp_path / "out" / "backup.db"
    cli.main(["--db", str(source), "backup", str(destination)])
    assert capsys.readouterr().out == (
        f"Saved {real(destination)}: a copy of {real(source)} holding 1 business.{PRIVATE}\n"
    )
    assert names_in(destination) == ["Synthetic business 2"]
    assert names_in(source) == []


def test_the_success_line_names_resolved_paths_for_relative_arguments(tmp_path, capsys, monkeypatch):
    database(tmp_path / "work" / "foundry.db", 1)
    monkeypatch.chdir(tmp_path)  # never the repository folder
    cli.main(["--db", "work/foundry.db", "backup", "out/a.db"])
    assert capsys.readouterr().out == (
        f"Saved {real(tmp_path)}/out/a.db: a copy of {real(tmp_path)}/work/foundry.db "
        f"holding 1 business.{PRIVATE}\n"
    )
    assert names_in(tmp_path / "out" / "a.db") == ["Synthetic business 1"]


def test_restore_names_a_relative_source_that_is_not_foundrys_by_its_resolved_path(
    tmp_path, capsys, monkeypatch
):
    other_sqlite(tmp_path / "plain.db")
    before = (tmp_path / "plain.db").read_bytes()
    monkeypatch.chdir(tmp_path)  # never the repository folder
    assert refused(capsys, "--db", "new/foundry.db", "restore", "plain.db") == (
        1,
        f"Foundry: {real(tmp_path)}/plain.db is not a Foundry database.\n",
    )
    assert (tmp_path / "plain.db").read_bytes() == before
    assert not (tmp_path / "new" / "foundry.db").exists()


def test_a_database_with_foundrys_metadata_but_no_businesses_table_is_refused(tmp_path, capsys):
    folder = tmp_path / "source"
    folder.mkdir()
    source = folder / "foundry.db"
    con = sqlite3.connect(source)
    con.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT INTO metadata VALUES ('schema_version', '1')")
    con.commit()
    con.close()
    before = source.read_bytes()
    destination = tmp_path / "out" / "backup.db"
    assert refused(capsys, "--db", str(source), "backup", str(destination)) == (
        1,
        f"Foundry: {real(source)} is not a Foundry database.\n",
    )
    assert source.read_bytes() == before and os.listdir(folder) == ["foundry.db"]
    assert not destination.exists() and not (tmp_path / "out").exists()


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


# The README's macOS example ---------------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="a POSIX shell line")
def test_the_readme_macos_backup_line_runs_when_pasted(tmp_path):
    readme = (Path(__file__).parents[1] / "README.md").read_text()
    [line] = re.findall(r"`(uv run foundry --db \"\$HOME/Library/[^`]*)`", readme)
    assert line == (
        'uv run foundry --db "$HOME/Library/Application Support/Business Foundry/foundry.db" '
        "backup /private/path/backup.db"
    )
    home = tmp_path / "home"  # never the real per-user folder
    database(home / "Library" / "Application Support" / "Business Foundry" / "foundry.db", 1)
    destination = tmp_path / "out" / "backup.db"
    work = tmp_path / "work"
    work.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "FOUNDRY_DB"}
    env["HOME"] = str(home)
    env["PATH"] = str(fake_uv(tmp_path / "tools")) + os.pathsep + env["PATH"]
    pasted = line.replace("/private/path/backup.db", str(destination))
    result = subprocess.run(["/bin/sh", "-c", pasted], cwd=work, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        f"Saved {real(destination)}: a copy of {real(home)}/Library/Application Support/Business Foundry/"
        f"foundry.db holding 1 business.{PRIVATE}\n"
    )
    assert names_in(destination) == ["Synthetic business 1"] and os.listdir(work) == []
