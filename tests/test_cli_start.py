"""The `foundry start` command: first-run setup, sign-in block, browser opening and shutdown.

No test opens a real browser: every in-process run passes a stub opener, and an autouse
fixture makes the real opener fail the test if it is ever reached. No test uses the network
beyond 127.0.0.1.
"""

import io
import json
import os
import shlex
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from foundry import cli
from foundry.store import Store

EXAMPLE_NAME = "Polymer Geospatial · demo"


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture(autouse=True)
def no_real_browser(monkeypatch):
    def refuse(url):
        raise AssertionError(f"the real browser opener was called with {url}")

    monkeypatch.setattr(cli, "open_browser", refuse)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(url, **kwargs):
    with urllib.request.urlopen(urllib.request.Request(url, **kwargs), timeout=5) as response:
        return response.status, response.read().decode()


class Running:
    """Run start_foundry in a thread; the server is stopped by the context exit."""

    def __init__(self, db, out=None, err=None, **options):
        self.db, self.port = Path(db), free_port()
        self.out = out if out is not None else Terminal()
        self.err = err if err is not None else io.StringIO()
        self.options = options
        self.ready = threading.Event()
        self.server = None
        self.code = None

    def _started(self, server):
        self.server = server
        self.ready.set()

    def _run(self):
        self.code = cli.start_foundry(
            self.db, port=self.port, out=self.out, err=self.err, on_started=self._started, **self.options
        )
        self.ready.set()

    def __enter__(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        assert self.ready.wait(15), "start did not report ready"
        assert self.server is not None, self.err.getvalue()
        return self

    def __exit__(self, *exc):
        if self.server:
            self.server.should_exit = True
        self.thread.join(15)
        assert not self.thread.is_alive()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"


def business_names(db):
    with sqlite3.connect(db) as con:
        return [json.loads(row[0])["name"] for row in con.execute("SELECT data FROM businesses")]


def test_fresh_database_creates_workspace_key_and_exactly_one_example(tmp_path):
    db = tmp_path / "ws" / "foundry.db"
    with Running(db, opener=lambda url: None) as run:
        key = (tmp_path / "ws" / "owner.key").read_text().strip()
        assert key.startswith("bf_")
        assert oct((tmp_path / "ws" / "owner.key").stat().st_mode & 0o777) == "0o600"
        assert business_names(db) == [EXAMPLE_NAME]
        status, body = get(run.url + "/api/businesses", headers={"Authorization": "Bearer " + key})
        assert status == 200 and [b["data"]["name"] for b in json.loads(body)] == [EXAMPLE_NAME]
    assert run.code == 0
    text = run.out.getvalue()
    assert "Added the example business 'Polymer Geospatial · demo'. It is synthetic" in text
    assert f"Open this address in your web browser: {run.url}" in text
    assert f"Sign-in key: {key}" in text
    assert "Keep the key private." in text
    assert "press Ctrl+C" in text


def test_empty_option_creates_the_workspace_without_the_example(tmp_path):
    db = tmp_path / "foundry.db"
    with Running(db, empty=True, opener=lambda url: None) as run:
        assert (tmp_path / "owner.key").exists()
        assert business_names(db) == []
    assert "example business" not in run.out.getvalue()


def test_second_start_adds_no_example_and_keeps_the_key_file(tmp_path):
    db = tmp_path / "foundry.db"
    with Running(db, opener=lambda url: None):
        pass
    before = (tmp_path / "owner.key").read_bytes()
    with Running(db, opener=lambda url: None) as second:
        assert business_names(db) == [EXAMPLE_NAME]
    assert (tmp_path / "owner.key").read_bytes() == before
    assert "Created a new private workspace" not in second.out.getvalue()
    assert "Added the example business" not in second.out.getvalue()
    assert f"Sign-in key: {before.decode().strip()}" in second.out.getvalue()


def test_output_that_is_not_a_terminal_never_contains_the_key(tmp_path, capfd):
    calls = []
    out, err = io.StringIO(), io.StringIO()
    with Running(tmp_path / "foundry.db", out=out, err=err, opener=calls.append) as run:
        status, _ = get(run.url + "/api/health")
        assert status == 200
    key = (tmp_path / "owner.key").read_text().strip()
    captured = capfd.readouterr()
    for text in (out.getvalue(), err.getvalue(), captured.out, captured.err):
        assert key not in text
    assert "Sign-in key: not shown, because this output is not going to a terminal." in out.getvalue()
    assert "foundry token" in out.getvalue()
    assert calls == []  # not a terminal, so no browser either


def test_block_and_browser_come_only_after_the_server_is_ready(tmp_path, monkeypatch):
    """The application's startup is slowed down; nothing may be shown or opened before it ends."""
    import contextlib

    import foundry.api

    original, events = foundry.api.create_app, []

    def slow_app(db_path):
        app = original(db_path)

        @contextlib.asynccontextmanager
        async def lifespan(app):
            import asyncio

            await asyncio.sleep(0.6)
            events.append("application ready")
            yield

        app.router.lifespan_context = lifespan
        return app

    monkeypatch.setattr(foundry.api, "create_app", slow_app)

    class RecordingTerminal(Terminal):
        def write(self, text):
            if "Business Foundry is running." in text:
                events.append("block shown")
            return super().write(text)

    seen, opened, holder = [], threading.Event(), {}

    def opener(url):
        events.append("browser opened")
        try:
            answered = get(f"http://127.0.0.1:{holder['port']}/api/health")[0]
        except OSError:
            answered = None
        seen.append((url, answered))
        opened.set()

    run = Running(tmp_path / "foundry.db", out=RecordingTerminal(), opener=opener)
    holder["port"] = run.port
    with run:
        assert opened.wait(10)
        time.sleep(0.2)  # a second call would have arrived by now
    assert events == ["application ready", "block shown", "browser opened"]
    assert seen == [(f"http://127.0.0.1:{run.port}", 200)]


def test_no_browser_option_never_calls_the_opener(tmp_path):
    calls = []
    with Running(tmp_path / "foundry.db", browser=False, opener=calls.append) as run:
        time.sleep(0.2)
        assert get(run.url + "/api/health")[0] == 200
    assert calls == []


def test_an_opener_that_raises_does_not_stop_the_server(tmp_path):
    raised = threading.Event()

    def opener(url):
        raised.set()
        raise OSError("no browser here")

    with Running(tmp_path / "foundry.db", opener=opener) as run:
        assert raised.wait(10)
        time.sleep(0.1)
        assert get(run.url + "/api/health")[0] == 200
    assert run.code == 0
    assert "no browser here" not in run.out.getvalue() + run.err.getvalue()


@pytest.mark.parametrize("through_main", [False, True])
def test_port_already_taken(tmp_path, capsys, through_main):
    calls = []
    with socket.socket() as other:
        other.bind(("127.0.0.1", 0))
        other.listen()
        port = other.getsockname()[1]
        db = tmp_path / "foundry.db"
        if through_main:
            with pytest.raises(SystemExit) as stopped:
                cli.main(["--db", str(db), "start", "--port", str(port)])
            assert stopped.value.code == 1
            captured = capsys.readouterr()
            out, err = captured.out, captured.err
        else:
            out_stream, err_stream = Terminal(), io.StringIO()
            code = cli.start_foundry(db, port=port, out=out_stream, err=err_stream, opener=calls.append)
            assert code == 1
            out, err = out_stream.getvalue(), err_stream.getvalue()
    assert f"Port {port} is already in use" in err
    assert f"Foundry may already be running at http://127.0.0.1:{port}" in err
    assert "--port" in err
    assert "Sign-in key" not in out + err and "bf_" not in out + err
    assert "running." not in out
    assert calls == []
    # Nothing was set up for a run that could not start.
    assert not (tmp_path / "owner.key").exists()


def test_missing_key_file_still_starts_with_a_plain_message(tmp_path, capfd):
    db = tmp_path / "restored" / "foundry.db"
    Store(db).bootstrap("Local owner")  # a workspace whose key file is elsewhere
    with Running(db, opener=lambda url: None) as run:
        assert get(run.url + "/")[0] == 200
    assert run.code == 0
    expected = tmp_path / "restored" / "owner.key"
    assert f"Sign-in key: the key file was not found. It was expected at {expected}" in run.out.getvalue()
    captured = capfd.readouterr()
    for text in (run.out.getvalue(), run.err.getvalue(), captured.out, captured.err):
        assert "Traceback" not in text


def test_request_lines_are_quiet_but_server_errors_still_appear(tmp_path, monkeypatch, capfd):
    import foundry.api

    original = foundry.api.create_app

    def with_failing_route(db_path):
        app = original(db_path)

        @app.get("/api/synthetic-failure")
        def fail():
            raise RuntimeError("synthetic failure for the logging test")

        return app

    monkeypatch.setattr(foundry.api, "create_app", with_failing_route)
    with Running(tmp_path / "foundry.db", opener=lambda url: None) as run:
        assert get(run.url + "/")[0] == 200
        with pytest.raises(urllib.error.HTTPError) as failed:
            get(run.url + "/api/synthetic-failure")
        assert failed.value.code == 500
        time.sleep(0.1)
    captured = capfd.readouterr()
    logged = captured.out + captured.err
    assert "GET / HTTP/1.1" not in logged
    assert "Uvicorn running" not in logged
    assert "Traceback" in logged
    assert "RuntimeError: synthetic failure for the logging test" in logged


def test_port_option_rejects_numbers_outside_the_port_range(tmp_path, capsys):
    for value in ("0", "65536", "web"):
        with pytest.raises(SystemExit) as stopped:
            cli.main(["--db", str(tmp_path / "foundry.db"), "start", "--port", value])
        assert stopped.value.code == 2
    assert not (tmp_path / "foundry.db").exists()


@pytest.mark.skipif(os.name == "nt", reason="sends SIGINT, as Ctrl+C does in a POSIX terminal")
def test_end_to_end_start_sign_in_and_interrupt(tmp_path):
    db, port = tmp_path / "foundry.db", free_port()
    origin = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        [sys.executable, "-m", "foundry.cli", "--db", str(db), "start", "--port", str(port), "--no-browser"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        # A terminal delivers Ctrl+C to a process whose SIGINT is at its default; a test
        # runner may have it ignored, which the child would otherwise inherit.
        preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
    )
    chunks = []
    reader = threading.Thread(target=lambda: chunks.extend(iter(process.stdout.readline, b"")), daemon=True)
    reader.start()
    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                if get(origin + "/api/health")[0] == 200:
                    break
            except OSError:
                pass
            assert time.monotonic() < deadline and process.poll() is None, b"".join(chunks).decode()
            time.sleep(0.1)
        status, page = get(origin + "/")
        assert status == 200 and "Finding your key" in page
        key = (tmp_path / "owner.key").read_text().strip()
        request = urllib.request.Request(
            origin + "/api/login",
            data=json.dumps({"token": key}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200
            assert json.loads(response.read())["role"] == "owner"
            assert "foundry_session=" in response.headers["Set-Cookie"]
        process.send_signal(signal.SIGINT)
        assert process.wait(timeout=8) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    reader.join(5)
    output = b"".join(chunks).decode()
    assert "Business Foundry is running." in output
    assert "Foundry has stopped." in output
    assert "Traceback" not in output and "KeyboardInterrupt" not in output
    assert key not in output


def test_existing_commands_keep_their_output(tmp_path, capsys, monkeypatch):
    """Expected text copied from the commands as they behave on main (45a07e9)."""
    db = tmp_path / "foundry.db"
    key_file = tmp_path / "owner.key"
    cli.main(["--db", str(db), "init"])
    captured = capsys.readouterr()
    assert captured.err == f"Created private database. Owner key saved in {key_file}\n"
    assert captured.out == f"Ready. Run 'foundry token' to retrieve the sign-in key. Database: {db}\n"

    cli.main(["--db", str(db), "token"])
    captured = capsys.readouterr()
    assert captured.out == key_file.read_text() and captured.err == ""

    cli.main(["--db", str(db), "demo"])
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["data"]["name"] == EXAMPLE_NAME
    assert captured.out.startswith('{\n  "id": "business_')

    import uvicorn

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(kw))
    cli.main(["--db", str(db), "serve", "--port", "9999"])
    captured = capsys.readouterr()
    assert captured.err == "Open http://127.0.0.1:9999. Retrieve the sign-in key with 'foundry token'.\n"
    assert captured.out == ""
    assert calls == [{"host": "127.0.0.1", "port": 9999, "log_level": "info"}]


def test_port_option_says_what_it_does(capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main(["start", "--help"])
    assert stopped.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "--port PORT Port to listen on at 127.0.0.1 (default: 8765)" in help_text


def wait_for_health(process, origin, chunks):
    deadline = time.monotonic() + 15
    while True:
        try:
            if get(origin + "/api/health")[0] == 200:
                return
        except OSError:
            pass
        assert time.monotonic() < deadline and process.poll() is None, b"".join(chunks).decode()
        time.sleep(0.1)


def spawn_start(arguments, *, cwd, env=None):
    """Run `foundry start` as a separate process with its output in a pipe, not a terminal."""
    port = free_port()
    process = subprocess.Popen(
        [sys.executable, "-m", "foundry.cli", *arguments, "start", "--port", str(port), "--no-browser"],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        # As a terminal leaves them: a test runner may have Ctrl+C ignored, which the child
        # would otherwise inherit.
        preexec_fn=None if os.name == "nt" else lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
    )
    chunks = []
    reader = threading.Thread(target=lambda: chunks.extend(iter(process.stdout.readline, b"")), daemon=True)
    reader.start()
    return process, f"http://127.0.0.1:{port}", chunks, reader


def stop(process, reader, chunks, signum=signal.SIGINT):
    try:
        process.send_signal(signum)
        code = process.wait(timeout=8)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    reader.join(5)
    return code, b"".join(chunks).decode()


def child_env(**changes):
    env = {k: v for k, v in os.environ.items() if k not in {"FOUNDRY_DB", "UV", "UV_RUN_RECURSION_DEPTH"}}
    env.update(changes)
    return env


@pytest.mark.parametrize(
    ("uv", "program"), [(None, "foundry"), ("/synthetic/bin/uv", "uv run foundry")], ids=["plain", "uv-run"]
)
def test_foundry_command_names_uv_run_only_under_uv(monkeypatch, uv, program):
    monkeypatch.delenv("UV", raising=False)
    monkeypatch.delenv("UV_RUN_RECURSION_DEPTH", raising=False)
    if uv:
        monkeypatch.setenv("UV", uv)
    assert cli.foundry_command() == program


@pytest.mark.skipif(os.name == "nt", reason="stops the server with SIGINT")
@pytest.mark.parametrize(
    ("arguments", "env", "program"),
    [
        ([], {}, "foundry"),
        (["--db", "work space/foundry.db"], {"UV": "/synthetic/bin/uv"}, "uv run foundry"),
        ([], {"FOUNDRY_DB": "from-env/foundry.db", "UV_RUN_RECURSION_DEPTH": "1"}, "uv run foundry"),
    ],
    ids=["default", "relative-db-under-uv", "db-from-environment"],
)
def test_piped_output_prints_a_token_command_that_works_as_printed(tmp_path, arguments, env, program):
    folder = tmp_path / "started here"
    folder.mkdir()
    process, origin, chunks, reader = spawn_start(arguments, cwd=folder, env=child_env(**env))
    try:
        wait_for_health(process, origin, chunks)
    finally:
        code, output = stop(process, reader, chunks)
    assert code == 0
    lines = [line for line in output.splitlines() if "Sign-in key:" in line]
    assert len(lines) == 1, output
    prefix = "  Sign-in key: not shown, because this output is not going to a terminal. To show it, run '"
    assert lines[0].startswith(prefix), lines[0]
    command, _, rest = lines[0][len(prefix) :].partition("' in a terminal, in ")
    assert rest == str(folder)
    assert command.startswith(program + " ")
    # Run it as printed, in the folder it names, from a terminal that has none of the starting
    # process's settings. Only the program words are replaced, by this checkout's Foundry.
    words = shlex.split(command)[len(program.split()) :]
    shown = subprocess.run(
        [sys.executable, "-m", "foundry.cli", *words],
        cwd=rest,
        env=child_env(),
        capture_output=True,
        text=True,
    )
    assert shown.returncode == 0, shown.stderr
    key_files = list(tmp_path.rglob("owner.key"))
    assert len(key_files) == 1
    assert shown.stdout == key_files[0].read_text()
    assert key_files[0].read_text().strip() not in output


@pytest.mark.skipif(os.name == "nt", reason="SIGTERM ends a Windows process without running handlers")
def test_sigterm_stops_start_as_ctrl_c_does(tmp_path):
    results = {}
    for name, signum in (("Ctrl+C", signal.SIGINT), ("SIGTERM", signal.SIGTERM)):
        db = tmp_path / name / "foundry.db"
        process, origin, chunks, reader = spawn_start(["--db", str(db)], cwd=tmp_path, env=child_env())
        try:
            wait_for_health(process, origin, chunks)
        finally:
            results[name] = stop(process, reader, chunks, signum)
    for name, (code, output) in results.items():
        assert code == 0, (name, output)
        last = output.rstrip().splitlines()[-1]
        assert last == f"Foundry has stopped. Your workspace is saved in {tmp_path / name}", (name, output)
        assert "Traceback" not in output and "KeyboardInterrupt" not in output, (name, output)


def workspace_summary(db):
    """What the example consists of, without its random ids and times."""
    with sqlite3.connect(db) as con:
        objects = sorted(con.execute("SELECT kind, title, state, version FROM objects").fetchall())
        events = sorted(row[0] for row in con.execute("SELECT action FROM events"))
        commands = sorted(row[0] for row in con.execute("SELECT command_key FROM commands"))
    commands = [key for key in commands if not key.startswith("demo:task:")]  # these name task ids
    return business_names(db), objects, events, commands


def _interrupt(signum, frame):
    raise KeyboardInterrupt


@pytest.mark.skipif(os.name == "nt", reason="raises SIGTERM, which Windows does not deliver to handlers")
@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM], ids=["Ctrl+C", "SIGTERM"])
def test_a_stop_during_first_setup_waits_for_the_whole_example(tmp_path, monkeypatch, signum):
    from foundry.demo import seed
    from foundry.service import Service

    reference = Store(tmp_path / "reference" / "foundry.db")
    reference.bootstrap("Local owner", tmp_path / "reference" / "owner.key")
    seed(
        Service(reference), reference.authenticate((tmp_path / "reference" / "owner.key").read_text().strip())
    )

    # The stop arrives part-way through the example, from inside setup itself, so the test
    # does not depend on when a signal from outside would land.
    original, raised = Service.save_forecast, []

    def interrupted(self, *args, **kwargs):
        raised.append(signum)
        signal.raise_signal(signum)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Service, "save_forecast", interrupted)
    # Both signals interrupt here, as Ctrl+C does in a terminal, whatever the test runner set.
    previous = {s: signal.signal(s, _interrupt) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        out = Terminal()
        db = tmp_path / "ws" / "foundry.db"
        code = cli.start_foundry(db, port=free_port(), out=out, err=io.StringIO(), opener=lambda url: None)
        after = {s: signal.getsignal(s) for s in previous}
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)
    assert raised == [signum]
    assert code == 0
    assert after == {signal.SIGINT: _interrupt, signal.SIGTERM: _interrupt}  # put back as they were
    assert workspace_summary(db) == workspace_summary(reference.path)
    text = out.getvalue()
    assert "Added the example business 'Polymer Geospatial · demo'." in text
    assert "Business Foundry is running." not in text
    assert text.rstrip().endswith(
        f"Foundry has stopped. It finished setting up first, so your workspace in {db.parent} is complete. "
        "Run the same command again to start Foundry."
    )
    monkeypatch.setattr(Service, "save_forecast", original)
    with Running(db, opener=lambda url: None) as second:
        assert business_names(db) == [EXAMPLE_NAME]
    assert "Added the example business" not in second.out.getvalue()
    assert workspace_summary(db) == workspace_summary(reference.path)


@pytest.mark.skipif(os.name == "nt", reason="ignores SIGINT through POSIX signal dispositions")
def test_an_ignored_ctrl_c_during_setup_stays_ignored(tmp_path, monkeypatch):
    from foundry.service import Service

    original = Service.save_forecast

    def interrupted(self, *args, **kwargs):
        signal.raise_signal(signal.SIGINT)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Service, "save_forecast", interrupted)
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    served = []
    try:
        out = Terminal()

        def started(server):
            served.append(True)
            server.should_exit = True

        code = cli.start_foundry(
            tmp_path / "foundry.db", port=free_port(), out=out, opener=lambda url: None, on_started=started
        )
        after = signal.getsignal(signal.SIGINT)
    finally:
        signal.signal(signal.SIGINT, previous)
    assert code == 0 and served == [True]
    assert after is signal.SIG_IGN
    assert "Business Foundry is running." in out.getvalue()
