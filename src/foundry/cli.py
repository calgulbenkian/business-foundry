from __future__ import annotations

import argparse
import contextlib
import errno
import json
import os
import shlex
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from typing import get_args
from uuid import uuid4

from . import models
from .service import Service
from .store import DomainError, Store


def not_foundry_database(source: Path) -> str:
    return f"{source.resolve()} is not a Foundry database."


def backup_database(source: Path, destination: Path) -> int:
    """Copy the Foundry database at ``source`` to the new file ``destination``.

    The source database file is opened read-only and never created or changed; SQLite may add its
    own ``-wal`` and ``-shm`` working files beside a source in WAL mode. A source that is a folder, is
    not an SQLite database, or has no Foundry tables is refused, and the destination file is
    removed. Returns the number of businesses in the finished copy, counted from the copy.
    """
    if source.resolve() == destination.resolve():
        raise ValueError("Source and destination must differ.")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        if source.is_dir():
            raise ValueError(not_foundry_database(source))
        src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
        dst = sqlite3.connect(destination)
        try:
            try:
                check = src.execute("PRAGMA integrity_check").fetchone()[0]
            except sqlite3.DatabaseError as exc:
                if exc.sqlite_errorcode == sqlite3.SQLITE_NOTADB:
                    raise ValueError(not_foundry_database(source)) from None
                raise
            if check != "ok":
                raise ValueError("Source database failed its integrity check.")
            tables = {row[0] for row in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"metadata", "businesses"} <= tables:
                raise ValueError(not_foundry_database(source))
            try:
                version = src.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
            except sqlite3.OperationalError:  # a metadata table that is not Foundry's
                version = None
            if version is None:
                raise ValueError(not_foundry_database(source))
            if version[0] != "1":
                raise ValueError("Unsupported source database version.")
            src.backup(dst)
            return dst.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]
        finally:
            src.close()
            dst.close()
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


START_HOST = "127.0.0.1"


def port_number(text):
    try:
        value = int(text)
    except ValueError:
        value = -1
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError("must be a whole number from 1 to 65535")
    return value


def open_browser(url):
    """Open url in the default graphical browser, never in this terminal.

    Python's webbrowser module is deliberately not used: it obeys $BROWSER and, where no
    graphical session exists, falls back to text-mode browsers (lynx, w3m, links) that would
    take over the terminal. Only the platform's desktop opener is used, and on Linux and
    other Unix systems only when a graphical session is present. The opener runs in its own
    session with no access to this terminal's input or output, so whatever it launches
    cannot read from or draw on the terminal.
    """
    if sys.platform == "win32":
        os.startfile(url)  # The registered handler for http links: a desktop browser.
        return
    if sys.platform == "darwin":
        command = ["open", url]
    elif os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        command = ["xdg-open", url]
    else:
        return
    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    ).wait()


def _open_quietly(opener, url):
    try:
        opener(url)
    except Exception:
        pass  # The address is already on screen; the person can open it by hand.


def _say(text, stream):
    # A closed or broken output (for example, piped into a program that has exited) must
    # not stop the server.
    try:
        print(text, file=stream, flush=True)
    except (OSError, ValueError):
        pass


def _is_terminal(stream):
    try:
        return stream.isatty()
    except (AttributeError, ValueError, OSError):
        return False


class _HoldStops:
    """Hold Ctrl+C and SIGTERM while the workspace is set up, so setup is never cut short.

    The example business is added by many commands with fixed command keys, and a later start
    never adds it again, so an interrupted setup would leave half an example for good. A stop
    asked for during setup is recorded and acted on once setup has finished. A signal that was
    being ignored stays ignored. Signal handlers can only be set in the main thread; elsewhere
    nothing is held.
    """

    def __init__(self):
        self.asked = False
        self._previous = {}

    def _hold(self, signum, frame):
        self.asked = True

    def __enter__(self):
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                if signal.getsignal(signum) is not signal.SIG_IGN:
                    self._previous[signum] = signal.signal(signum, self._hold)
        return self

    def __exit__(self, *exc):
        for signum, handler in self._previous.items():
            signal.signal(signum, handler)


def _stop_as_ctrl_c(signum, frame):
    raise KeyboardInterrupt


def foundry_command():
    """The command a person types to run Foundry the way this process was started."""
    # uv sets UV (and UV_RUN_RECURSION_DEPTH) for the programs `uv run` starts.
    if os.environ.get("UV") or os.environ.get("UV_RUN_RECURSION_DEPTH"):
        return "uv run foundry"
    return "foundry"


