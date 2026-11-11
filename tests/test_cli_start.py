"""The `foundry start` command: first-run setup, sign-in block, browser opening and shutdown.

No test opens a real browser: every in-process run passes a stub opener, and an autouse
fixture makes the real opener fail the test if it is ever reached. No test uses the network
beyond 127.0.0.1.
"""

import io
import json
import os
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
