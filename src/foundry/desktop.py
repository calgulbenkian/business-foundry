"""Run Foundry as a desktop app: a native window onto the local web interface.

The existing web application is served in this same process, on a loopback socket that the
operating system numbers (port 0), and is shown in a pywebview window. The window signs in
with a single-use launch code that exists only in this process's memory, so the user never
handles an access key.

Install with the ``desktop`` extra; the CLI, the web server and the test suite do not need it.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import hmac
import logging
import logging.handlers
import multiprocessing
import os
import secrets
import signal
import socket
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .store import Store, digest, now

APP_NAME = "Business Foundry"
LAUNCH_CODE_SECONDS = 30
# The browser cookie has no expiry and the window keeps no cookies on disk, so the session
# ends with the process; the database row is removed at shutdown. This bound only limits a
# row left behind by a crash, whose token never left the dead process's memory.
SESSION_DAYS = 30
SERVER_START_SECONDS = 15
SHUTDOWN_SECONDS = 10

log = logging.getLogger("foundry.desktop")


class DesktopError(Exception):
    """A problem to show the user in plain words; the app does not start."""


BROWSER_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Business Foundry</title></head>
<body>
<h1>Open Business Foundry from its icon</h1>
<p>This address belongs to the Business Foundry desktop app and does not open in a web browser.
Close this tab and open Business Foundry from its icon.</p>
<p>If you see this inside the Business Foundry window, quit the app and open it again.</p>
</body></html>
"""

WEBVIEW2_MISSING = (
    "Business Foundry needs the Microsoft Edge WebView2 Runtime, which is not installed on this "
    "computer. Install it from Microsoft (search for \u201cWebView2 Runtime\u201d), then open "
    "Business Foundry again."
)


# Data location ------------------------------------------------------------------------------


def default_data_dir() -> Path:
    """The per-user application data directory, never the launch folder.

    macOS: ~/Library/Application Support/Business Foundry. Windows: %LOCALAPPDATA%\\Business
    Foundry (``appauthor=False`` keeps it to one folder; ``roaming=False`` keeps a database off
    roaming profiles that are copied to servers).
    """
    from platformdirs import user_data_dir

    return Path(user_data_dir(APP_NAME, appauthor=False, roaming=False))


NETWORK_FILESYSTEMS = {
    "nfs",
    "nfs4",
    "cifs",
    "smbfs",
    "smb2",
    "smb3",
    "afpfs",
    "webdav",
    "davfs",
    "fuse.sshfs",
    "sshfs",
    "9p",
    "afs",
    "ceph",
    "glusterfs",
    "fuse.glusterfs",
    "lustre",
}


