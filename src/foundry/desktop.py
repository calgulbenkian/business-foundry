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
import collections.abc
import contextlib
import ctypes
import hmac
import logging
import logging.handlers
import multiprocessing
import os
import re
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
# After a termination signal the process ends within SIGNAL_EXIT_SECONDS, whatever the window,
# the server or the database are doing. The window first has SIGNAL_CLOSE_SECONDS to close the
# normal way; if it has not, the app runs the same cleanup itself in the time that remains.
SIGNAL_EXIT_SECONDS = 30
SIGNAL_CLOSE_SECONDS = 10

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


# /proc/self/mounts ends each line with "\n" only and separates its fields with an ASCII space
# (a tab is accepted too); no other character ends a line or a field.
# Inside a field the kernel writes a byte as a backslash and three octal digits (a space as
# \040, a tab as \011, a newline as \012, a backslash as \134, "#" as \043 on current kernels).
# Every such escape that names a byte (\000 to \377) is decoded, as util-linux does; a backslash
# without three octal digits after it is kept as it is. Escapes stand for bytes, so they are
# decoded on the field's bytes, which are then decoded the way the path being compared is.
_MOUNT_ESCAPES = re.compile(rb"\\([0-3][0-7][0-7])")
_MOUNT_FIELDS = re.compile(r"[^ \t]+")


def _mount_point(field: str) -> str:
    """Decode a mount point field in one pass, so that an escaped backslash followed by digits
    (``\\134040``) stays a backslash followed by digits instead of becoming a space."""
    raw = _MOUNT_ESCAPES.sub(lambda match: bytes([int(match.group(1), 8)]), os.fsencode(field))
    return os.fsdecode(raw)


def _linux_filesystem(path: Path, mounts: str) -> str:
    best, kind = "", ""
    target = str(path)
    for line in mounts.split("\n"):
        fields = _MOUNT_FIELDS.findall(line)
        if len(fields) < 3:
            continue
        point = _mount_point(fields[1])
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
        # Mount points are bytes. Decode them the way the path itself is decoded, so a name that
        # is not valid UTF-8 still matches (and does not stop the app with a decoding error).
        mounts = os.fsdecode(Path("/proc/self/mounts").read_bytes())
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


REDACTED = "[redacted]"
# The start address's code parameter however it is written, so that even part of a code (a
# shortened URL, say), which an exact match would miss, is removed, and with it ``code=``, so
# that a search of the log for ``code=`` finds no start address.
_START_QUERY = re.compile(r"(/desktop/start\?(?:[^\s#?]*&)?)code=[A-Za-z0-9_-]*")
_PLAIN = logging.Formatter()


class _RedactedArgument:
    """Stands in for a log argument that held a secret: it prints the argument's redacted text."""

    __slots__ = ("_str", "_repr")

    def __init__(self, text: str, representation: str):
        self._str, self._repr = text, representation

    def __str__(self):
        return self._str

    def __repr__(self):
        return self._repr


