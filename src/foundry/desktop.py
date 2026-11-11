"""Run Foundry as a desktop app: a native window onto the local web interface.

The existing web application is served in this same process, on a loopback socket that the
operating system numbers (port 0), and is shown in a pywebview window. The window signs in
with a single-use launch code that exists only in this process's memory, so the user never
handles an access key.

Install with the ``desktop`` extra; the CLI, the web server and the test suite do not need it.
"""

from __future__ import annotations

import argparse
import atexit
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
# How long the window has to use a launch code, counted from the moment it is told to open the
# start address. On macOS the first request arrived 11 seconds after server start on a cold
# first launch of the Qt engine; a first WebView2 start on a slow Windows PC, with antivirus scanning a new program, can take
# far longer. The duration adds little protection: the code is 256 random bits, single use,
# held only in memory and handed straight to the window. So the limit is generous, and a window
# that still misses it is sent a fresh code (up to LAUNCH_ATTEMPTS in all) instead of a dead end.
LAUNCH_CODE_SECONDS = 180
LAUNCH_ATTEMPTS = 3
LAUNCH_GRACE_SECONDS = 5
# How long to wait for the window to show its first page before sending it the start address
# anyway. The launch code's clock starts only then.
WINDOW_READY_SECONDS = 120
# The browser cookie has no expiry and the window keeps no cookies on disk, so the session
# ends with the process; the database row is removed at shutdown. This bound only limits a
# row left behind by a crash, whose token never left the dead process's memory.
SESSION_DAYS = 30
SERVER_START_SECONDS = 15
SHUTDOWN_SECONDS = 10
# After a termination signal, how long the window has to close the normal way before the app
# cleans up and exits without it.
SIGNAL_EXIT_SECONDS = 20

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