def _existing(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def _windows_is_network(path: Path) -> bool:
    text = str(path)
    if text.startswith(("\\\\", "//")):
        return True  # UNC path: \\server\share
    drive = os.path.splitdrive(text)[0]
    if not drive:
        return False
    drive_remote = 4
    return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == drive_remote


class _MacStatfs(ctypes.Structure):
    # struct statfs from <sys/mount.h>, 64-bit inode layout (the only layout on Apple silicon).
    _fields_ = [
        ("f_bsize", ctypes.c_uint32),
        ("f_iosize", ctypes.c_int32),
        ("f_blocks", ctypes.c_uint64),
        ("f_bfree", ctypes.c_uint64),
        ("f_bavail", ctypes.c_uint64),
        ("f_files", ctypes.c_uint64),
        ("f_ffree", ctypes.c_uint64),
        ("f_fsid", ctypes.c_int32 * 2),
        ("f_owner", ctypes.c_uint32),
        ("f_type", ctypes.c_uint32),
        ("f_flags", ctypes.c_uint32),
        ("f_fssubtype", ctypes.c_uint32),
        ("f_fstypename", ctypes.c_char * 16),
        ("f_mntonname", ctypes.c_char * 1024),
        ("f_mntfromname", ctypes.c_char * 1024),
        ("f_flags_ext", ctypes.c_uint32),
        ("f_reserved", ctypes.c_uint32 * 7),
    ]


MNT_LOCAL = 0x00001000


def _mac_filesystem(path: Path) -> tuple[str, bool]:
    """(filesystem type, mounted as local) for the volume holding an existing path."""
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        statfs = libc["statfs$INODE64"]  # Intel Macs; Apple silicon has only the plain name
    except AttributeError:
        statfs = libc["statfs"]
    statfs.argtypes = [ctypes.c_char_p, ctypes.POINTER(_MacStatfs)]
    result = _MacStatfs()
    if statfs(os.fsencode(path), ctypes.byref(result)) != 0:
        raise OSError(ctypes.get_errno(), "Could not inspect the storage volume", str(path))
    return result.f_fstypename.decode(errors="replace"), bool(result.f_flags & MNT_LOCAL)


def _linux_filesystem(path: Path, mounts: str) -> str:
    best, kind = "", ""
    target = str(path)
    for line in mounts.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        point = fields[1].replace("\\040", " ")
        inside = target == point or target.startswith(point.rstrip("/") + "/")
        if inside and len(point) >= len(best):
            best, kind = point, fields[2]
    return kind


def is_network_path(path: Path, platform: str | None = None) -> bool:
    """Whether the path is on a network filesystem, where SQLite's WAL mode is unsafe."""
    platform = platform or sys.platform
    if platform == "win32":
        return _windows_is_network(path)
    existing = _existing(path)
    if platform == "darwin":
        kind, local = _mac_filesystem(existing)
        return not local or kind in NETWORK_FILESYSTEMS
    try:
        mounts = Path("/proc/self/mounts").read_text()
    except OSError:
        return False
    return _linux_filesystem(existing.resolve(), mounts) in NETWORK_FILESYSTEMS


def prepare_data_dir(path: Path | None = None) -> Path:
    """Create the private data directory, refusing a network location."""
    path = Path(path).expanduser() if path else default_data_dir()
    path = Path(os.path.abspath(path))
    if is_network_path(path):
        raise DesktopError(
            f"Business Foundry keeps its records in {path}, which is on a network drive. Its "
            "database is only safe on this computer's own disk, so it has not been opened. Move "
            "your user folder to a local disk, or ask your administrator, then open the app again."
        )
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        # Owner-only, even if the folder already existed. On Windows the per-user profile's
        # access control list already restricts %LOCALAPPDATA% to its owner.
        os.chmod(path, 0o700)
    return path


# Process plumbing ---------------------------------------------------------------------------


def replace_missing_streams():
    """A windowed build has no stdout or stderr; give libraries somewhere harmless to write."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w"))  # kept open for the process


def configure_logging(data_dir: Path) -> Path:
    path = data_dir / "desktop.log"
    if not path.exists():
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    return path


@contextlib.contextmanager
def instance_lock(data_dir: Path):
    """Hold an exclusive lock for as long as this process uses the database in data_dir.

    The operating system releases the lock when the process ends, even after a crash.
    """
    path = data_dir / "desktop.lock"
    stream = open(path, "a+")  # closed below
    try:
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise DesktopError(
                "Business Foundry is already open. Switch to its window instead of opening it again."
            ) from None
        yield path
    finally:
        stream.close()


def bind_loopback() -> socket.socket:
    """A listening socket on 127.0.0.1 with a port the OS chose, held open from the start.

    The socket is passed to the server as is. Choosing a free port, closing it and binding
    again would leave a gap in which another program could take the port and receive the
    window's launch code.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # Windows otherwise lets another program bind the same port with SO_REUSEADDR.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        sock.set_inheritable(False)
    except BaseException:
        sock.close()
        raise
    return sock


# Launch-code sign-in ------------------------------------------------------------------------


class LaunchSignIn:
    """Signs in the desktop window, once, with a code that never leaves this process.

    The 256-bit code is generated here, kept only in memory and handed straight to the window.
    It is accepted once, within LAUNCH_CODE_SECONDS of being issued, and exchanged for an
    ordinary owner browser session. Sessions it created are deleted at shutdown.
    """

    def __init__(self, store: Store, host: str, *, lifetime: float = LAUNCH_CODE_SECONDS, clock=None):
        self.store, self.host = store, host
        self._clock = clock or time.monotonic
        self._lifetime = lifetime
        self._code: str | None = None
        self._issued = False
        self._deadline = 0.0
        self._lock = threading.Lock()
        self._sessions: set[str] = set()

    def __repr__(self):
        return f"LaunchSignIn(host={self.host!r})"

    @property
    def origin(self) -> str:
        return "http://" + self.host

    def issue(self) -> str:
        """Create the code, start its clock and return the window's start URL. Once only."""
        with self._lock:
            if self._issued:
                raise RuntimeError("A launch code is issued once per process.")
            self._issued = True
            self._code = secrets.token_urlsafe(32)  # 256 bits
            self._deadline = self._clock() + self._lifetime
            return f"{self.origin}/desktop/start?code={self._code}"

    def redeem(self, code: str) -> str | None:
        """Exchange the launch code for a session token; None when the code is refused."""
        with self._lock:
            if self._code is None:
                return None
            if self._clock() > self._deadline:
                self._code = None
                log.warning("Refused a launch code presented after it expired.")
                return None
            if not hmac.compare_digest(str(code).encode(), self._code.encode()):
                log.warning("Refused an incorrect launch code.")
                return None
            self._code = None
        return self._open_session()

    def _open_session(self) -> str | None:
        token = secrets.token_urlsafe(40)
        expiry = (datetime.now(UTC) + timedelta(days=SESSION_DAYS)).isoformat(timespec="microseconds")
        with self.store.write() as con:
            owner = con.execute(
                "SELECT id FROM principals WHERE role='owner' AND revoked=0 ORDER BY created_at, id LIMIT 1"
            ).fetchone()
            if not owner:
                log.error("No active owner to sign in as.")
                return None
            con.execute("DELETE FROM sessions WHERE expires_at<?", (now(),))
            con.execute("INSERT INTO sessions VALUES(?,?,?)", (digest(token), owner["id"], expiry))
        self._sessions.add(digest(token))
        log.info("Signed in the app window.")
        return token

    def end_sessions(self):
        hashes = list(self._sessions)
        if hashes:
            with self.store.write() as con:
                con.executemany("DELETE FROM sessions WHERE token_hash=?", [(h,) for h in hashes])
        self._sessions.clear()


# Server -------------------------------------------------------------------------------------


class RequestLog:
    """Log each request's method, path (never its query string) and status.

    The launch code travels in a query string, so uvicorn's access log, which records it, is
    switched off and this narrower record is written instead.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status = {}

        async def record(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, record)
        finally:
            log.info("%s %s %s", scope["method"], scope["path"], status.get("code", "-"))


class DesktopApp:
    """The local server and its sign-in, independent of any window (tested without one)."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "foundry.db"
        self.signin: LaunchSignIn | None = None
        self.server = None
        self._thread: threading.Thread | None = None
        self._stopped = False
        self._stop_lock = threading.Lock()

    @property
    def port(self) -> int:
        return int(self.signin.host.rsplit(":", 1)[1])

    def start(self) -> str:
        """Start serving; return the window's single-use start URL."""
        import uvicorn

        from .api import create_app

        store = Store(self.db_path)
        if store.bootstrap(token_file=self.data_dir / "owner.key"):
            log.info("Created a private database in %s", self.data_dir)
        sock = bind_loopback()
        address, port = sock.getsockname()[:2]
        host = f"{address}:{port}"
        self.signin = LaunchSignIn(store, host)
        app = create_app(self.db_path, allowed_hosts=["127.0.0.1"], desktop=self.signin)
        config = uvicorn.Config(
            RequestLog(app),
            log_config=None,
            access_log=False,
            timeout_graceful_shutdown=5,
            server_header=False,
        )
        self.server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self.server.run, kwargs={"sockets": [sock]}, name="foundry-server", daemon=True
        )
        self._thread.start()
        deadline = time.monotonic() + SERVER_START_SECONDS
        while not self.server.started:
            if not self._thread.is_alive() or time.monotonic() > deadline:
                sock.close()
                raise DesktopError("Business Foundry could not start its local server. See desktop.log.")
            time.sleep(0.02)
        log.info("Serving on %s", host)
        # The code's time limit starts now, when the window can actually use it.
        return self.signin.issue()

    def stop(self):
        """Stop the server, end the window's session and checkpoint the database. Idempotent."""
        with self._stop_lock:
            if self._stopped or self.server is None:
                return
            self._stopped = True
            self.server.should_exit = True
            self._thread.join(SHUTDOWN_SECONDS)
            if self._thread.is_alive():
                log.warning("The server did not stop within %s seconds.", SHUTDOWN_SECONDS)
            self.signin.end_sessions()
            con = self.signin.store.connect()
            try:
                busy, frames, done = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            finally:
                con.close()
            if busy:
                log.warning("Database checkpoint incomplete (%s of %s frames).", done, frames)
            log.info("Stopped cleanly.")