class LogRedaction(logging.Filter):
    """Keeps every launch code and the window's session token out of log records.

    It is installed as the process's log record factory, so it sees each record made through
    the logging module's usual calls (``Logger.debug`` to ``Logger.critical``, ``Logger.log``,
    ``Logger.exception`` and the module-level functions), from any logger, at whatever level a
    library has set, as the record is made and before any handler or formatter, including
    handlers a library adds for itself (pywebview writes to stderr). Each secret is added before
    it exists outside this module.

    Each argument is checked before the message is formatted, so that a format which shortens it
    (``%.20s``) cannot leave part of a secret behind: an argument whose ``str()`` or ``repr()``
    holds a whole secret, or a start address, is replaced by its redacted text. Then a whole
    secret, or a start address, in the formatted message, the text of a logged exception or a
    stack is replaced. A start address's ``code=`` parameter is replaced even when it holds only
    part of a code; any other piece of a secret is not recognised. A record that cannot be
    checked is withheld.

    As second layers it is also a filter on the handlers present when logging is set up and when
    the window library loads, and the log file's formatter redacts each finished line. Those
    layers also check a record built another way, which the factory does not check:
    ``logging.makeLogRecord`` fills a record in after the factory has made it, and ``LogRecord``
    called directly does not use the factory. A handler added later does not check it.
    """

    def __init__(self):
        super().__init__()
        self._secrets: tuple[str, ...] = ()
        self._lock = threading.Lock()
        self._installed = False

    def protect(self, secret: str):
        """Remove ``secret`` from log records from now on, as far as the class docstring says."""
        self.install()
        with self._lock:
            if secret and secret not in self._secrets:
                self._secrets = (*self._secrets, secret)

    def install(self):
        with self._lock:
            if self._installed:
                return
            make = logging.getLogRecordFactory()

            def factory(*args, **kwargs):
                return self.scrub(make(*args, **kwargs))

            logging.setLogRecordFactory(factory)
            self._installed = True

    def redact(self, text: str) -> str:
        text = _START_QUERY.sub(lambda match: match.group(1) + REDACTED, text)
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text

    def _holds_secret(self, value) -> bool:
        return any(self.redact(text) != text for text in (str(value), repr(value)))

    def _checked(self, value):
        """``value`` itself when it holds no secret, otherwise a stand-in for its redacted text."""
        text, representation = str(value), repr(value)
        clean, clean_representation = self.redact(text), self.redact(representation)
        if clean == text and clean_representation == representation:
            return value
        return _RedactedArgument(clean, clean_representation)

    def _checked_args(self, args):
        """The arguments with each one that holds a secret replaced; None when none does."""
        if isinstance(args, tuple):
            checked = tuple(self._checked(value) for value in args)
            changed = any(new is not old for new, old in zip(checked, args, strict=True))
        elif isinstance(args, collections.abc.Mapping):
            # A key is printed by ``"%s" % mapping``, but one that holds a secret cannot be
            # replaced without breaking the lookup, so the record is withheld.
            if any(self._holds_secret(key) for key in args):
                raise ValueError("a mapping key holds a secret")
            checked = {key: self._checked(value) for key, value in args.items()}
            # ``"%s" % mapping`` prints the mapping's own text, which a mapping type may write
            # its own way; the plain dict printed in its place holds the checked values.
            changed = any(checked[key] is not args[key] for key in args) or self._holds_secret(args)
        else:
            checked = self._checked(args)
            changed = checked is not args
        return checked if changed else None

    def scrub(self, record: logging.LogRecord) -> logging.LogRecord:
        try:
            # Before formatting: a precision or width in the format can shorten an argument, and
            # a shortened secret would not be found in the formatted message.
            checked = self._checked_args(record.args) if record.args else None
            if checked is not None:
                record.args = checked
            try:
                message = record.getMessage()
                clean = self.redact(message)
            except Exception:
                # Arguments that do not fit the message would otherwise be printed raw, with the
                # logging error, to stderr.
                message, clean = None, self.redact(f"{record.msg} {record.args!r}")
            if checked is not None or clean != message:
                record.msg, record.args = clean, None
            if record.exc_info:
                text = _PLAIN.formatException(record.exc_info)
                clean = self.redact(text)
                if clean != text:
                    record.exc_info, record.exc_text = None, clean
            elif record.exc_text:
                record.exc_text = self.redact(record.exc_text)
            if record.stack_info:
                record.stack_info = self.redact(record.stack_info)
        except Exception:
            record.msg, record.args = "(a log record withheld: it could not be checked for secrets)", None
            record.exc_info = record.exc_text = record.stack_info = None
        return record

    def filter(self, record):
        self.scrub(record)
        return True


LOG_REDACTION = LogRedaction()