OPENING_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Business Foundry</title></head>
<body><p>Opening Business Foundry\u2026</p></body></html>
"""

SIGN_IN_FAILED_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Business Foundry</title></head>
<body>
<h1>Business Foundry could not open your records</h1>
<p>The window could not sign in. Quit Business Foundry and open it again.</p>
<p>If this keeps happening, the details are in desktop.log in the Business Foundry data
folder.</p>
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
    # httpx logs every request's full URL at INFO. Nothing in the app sends the launch code
    # through httpx, but keep any URL with a query string out of the file regardless.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return path


@contextlib.contextmanager
def instance_lock(data_dir: Path):
    """Hold an exclusive lock for as long as this process uses the database in data_dir.

    The operating system releases the lock when the process ends, even after a crash.
    """
    path = data_dir / "desktop.lock"
    stream = os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR, 0o600), "r+")  # closed below
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
    """Signs in the desktop window with a code that never leaves this process.

    Each 256-bit code is generated here, kept only in memory and handed straight to the window.
    At most one code is valid at a time: issuing a new one replaces the last. A code is accepted
    once, within LAUNCH_CODE_SECONDS of being issued, and exchanged for an ordinary owner
    browser session. At most LAUNCH_ATTEMPTS codes are issued per process, none after the
    window has signed in. Sessions it created are deleted at shutdown.

    Whoever waits on ``wait`` learns how the current code ended: the window signed in, the code
    was presented but refused, or the app is closing.
    """

    SIGNED_IN, REFUSED, CLOSED = "signed in", "refused", "closed"

    def __init__(
        self,
        store: Store,
        host: str,
        *,
        lifetime: float = LAUNCH_CODE_SECONDS,
        attempts: int = LAUNCH_ATTEMPTS,
        clock=None,
    ):
        self.store, self.host = store, host
        self.lifetime = lifetime
        self._clock = clock or time.monotonic
        self._attempts = attempts
        self._issued = 0
        self._code: str | None = None
        self._deadline = 0.0
        self._outcome: str | None = None
        self._finished = False  # signed in or closed: no more codes
        self._changed = threading.Condition()
        self._sessions: set[str] = set()

    def __repr__(self):
        return f"LaunchSignIn(host={self.host!r})"

    @property
    def origin(self) -> str:
        return "http://" + self.host

    def issue(self) -> str:
        """Create a new code, start its clock and return the window's start URL.

        Call it as the window is told to navigate, so that the window, not the server's start,
        uses the time. Any earlier code stops working.
        """
        with self._changed:
            if self._finished:
                raise RuntimeError("The window has already signed in, or the app is closing.")
            if self._issued >= self._attempts:
                raise RuntimeError(f"Only {self._attempts} launch codes are issued per process.")
            self._issued += 1
            self._code = secrets.token_urlsafe(32)  # 256 bits
            self._deadline = self._clock() + self.lifetime
            self._outcome = None
            log.info("Issued launch code %s of %s.", self._issued, self._attempts)
            return f"{self.origin}/desktop/start?code={self._code}"

    def redeem(self, code: str) -> str | None:
        """Exchange the current launch code for a session token; None when it is refused."""
        with self._changed:
            current = self._code
            if current is None or not hmac.compare_digest(str(code).encode(), current.encode()):
                # A guess, or a code that has been used or replaced. The current code, if any,
                # is not spent: a wrong guess must not lock the window out.
                log.warning("Refused a launch code that is not the current one.")
                return None
            self._code = None  # used once, whether or not it is in time
            if self._clock() > self._deadline:
                log.warning("Refused a launch code presented after it expired.")
                self._finish_attempt(self.REFUSED)
                return None
        token = self._open_session()
        with self._changed:
            self._finish_attempt(self.SIGNED_IN if token else self.REFUSED)
        return token

    def _finish_attempt(self, outcome: str):
        self._outcome = outcome
        if outcome == self.SIGNED_IN:
            self._finished = True
        self._changed.notify_all()

    def wait(self, timeout: float) -> str | None:
        """How the current code ended, or None if nothing happened within timeout seconds."""
        with self._changed:
            self._changed.wait_for(lambda: self._outcome is not None, timeout)
            return self._outcome

    def close(self):
        """Refuse every code from now on and wake anyone waiting."""
        with self._changed:
            self._code = None
            self._finished = True
            self._outcome = self.CLOSED
            self._changed.notify_all()

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


def sign_in_window(signin: LaunchSignIn, navigate, give_up) -> bool:
    """Send the window launch codes until it signs in, at most LAUNCH_ATTEMPTS times.

    ``navigate(url)`` points the window at a start URL; ``give_up()`` shows it a final message.
    The server reports a code that arrived late or was refused, and a code that never arrived
    is replaced once its time is up, so a slow window gets a fresh code instead of a dead end.
    Returns whether the window signed in.
    """
    attempt = 0
    while True:
        try:
            url = signin.issue()
        except RuntimeError:
            break  # closing, or out of codes
        attempt += 1
        navigate(url)
        # A little longer than the code's lifetime, so a request that arrived just in time can
        # finish opening its session.
        outcome = signin.wait(signin.lifetime + LAUNCH_GRACE_SECONDS)
        if outcome == signin.SIGNED_IN:
            return True
        if outcome == signin.CLOSED:
            return False
        reason = "its code was refused" if outcome == signin.REFUSED else "its code was not used in time"
        log.warning("The window did not sign in (attempt %s): %s.", attempt, reason)
    if signin.wait(0) != signin.CLOSED:
        log.error("The window could not sign in after %s launch codes.", attempt)
        give_up()
    return False


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

    def __init__(self, data_dir: Path, *, launch_seconds: float = LAUNCH_CODE_SECONDS):
        self.data_dir = Path(data_dir)
        self.launch_seconds = launch_seconds
        self.db_path = self.data_dir / "foundry.db"
        self.signin: LaunchSignIn | None = None
        self.server = None
        self._thread: threading.Thread | None = None
        self._stopped = False
        self._stop_lock = threading.Lock()

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def port(self) -> int:
        return int(self.signin.host.rsplit(":", 1)[1])

    def start(self) -> LaunchSignIn:
        """Start serving; return the sign-in that issues the window's launch codes."""
        import uvicorn

        from .api import create_app

        store = Store(self.db_path)
        if store.bootstrap(token_file=self.data_dir / "owner.key"):
            log.info("Created a private database in %s", self.data_dir)
        sock = bind_loopback()
        address, port = sock.getsockname()[:2]
        host = f"{address}:{port}"
        self.signin = LaunchSignIn(store, host, lifetime=self.launch_seconds)
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
        if os.getenv("FOUNDRY_PUBLIC_ORIGIN"):
            log.info(
                "FOUNDRY_PUBLIC_ORIGIN is set; the desktop app ignores it and accepts writes only "
                "from its own window at %s.",
                self.signin.origin,
            )
        # No code yet: it is issued as the window is told to open it (see sign_in_window).
        return self.signin

    def stop(self):
        """Stop the server, end the window's session and checkpoint the database. Idempotent."""
        with self._stop_lock:
            if self._stopped or self.server is None:
                return
            self._stopped = True
            self.signin.close()
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