# Window -------------------------------------------------------------------------------------


def _require_edge_chromium():
    """pywebview falls back to MSHTML (Internet Explorer's engine) without WebView2; refuse."""
    if sys.platform != "win32":
        return
    from webview.guilib import initialize

    try:
        lib = initialize("edgechromium")
    except Exception as exc:
        raise DesktopError(WEBVIEW2_MISSING) from exc
    if getattr(lib, "renderer", None) != "edgechromium":
        raise DesktopError(WEBVIEW2_MISSING)


def _close_on_signals(window):
    """Ctrl+C or a termination signal closes the window, so shutdown runs the normal way.

    The GUI loop holds the main thread in native code, where Python signal handlers do not
    run, so the signals are blocked and a helper thread waits for them instead.
    """
    if not hasattr(signal, "pthread_sigmask"):
        return
    signals = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}

    def wait():
        received = signal.sigwait(signals)
        log.info("Received signal %s; closing the window.", received)
        window.events.shown.wait(SERVER_START_SECONDS)
        window.destroy()

    threading.Thread(target=wait, name="foundry-signals", daemon=True).start()


def open_window(app: DesktopApp, start_url: str):
    import webview

    _require_edge_chromium()
    webview.settings["ALLOW_DOWNLOADS"] = True  # exports and document downloads
    window = webview.create_window(APP_NAME, start_url, width=1280, height=860, min_size=(900, 600))

    def closing():
        # Runs synchronously on the GUI thread before the window closes, and also when the app
        # is quit from its menu, which ends the process without returning from webview.start.
        app.stop()

    window.events.closing += closing
    _close_on_signals(window)
    gui = "edgechromium" if sys.platform == "win32" else None
    webview.start(
        gui=gui,
        private_mode=True,  # no cookies or site data kept on disk
        storage_path=str(app.data_dir / "webview"),  # never pywebview's shared default folder
    )
    if sys.platform == "win32" and webview.renderer != "edgechromium":  # pragma: no cover
        log.error("Window used renderer %s.", webview.renderer)