class _RedactingFormatter(logging.Formatter):
    def format(self, record):
        return LOG_REDACTION.redact(super().format(record))


def guard_log_handlers():
    """Add the redaction filter to every handler that exists now, on any logger."""
    loggers = [logging.getLogger()]
    loggers += [x for x in logging.Logger.manager.loggerDict.values() if isinstance(x, logging.Logger)]
    handlers = [h for logger in loggers for h in logger.handlers]
    if logging.lastResort is not None:
        handlers.append(logging.lastResort)
    for handler in handlers:
        if LOG_REDACTION not in handler.filters:
            handler.addFilter(LOG_REDACTION)


def configure_logging(data_dir: Path) -> Path:
    path = data_dir / "desktop.log"
    if not path.exists():
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    LOG_REDACTION.install()
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3)
    handler.setFormatter(_RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # A further layer: httpx logs every request's full URL at INFO. Nothing in the app sends the
    # launch code through httpx, and the redaction above would remove it, but keep any URL with a
    # query string out of the file regardless.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    guard_log_handlers()
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
    browser session. At most LAUNCH_ATTEMPTS codes are issued per process. Sessions it created
    are deleted at shutdown.

    The rules hold however the window's request, the controller (``sign_in_window``), wrong
    guesses and shutdown interleave. All state changes under one lock, which is never held while
    the database is used, so ``close`` and a wrong guess are always prompt:

    - Accepting a code spends it and starts an exchange; only one exchange can be in progress.
      While it is, no other code exists (``issue`` waits for it to end) and ``wait`` does not
      report the time as up: the exchange either signs the window in or is reported refused.
    - Once the window has signed in, the app is closing, or the controller has given up
      (``conclude``), no code is issued or accepted. So one launch opens at most one session.
    - A session's hash is recorded before its row is written, so ``end_sessions`` removes it
      whenever the row lands. An exchange that finishes after ``close`` deletes its own row and
      returns no token, so no cookie is handed out after closing.

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
        self._exchanging = False  # an accepted code is being exchanged for a session
        self._signed_in = False
        self._closed = False
        self._concluded = False  # the controller gave up: no more codes
        self._changed = threading.Condition()
        self._sessions: set[str] = set()  # hashes of every session this launch began to open

    def __repr__(self):
        return f"LaunchSignIn(host={self.host!r})"

    @property
    def origin(self) -> str:
        return "http://" + self.host

    def _over(self) -> bool:
        return self._signed_in or self._closed or self._concluded

    def issue(self) -> str:
        """Create a new code, start its clock and return the window's start URL.

        Call it as the window is told to navigate, so that the window, not the server's start,
        uses the time. Any earlier code stops working. If a code is being exchanged, this waits
        for the exchange to end first, and raises if it signed the window in.
        """
        with self._changed:
            while self._exchanging and not self._closed:
                self._changed.wait()
            if self._over():
                raise RuntimeError("The window has already signed in, or the app is closing.")
            if self._issued >= self._attempts:
                raise RuntimeError(f"Only {self._attempts} launch codes are issued per process.")
            code = secrets.token_urlsafe(32)  # 256 bits
            # Known to the log redaction before it exists anywhere a library could log it.
            LOG_REDACTION.protect(code)
            self._issued += 1
            self._code = code
            self._deadline = self._clock() + self.lifetime
            self._outcome = None
            issued = self._issued
        log.info("Issued launch code %s of %s.", issued, self._attempts)
        return f"{self.origin}/desktop/start?code={code}"

    def redeem(self, code: str) -> str | None:
        """Exchange the current launch code for a session token; None when it is refused."""
        token = secrets.token_urlsafe(40)
        hashed = digest(token)
        with self._changed:
            current = self._code
            if (
                self._over()
                or current is None
                or not hmac.compare_digest(str(code).encode(), current.encode())
            ):
                # A guess, a code that has been used or replaced, or one presented after sign-in
                # has ended. The current code, if any, is not spent: a wrong guess must not lock
                # the window out.
                refused = "Refused a launch code that is not the current one."
            elif self._clock() > self._deadline:
                self._code = None  # used once, whether or not it is in time
                self._end_attempt(self.REFUSED)
                refused = "Refused a launch code presented after it expired."
            else:
                self._code = None
                self._exchanging = True
                self._sessions.add(hashed)  # before the row exists: shutdown always removes it
                refused = None
        if refused:
            log.warning(refused)
            return None
        LOG_REDACTION.protect(token)
        opened = False
        try:
            opened = self._open_session(token)
        except Exception:
            log.exception("Could not open the window's session.")
        with self._changed:
            self._exchanging = False
            kept = opened and not self._closed
            if kept:
                self._signed_in = True
                self._end_attempt(self.SIGNED_IN)
            elif not self._closed:
                self._end_attempt(self.REFUSED)
            else:
                self._changed.notify_all()  # the app is closing: its outcome stays CLOSED
        if kept:
            log.info("Signed in the app window.")
            return token
        if opened:
            log.info("The app began closing while the window signed in; its session is removed.")
        try:
            self._delete_sessions([hashed])  # also if the write failed after it was committed
        except Exception:
            log.exception("Could not remove an unused session.")
        return None

    def _end_attempt(self, outcome: str):
        self._outcome = outcome
        self._changed.notify_all()

    def wait(self, timeout: float) -> str | None:
        """How the current code ended, or None if nothing happened within timeout seconds.

        A code that is still being exchanged for a session when the time is up is waited for, so
        a sign-in in progress is never reported as a code that was not used. ``close`` ends the
        wait at once.
        """
        with self._changed:
            end = self._clock() + timeout
            while self._outcome is None:
                left = end - self._clock()
                if left <= 0 and not self._exchanging:
                    return None
                self._changed.wait(None if self._exchanging else left)
            return self._outcome

    def conclude(self) -> str:
        """End sign-in for this launch, unless the window has signed in or the app is closing.

        Waits for a code that is being exchanged. Returns SIGNED_IN or CLOSED if one of those
        happened; otherwise no code is issued or accepted after this, and it returns REFUSED.
        The controller decides on the failure page with this, so the page is never shown over a
        sign-in that then succeeds.
        """
        with self._changed:
            while self._exchanging and not self._closed:
                self._changed.wait()
            if self._signed_in:
                return self.SIGNED_IN
            if self._closed:
                return self.CLOSED
            self._concluded = True
            self._code = None
            return self.REFUSED

    def close(self):
        """Refuse every code from now on and wake anyone waiting."""
        with self._changed:
            self._code = None
            self._closed = True
            self._outcome = self.CLOSED
            self._changed.notify_all()

    def _open_session(self, token: str) -> bool:
        expiry = (datetime.now(UTC) + timedelta(days=SESSION_DAYS)).isoformat(timespec="microseconds")
        with self.store.write() as con:
            owner = con.execute(
                "SELECT id FROM principals WHERE role='owner' AND revoked=0 ORDER BY created_at, id LIMIT 1"
            ).fetchone()
            if not owner:
                log.error("No active owner to sign in as.")
                return False
            con.execute("DELETE FROM sessions WHERE expires_at<?", (now(),))
            con.execute("INSERT INTO sessions VALUES(?,?,?)", (digest(token), owner["id"], expiry))
        return True

    def _delete_sessions(self, hashes):
        with self.store.write() as con:
            con.executemany("DELETE FROM sessions WHERE token_hash=?", [(h,) for h in hashes])

    def end_sessions(self):
        """Delete every session this launch opened or began to open. Call after ``close``."""
        with self._changed:
            hashes = sorted(self._sessions)
        if hashes:
            self._delete_sessions(hashes)


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
            break  # signed in, closing, or out of codes: ``conclude`` below says which
        attempt += 1
        navigate(url)
        # A little longer than the code's lifetime, so a request that arrived just in time is
        # counted. A code still being exchanged when this runs out is waited for.
        outcome = signin.wait(signin.lifetime + LAUNCH_GRACE_SECONDS)
        if outcome == signin.SIGNED_IN:
            return True
        if outcome == signin.CLOSED:
            break  # the window may have signed in just before: ``conclude`` says
        reason = "its code was refused" if outcome == signin.REFUSED else "its code was not used in time"
        log.warning("The window did not sign in (attempt %s): %s.", attempt, reason)
    final = signin.conclude()
    if final == signin.SIGNED_IN:
        return True
    if final == signin.CLOSED:
        return False
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
        self._stopping = False
        self._stopped = threading.Event()
        self._stop_lock = threading.Lock()

    @property
    def stopping(self) -> bool:
        """Whether shutdown has begun; it may still be in progress."""
        return self._stopping

    @property
    def stopped(self) -> bool:
        """Whether shutdown has finished: ``stop`` has done its cleanup and returned."""
        return self._stopped.is_set()

    def wait_stopped(self, timeout: float | None) -> bool:
        return self._stopped.wait(timeout)

    @property
    def port(self) -> int:
        return int(self.signin.host.rsplit(":", 1)[1])

    def start(self) -> LaunchSignIn | None:
        """Start serving; return the sign-in that issues the window's launch codes.

        Returns None, and starts nothing, if ``stop`` has already been called (by the signal
        handling, while this was waiting for the lock).
        """
        # Held while starting, so that a stop from another thread waits for a server it can stop.
        with self._stop_lock:
            if self._stopping:
                log.info("Not starting: the app is closing.")
                return None
            return self._start()

    def _start(self) -> LaunchSignIn:
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
        """Stop the server, end the window's session and checkpoint the database. Idempotent.

        A second caller waits for the first to finish. ``stopped`` becomes true only when the
        cleanup has run, whether or not every step of it succeeded.
        """
        with self._stop_lock:
            if self._stopping:
                return
            self._stopping = True
            try:
                if self.server is not None:
                    self._shut_down()
            finally:
                self._stopped.set()

    def _shut_down(self):
        self.signin.close()
        self.server.should_exit = True
        self._thread.join(SHUTDOWN_SECONDS)
        if self._thread.is_alive():
            log.warning("The server did not stop within %s seconds.", SHUTDOWN_SECONDS)
        clean = True
        for step, failure in (
            (self.signin.end_sessions, "Could not delete the window's session."),
            (self._checkpoint, "Could not checkpoint the database."),
        ):
            try:
                step()
            except Exception:
                clean = False
                log.exception(failure)
        log.info("Stopped cleanly." if clean else "Stopped; part of the cleanup failed (see above).")

    def _checkpoint(self):
        con = self.signin.store.connect()
        try:
            busy, frames, done = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            con.close()
        if busy:
            log.warning("Database checkpoint incomplete (%s of %s frames).", done, frames)


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
    """Turn Ctrl+C and termination signals into the app's normal shutdown, within a time limit.

    ``signal.set_wakeup_fd`` makes Python's low-level handler write the signal number to a
    socket, and a Python handler is installed for each signal, which makes its disposition
    "caught" for the whole process. The write happens on whichever thread the operating system
    picks, including threads started by other code before ``main`` (a packaged app's bootstrap,
    or a GUI toolkit), and while the main thread is held in the GUI's native loop, where the
    Python handler itself would not run. The socket is in place before the first handler, so a
    signal that reaches one of the handlers is always written to it.

    Two threads act on a signal:

    - The reader takes the time the moment it reads the signal and, from then on, only sleeps:
      SIGNAL_EXIT_SECONDS later it ends the process with ``os._exit``. It takes no lock and
      writes no log, so nothing the window, the server, the database or the log does can hold
      it, and the process is gone by then even if ``main`` never returns.
    - The shutdown thread closes the window, from a thread of its own (pywebview's ``destroy``
      can wait for the GUI for a long time). That runs the same shutdown as the close button.
      If the app has not finished stopping SIGNAL_CLOSE_SECONDS after the signal, it starts
      ``DesktopApp.stop`` itself, again from a thread of its own (another stop may hold the
      lock), and exits as soon as the cleanup is done.

    Must be created on the main thread (Python allows signal handlers to be set only there).
    """

    def __init__(self, app: DesktopApp, signals=None):
        names = ("SIGINT", "SIGTERM", "SIGHUP")
        self.signals = signals or [getattr(signal, n) for n in names if hasattr(signal, n)]
        self.app = app
        self.received = threading.Event()
        self.close_window = None  # set once there is a window to close
        self._signum: int | None = None
        self._exit_at = 0.0
        self._go = threading.Event()  # a signal arrived, or restore() was called
        self._reader, self._writer = socket.socketpair()
        self._writer.setblocking(False)
        self._previous = {sig: signal.getsignal(sig) for sig in self.signals}
        # The socket first, then the handlers: see the class notes.
        self._previous_fd = signal.set_wakeup_fd(self._writer.fileno(), warn_on_full_buffer=False)
        for sig in self.signals:
            signal.signal(sig, self._noted)
        # A signal that arrived before the threads start waits in the socket.
        threading.Thread(target=self._shut_down, name="foundry-shutdown", daemon=True).start()
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
            if data[0] in self.signals:
                break
        signum = data[0]
        exit_at = time.monotonic() + SIGNAL_EXIT_SECONDS
        self._signum, self._exit_at = signum, exit_at
        self.received.set()
        self._go.set()
        while (left := exit_at - time.monotonic()) > 0:
            time.sleep(left)
        os._exit(128 + signum)

    def _shut_down(self):
        self._go.wait()
        signum = self._signum
        if signum is None:
            return  # restore() was called without a signal
        log.info("Received signal %s; closing the window.", signum)
        # main() checks ``received`` after setting ``close_window``, and this reads
        # ``close_window`` after ``received`` is set, so one of the two always acts.
        close = self.close_window
        if close is not None:
            threading.Thread(target=self._close, args=(close,), name="foundry-close", daemon=True).start()
        close_by = self._exit_at - SIGNAL_EXIT_SECONDS + SIGNAL_CLOSE_SECONDS
        if self.app.wait_stopped(max(0.0, close_by - time.monotonic())):
            return  # stopped the normal way; main() is returning, and the reader still bounds it
        log.warning(
            "The app did not stop within %s seconds of the signal; stopping now.", SIGNAL_CLOSE_SECONDS
        )
        threading.Thread(target=self._stop, name="foundry-stop", daemon=True).start()
        if self.app.wait_stopped(max(0.0, self._exit_at - time.monotonic())):
            os._exit(128 + signum)

    @staticmethod
    def _close(close):
        try:
            close()
        except Exception:
            log.exception("Could not close the window after a signal.")

    def _stop(self):
        try:
            self.app.stop()
        except Exception:
            log.exception("Could not stop the app after a signal.")

    def restore(self):
        """Put the previous handlers back (main thread only). Used by tests and on exit."""
        # Handlers first: until each one is replaced, its signal still reaches the socket.
        for sig, handler in self._previous.items():
            signal.signal(sig, handler if handler is not None else signal.SIG_DFL)
        signal.set_wakeup_fd(self._previous_fd)
        self._go.set()
        self._writer.close()
        self._reader.close()


def open_window(app: DesktopApp, signals: SignalShutdown | None = None):
    import webview

    guard_log_handlers()  # pywebview adds its own stderr handler when it is imported
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
            if not app.stopping:
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
                # start() returns None if the signal handling has already stopped the app.
                if app.start() is None or signals.received.is_set():
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