class SignalShutdown:
    """Turn Ctrl+C and termination signals into the app's normal shutdown.

    A Python handler is installed for each signal, which makes the signal's disposition "caught"
    for the whole process, and ``signal.set_wakeup_fd`` makes Python's low-level handler write
    the signal number to a socket. That happens on whichever thread the operating system picks,
    including threads started by other code before ``main`` (a packaged app's bootstrap, or a
    GUI toolkit), and while the main thread is held in the GUI's native loop, where the Python
    handler itself would not run. A helper thread reads the socket and closes the window, which
    runs the same shutdown as the close button. If the window has not closed within
    SIGNAL_EXIT_SECONDS, the helper stops the app itself and exits.

    Must be created on the main thread (Python allows signal handlers to be set only there).
    """

    def __init__(self, app: DesktopApp, signals=None):
        names = ("SIGINT", "SIGTERM", "SIGHUP")
        self.signals = signals or [getattr(signal, n) for n in names if hasattr(signal, n)]
        self.app = app
        self.received = threading.Event()
        self.close_window = None  # set once there is a window to close
        self._reader, self._writer = socket.socketpair()
        self._writer.setblocking(False)
        self._previous = {sig: signal.getsignal(sig) for sig in self.signals}
        for sig in self.signals:
            signal.signal(sig, self._noted)
        self._previous_fd = signal.set_wakeup_fd(self._writer.fileno(), warn_on_full_buffer=False)
        threading.Thread(target=self._watch, name="foundry-signals", daemon=True).start()

    @staticmethod
    def _noted(signum, frame):
        """The Python-level handler. The wakeup socket does the work; see the class notes."""

    def _watch(self):
        while True:
            try:
                data = self._reader.recv(1)
            except OSError:
                return
            if not data:
                return
            signum = data[0]
            if signum in self.signals and not self.received.is_set():
                break
        self.received.set()
        log.info("Received signal %s; closing the window.", signum)
        # main() checks ``received`` after setting ``close_window``, and this reads
        # ``close_window`` after setting ``received``, so one of the two always acts.
        close = self.close_window
        if close is not None:
            try:
                close()
            except Exception:
                log.exception("Could not close the window after a signal.")
        if self._wait_for_exit():
            return
        log.warning(
            "The app did not stop within %s seconds of the signal; stopping now.", SIGNAL_EXIT_SECONDS
        )
        self.app.stop()
        logging.shutdown()
        os._exit(128 + signum)

    def _wait_for_exit(self) -> bool:
        """Whether the app stopped within SIGNAL_EXIT_SECONDS (the process is then ending)."""
        deadline = time.monotonic() + SIGNAL_EXIT_SECONDS
        while time.monotonic() < deadline:
            if self.app.stopped:
                return True
            time.sleep(0.1)
        return self.app.stopped

    def restore(self):
        """Put the previous handlers back (main thread only). Used by tests and on exit."""
        signal.set_wakeup_fd(self._previous_fd)
        for sig, handler in self._previous.items():
            signal.signal(sig, handler if handler is not None else signal.SIG_DFL)
        self._writer.close()
        self._reader.close()


def open_window(app: DesktopApp, signals: SignalShutdown | None = None):
    import webview

    _require_edge_chromium()
    webview.settings["ALLOW_DOWNLOADS"] = True  # exports and document downloads
    # The window opens on a local placeholder. The start address, and with it the launch code's
    # clock, follows once the web engine has shown that page.
    window = webview.create_window(APP_NAME, html=OPENING_PAGE, width=1280, height=860, min_size=(900, 600))

    def closing():
        # Runs synchronously on the GUI thread before the window closes, and also when the app
        # is quit from its menu, which ends the process without returning from webview.start.
        app.stop()

    def sign_in():
        if not window.events.loaded.wait(WINDOW_READY_SECONDS):
            log.warning(
                "The window took over %s seconds to start; sending it the start address.",
                WINDOW_READY_SECONDS,
            )
        try:
            sign_in_window(app.signin, window.load_url, lambda: window.load_html(SIGN_IN_FAILED_PAGE))
        except Exception:
            if not app.stopped:
                log.exception("Could not sign in the window.")

    window.events.closing += closing
    if signals is not None:
        signals.close_window = window.destroy
        if signals.received.is_set():
            return  # a signal arrived before there was a window to close
    gui = "edgechromium" if sys.platform == "win32" else None
    webview.start(
        sign_in,
        gui=gui,
        # Clears the window's cookies and site data at startup (InPrivate on WebView2; on macOS
        # pywebview clears WebKit's default store for the app instead).
        private_mode=True,
        # Used by WebView2, whose default folder is shared by every pywebview app; macOS
        # ignores it.
        storage_path=str(app.data_dir / "webview"),
    )


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
    parser = argparse.ArgumentParser(prog="foundry-desktop", description="Open Business Foundry.")
    parser.add_argument("--data-dir", type=Path, help="Folder for the private database (default: per-user)")
    # A Finder launch can add its own arguments; ignore anything unrecognised.
    args, _ = parser.parse_known_args(argv)
    try:
        data_dir = prepare_data_dir(args.data_dir)
        configure_logging(data_dir)
        with instance_lock(data_dir):
            app = DesktopApp(data_dir)
            signals = SignalShutdown(app)
            # A last resort for an exit that bypasses both the window's closing event and the
            # finally below, such as SystemExit raised inside the GUI toolkit. Idempotent.
            atexit.register(app.stop)
            try:
                app.start()
                if signals.received.is_set():
                    log.info("Stopping before the window opened.")
                else:
                    open_window(app, signals)
            finally:
                app.stop()
                atexit.unregister(app.stop)
                signals.restore()
    except DesktopError as exc:
        show_error(str(exc))
        raise SystemExit(1) from None
    except Exception:
        log.exception("Business Foundry stopped unexpectedly.")
        show_error("Business Foundry stopped unexpectedly. Details are in desktop.log.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