def show_error(message: str):
    log.error(message)
    print("Business Foundry: " + message, file=sys.stderr)
    try:
        if sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)
            return
        import html

        import webview

        webview.create_window(
            APP_NAME,
            html=f"<!doctype html><title>{APP_NAME}</title><p>{html.escape(message)}</p>",
            width=520,
            height=220,
        )
        webview.start()
    except Exception:  # pragma: no cover - the message is already logged and printed
        log.exception("Could not display the message in a window.")


def main(argv=None):
    multiprocessing.freeze_support()
    replace_missing_streams()
    if hasattr(signal, "pthread_sigmask"):
        # Before any thread starts, so every thread inherits the mask; see _close_on_signals.
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
    parser = argparse.ArgumentParser(prog="foundry-desktop", description="Open Business Foundry.")
    parser.add_argument("--data-dir", type=Path, help="Folder for the private database (default: per-user)")
    # A Finder launch can add its own arguments; ignore anything unrecognised.
    args, _ = parser.parse_known_args(argv)
    try:
        data_dir = prepare_data_dir(args.data_dir)
        configure_logging(data_dir)
        with instance_lock(data_dir):
            app = DesktopApp(data_dir)
            try:
                url = app.start()
                open_window(app, url)
            finally:
                app.stop()
    except DesktopError as exc:
        show_error(str(exc))
        raise SystemExit(1) from None
    except Exception:
        log.exception("Business Foundry stopped unexpectedly.")
        show_error("Business Foundry stopped unexpectedly. Details are in desktop.log.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