def bind_local(port):
    """Return (socket, None) listening on 127.0.0.1:port, or (None, message) if that fails.

    A program already answering on the address counts as taken even where the operating
    system would let a second socket bind (on macOS, beside a wildcard listener), so Foundry
    never reports itself running at an address that serves another program.
    """
    taken = (
        f"Port {port} is already in use. Foundry may already be running at "
        f"http://{START_HOST}:{port}; open that address, or choose another port with --port."
    )
    try:
        with socket.create_connection((START_HOST, port), timeout=0.5):
            return None, taken
    except OSError:
        pass
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name != "nt":
            # Lets a restart reuse the port while connections from the last run linger in
            # TIME_WAIT. It does not allow sharing a port another socket is listening on.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((START_HOST, port))
        sock.listen(2048)
    except OSError as exc:
        sock.close()
        if exc.errno in {errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", errno.EADDRINUSE)}:
            return None, taken
        return None, f"Could not use port {port}: {exc.strerror or exc}. Choose another port with --port."
    return sock, None


def start_foundry(
    db_path,
    *,
    port=8765,
    empty=False,
    browser=True,
    token_command="foundry token",
    out=None,
    err=None,
    opener=None,
    on_started=None,
):
    """Set up a workspace on first run, serve it on 127.0.0.1, and say how to sign in.

    Returns the exit status. `opener` and `on_started` are for tests: the first replaces the
    browser launcher, the second receives the running server once the sign-in block is shown.
    """
    out = out or sys.stdout
    err = err or sys.stderr
    opener = opener or open_browser
    db_path = Path(db_path)
    token_path = db_path.parent / "owner.key"
    sock, problem = bind_local(port)
    if sock is None:
        print(problem, file=err)
        return 1
    # SIGTERM (for example `kill`, or a service manager stopping Foundry) stops it the way
    # Ctrl+C does: the same message and the same exit status. uvicorn catches SIGTERM while it
    # serves, shuts down, then raises it again, which reaches this handler.
    sigterm = None
    if threading.current_thread() is threading.main_thread():
        if signal.getsignal(signal.SIGTERM) is signal.SIG_DFL:
            sigterm = signal.signal(signal.SIGTERM, _stop_as_ctrl_c)
    try:
        with _HoldStops() as held:
            store = Store(db_path)
            created = store.bootstrap("Local owner", token_path)
            if created:
                _say(f"Created a new private workspace. Its sign-in key is saved in {token_path}", out)
                if not empty:
                    from .demo import seed

                    seed(Service(store), store.authenticate(token_path.read_text().strip()))
                    _say(
                        "Added the example business 'Polymer Geospatial · demo'. It is synthetic: "
                        "its customers, figures and receipts are made up.",
                        out,
                    )
        if held.asked:
            _say(
                "\nFoundry has stopped. It finished setting up first, so your workspace in "
                f"{db_path.parent} is complete. Run the same command again to start Foundry.",
                out,
            )
            return 0

        terminal = _is_terminal(out)
        url = f"http://{START_HOST}:{sock.getsockname()[1]}"
        try:
            key = token_path.read_text().strip()
        except FileNotFoundError:
            key_line = f"Sign-in key: the key file was not found. It was expected at {token_path}"
        except OSError as exc:
            key_line = f"Sign-in key: the key file at {token_path} could not be read ({exc.strerror})."
        else:
            if not key:
                key_line = f"Sign-in key: the key file at {token_path} is empty."
            elif terminal:
                key_line = f"Sign-in key: {key}"
            else:
                key_line = (
                    "Sign-in key: not shown, because this output is not going to a terminal. "
                    f"To show it, run '{token_command}' in a terminal, in {Path.cwd()}"
                )
        block = "\n".join(
            [
                "",
                "Business Foundry is running.",
                "",
                f"  Open this address in your web browser: {url}",
                f"  {key_line}",
                "",
                "Keep the key private. Anyone who has it can sign in as the owner of this workspace.",
                "To stop Foundry, press Ctrl+C in this window.",
                "",
            ]
        )

        import uvicorn

        from .api import create_app

        class StartServer(uvicorn.Server):
            async def startup(self, sockets=None):
                await super().startup(sockets=sockets)
                # The event loop is now serving the bound socket.
                if self.started and not self.should_exit:
                    _say(block, out)
                    if browser and terminal:
                        threading.Thread(target=_open_quietly, args=(opener, url), daemon=True).start()
                    if on_started:
                        on_started(self)

        config = uvicorn.Config(
            create_app(db_path),
            host=START_HOST,
            port=sock.getsockname()[1],
            log_level="warning",
            access_log=False,
        )
        server = StartServer(config)
        try:
            server.run(sockets=[sock])
        except KeyboardInterrupt:
            pass
        if server.started:
            _say(f"\nFoundry has stopped. Your workspace is saved in {db_path.parent}", out)
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        sock.close()
        if sigterm is not None:
            signal.signal(signal.SIGTERM, sigterm)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Business Foundry: start, acquire and operate with evidence."
    )
    parser.add_argument(
        "--db", default=os.getenv("FOUNDRY_DB", ".foundry/foundry.db"), help="Private SQLite database path"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Initialize a private database and owner key")
    init.add_argument("--name", default="Local owner")
    commands.add_parser("token", help="Print the local owner key for sign-in (keep it private)")
    commands.add_parser("demo", help="Load the explicitly synthetic Polymer demonstration")
    start = commands.add_parser(
        "start",
        help="Set up on first run, then start the web application on 127.0.0.1 and open it in a browser",
    )
    start.add_argument(
        "--port", type=port_number, default=8765, help="Port to listen on at 127.0.0.1 (default: 8765)"
    )
    start.add_argument("--empty", action="store_true", help="On first run, do not add the example business")
    start.add_argument("--no-browser", action="store_true", help="Do not open a web browser")
    serve = commands.add_parser("serve", help="Start the local web application")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    backup = commands.add_parser("backup", help="Create an atomic SQLite backup, including access keys")
    backup.add_argument("destination", type=Path)
    restore = commands.add_parser("restore", help="Restore into a new, absent database path")
    restore.add_argument("source", type=Path)
    create = commands.add_parser("create", help="Create a business from a JSON configuration")
    create.add_argument("configuration", type=Path)
    for name in ("forecast", "acquire"):
        entry = commands.add_parser(name, help="Save a financial model or acquisition analysis from JSON")
        entry.add_argument("business_id")
        entry.add_argument("configuration", type=Path)
    document = commands.add_parser("document", help="Generate a draft document from business records")
    document.add_argument("business_id")
    document.add_argument(
        "template",
        choices=[
            "business_plan",
            "structure_options",
            "contract_brief",
            "operating_manual",
            "readiness_report",
            "investment_memo",
        ],
    )
    document.add_argument("--deal-id")
    jurisdiction = commands.add_parser(
        "jurisdiction",
        help="Change the planning jurisdiction as the local owner: supersede the old guide's steps, "
        "add the new guide's steps and reopen completed work (not legal advice; no entity is moved)",
    )
    jurisdiction.add_argument("business_id")
    jurisdiction.add_argument("jurisdiction", choices=get_args(models.Jurisdiction))
    jurisdiction.add_argument("--reason", help="Why the human decision was made (required to change)")
    jurisdiction.add_argument(
        "--evidence", action="extend", nargs="+", default=[], metavar="ID", help="Reviewed evidence IDs"
    )
    jurisdiction.add_argument(
        "--expected-version", type=int, help="Business version the decision was based on (default: current)"
    )
    jurisdiction.add_argument(
        "--preview", action="store_true", help="Show the effect without changing anything"
    )
    for name in ("export", "verify"):
        command = commands.add_parser(
            name,
            help="Export business JSON" if name == "export" else "Check the event chain and record history",
        )
        command.add_argument("business_id")
    principal = commands.add_parser(
        "key", help="Create a scoped API access key; save the printed token securely"
    )
    principal.add_argument("--name", required=True)
    principal.add_argument("--role", choices=["operator", "reviewer", "viewer", "agent"], required=True)
    principal.add_argument("--business", required=True, action="append")
    commands.add_parser("mcp", help="Run the stdio agent adapter against a running Foundry server")
    args = parser.parse_args(argv)
    db_path = Path(args.db).expanduser().resolve()
    token_path = db_path.parent / "owner.key"
    try:
        if args.command == "mcp":
            from .mcp_server import main as mcp_main

            mcp_main()
            return
        if args.command == "start":
            # Printed for a person to type in another terminal, which may not share this
            # process's FOUNDRY_DB, so the database is named unless it is the built-in default.
            token_command = f"{foundry_command()} token"
            if args.db != ".foundry/foundry.db":
                token_command = f"{foundry_command()} --db {shlex.quote(args.db)} token"
            code = start_foundry(
                db_path,
                port=args.port,
                empty=args.empty,
                browser=not args.no_browser,
                token_command=token_command,
            )
            if code:
                raise SystemExit(code)
            return
        if args.command == "restore":
            backup_database(args.source, db_path)
            print(
                f"Restored {db_path}. Existing credentials remain valid; retain the original owner.key securely."
            )
            return
        if args.command == "backup":
            # Checked before anything is opened: naming no database must not create one.
            if not db_path.exists():
                raise ValueError(f"No database at {db_path}. Name the database to back up with --db.")
            # Folders this backup creates for its destination are removed again if it fails.
            created, folder = [], args.destination.resolve().parent
            while not folder.exists() and folder != folder.parent:
                created.append(folder)
                folder = folder.parent
            try:
                count = backup_database(db_path, args.destination)
            except BaseException:
                for folder in created:  # the deepest first; a folder that is not empty stays
                    with contextlib.suppress(OSError):
                        folder.rmdir()
                raise
            print(
                f"Saved {args.destination.resolve()}: a copy of {db_path} holding {count} "
                f"{'business' if count == 1 else 'businesses'}. "
                "This private backup includes credential hashes and business data."
            )
            return
        store = Store(db_path)
        service = Service(store)
        if args.command in {"init", "demo", "serve"}:
            item = store.bootstrap(getattr(args, "name", "Local owner"), token_path)
            if item:
                print(f"Created private database. Owner key saved in {token_path}", file=sys.stderr)
        if args.command == "init":
            print(f"Ready. Run 'foundry token' to retrieve the sign-in key. Database: {db_path}")
        elif args.command == "token":
            print(token_path.read_text().strip())
        elif args.command == "serve":
            import uvicorn

            from .api import create_app

            print(
                f"Open http://{args.host}:{args.port}. Retrieve the sign-in key with 'foundry token'.",
                file=sys.stderr,
            )
            uvicorn.run(create_app(db_path), host=args.host, port=args.port, log_level="info")
        else:
            actor = store.authenticate(token_path.read_text().strip())
            if args.command == "demo":
                from .demo import seed

                print(json.dumps(seed(service, actor), indent=2))
            elif args.command == "key":
                print(
                    json.dumps(
                        service.add_principal(
                            actor, {"name": args.name, "role": args.role, "business_ids": args.business}
                        ),
                        indent=2,
                    )
                )
            elif args.command in {"export", "verify"}:
                result = service.audit(actor, args.business_id, args.command == "export")
                print(json.dumps(result if args.command == "export" else result["integrity"], indent=2))
                if not result["integrity"]["valid"]:
                    raise SystemExit(1)
            elif args.command == "create":
                payload = models.BusinessCreate.model_validate_json(
                    args.configuration.read_text()
                ).model_dump(mode="json")
                print(json.dumps(service.create_business(actor, str(uuid4()), payload), indent=2))
            elif args.command in {"forecast", "acquire"}:
                model = models.ForecastCreate if args.command == "forecast" else models.DealInputs
                payload = model.model_validate_json(args.configuration.read_text()).model_dump(mode="json")
                fn = service.save_forecast if args.command == "forecast" else service.save_deal
                print(json.dumps(fn(actor, args.business_id, str(uuid4()), payload), indent=2))
            elif args.command == "jurisdiction":
                if args.preview:
                    result = service.preview_jurisdiction(actor, args.business_id, args.jurisdiction)
                else:
                    if args.reason is None:
                        raise ValueError("Give --reason for the jurisdiction decision, or use --preview.")
                    version = args.expected_version
                    if version is None:
                        version = service.snapshot(actor, args.business_id)["business"]["version"]
                    payload = models.JurisdictionChange(
                        expected_version=version,
                        jurisdiction=args.jurisdiction,
                        reason=args.reason,
                        evidence_ids=args.evidence,
                    ).model_dump(mode="json")
                    result = service.change_jurisdiction(actor, args.business_id, str(uuid4()), payload)
                print(json.dumps(result, indent=2))
            elif args.command == "document":
                payload = models.DocumentCreate(template=args.template, deal_id=args.deal_id).model_dump(
                    mode="json"
                )
                print(
                    json.dumps(
                        service.generate_document(actor, args.business_id, str(uuid4()), payload), indent=2
                    )
                )
    except (DomainError, ValueError, OSError, sqlite3.Error) as exc:
        print(f"Foundry: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
