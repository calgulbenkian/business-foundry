"""The desktop shell: data location, loopback binding, launch-code sign-in and shutdown.

These tests run the real local server over real HTTP, without opening a window. pywebview is
imported by one test only, which is skipped when the desktop extra is not installed.
"""

import contextlib
import decimal
import enum
import fractions
import importlib.util
import io
import logging
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
import types
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx
import pytest
from conftest import fake_uv
from fastapi.testclient import TestClient

from foundry import desktop
from foundry.api import create_app
from foundry.desktop import BROWSER_PAGE, DesktopApp, DesktopError, LaunchSignIn
from foundry.store import Store, digest

PLAIN_PAGE_TEXT = "Open Business Foundry from its icon"


@pytest.fixture
def running(tmp_path):
    app = DesktopApp(desktop.prepare_data_dir(tmp_path / "data"))
    app.start()
    url = app.signin.issue()
    try:
        yield app, url
    finally:
        app.stop()


def session_cookie(response):
    headers = [v for k, v in response.headers.multi_items() if k.lower() == "set-cookie"]
    return [h for h in headers if h.startswith("foundry_session=")]


# Data location ------------------------------------------------------------------------------


def test_data_dir_is_the_per_user_application_folder_on_this_os(monkeypatch):
    pytest.importorskip("platformdirs")
    monkeypatch.chdir("/")
    path = desktop.default_data_dir()
    assert path.is_absolute()
    if sys.platform == "darwin":
        assert path == Path.home() / "Library" / "Application Support" / "Business Foundry"
    elif sys.platform == "win32":  # pragma: no cover - not run on this CI
        assert path == Path(os.environ["LOCALAPPDATA"]) / "Business Foundry"
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        assert path == Path(base) / "Business Foundry"


def test_the_old_default_fails_from_the_root_folder_and_the_desktop_app_does_not(tmp_path, monkeypatch):
    # A Finder-launched app starts in "/". The CLI default is relative to that folder.
    monkeypatch.chdir("/")
    if not os.access("/", os.W_OK):
        with pytest.raises(OSError):
            Store(".foundry/foundry.db")
    assert not Path("/.foundry").exists()
    app = DesktopApp(desktop.prepare_data_dir(tmp_path / "data"))
    app.start()
    try:
        assert (tmp_path / "data" / "foundry.db").exists()
        assert (tmp_path / "data" / "owner.key").exists()
    finally:
        app.stop()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_data_dir_is_owner_only_even_when_it_existed(tmp_path):
    fresh = desktop.prepare_data_dir(tmp_path / "a" / "b")
    assert fresh.stat().st_mode & 0o777 == 0o700
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o755)
    os.chmod(loose, 0o755)
    assert desktop.prepare_data_dir(loose).stat().st_mode & 0o777 == 0o700


def test_a_local_folder_is_not_treated_as_a_network_drive(tmp_path):
    assert desktop.is_network_path(tmp_path) is False
    assert desktop.is_network_path(tmp_path / "not" / "yet" / "created") is False


def test_network_locations_are_recognised():
    assert desktop.is_network_path(Path(r"\\server\share\Foundry"), platform="win32") is True
    mounts = textwrap.dedent("""\
        /dev/sda1 / ext4 rw 0 0
        server:/export /home/founder nfs4 rw 0 0
        //nas/share /mnt/nas cifs rw 0 0
        /dev/sdb1 /mnt/nas/local ext4 rw 0 0
    """)
    assert desktop._linux_filesystem(Path("/home/founder/.local/share/x"), mounts) == "nfs4"
    assert desktop._linux_filesystem(Path("/home/other"), mounts) == "ext4"
    assert desktop._linux_filesystem(Path("/mnt/nas/file"), mounts) == "cifs"
    assert desktop._linux_filesystem(Path("/mnt/nas/local/db"), mounts) == "ext4"


def test_mount_points_with_escaped_characters_are_matched():
    # /proc/self/mounts writes a space, tab, newline and backslash as \040, \011, \012, \134.
    mounts = "\n".join(
        [
            "/dev/sda1 / ext4 rw 0 0",
            r"server:/export /mnt/team\011share nfs4 rw 0 0",
            r"//nas/share /mnt/back\134slash cifs rw 0 0",
            r"//nas/b /mnt/new\012line smb3 rw 0 0",
            r"server:/x /mnt/with\040space nfs rw 0 0",
            r"/dev/sdb1 /mnt/odd\134040name ext4 rw 0 0",  # a backslash, then the digits 040
            r"server:/y /mnt/odd\040name nfs rw 0 0",  # a real space
        ]
    )
    expected = {
        "/mnt/team\tshare/Business Foundry": "nfs4",
        "/mnt/back\\slash/Business Foundry": "cifs",
        "/mnt/new\nline/Business Foundry": "smb3",
        "/mnt/with space/Business Foundry": "nfs",
        "/mnt/odd\\040name/Business Foundry": "ext4",
        "/mnt/odd name/Business Foundry": "nfs",
        "/mnt/team/Business Foundry": "ext4",
    }
    for path, kind in expected.items():
        assert desktop._linux_filesystem(Path(path), mounts) == kind, repr(path)


@pytest.mark.parametrize(
    ("point", "path"),
    [
        (r"/mnt/a\043b", "/mnt/a#b/data"),
        ("/mnt/a\u00a0b", "/mnt/a\u00a0b/data"),  # no-break space
        # ideographic space, in a Japanese name
        ("/mnt/\u5171\u6709\u3000\u30c7\u30fc\u30bf", "/mnt/\u5171\u6709\u3000\u30c7\u30fc\u30bf/data"),
        ("/mnt/a\u2028b", "/mnt/a\u2028b/data"),  # line separator
        ("/mnt/a\u000cb", "/mnt/a\u000cb/data"),  # form feed
        ("/mnt/a\u0085b", "/mnt/a\u0085b/data"),  # next line
    ],
)
def test_mount_points_are_split_and_decoded_the_way_the_kernel_writes_them(point, path):
    # Only "\n" ends a line and only an ASCII space or tab separates fields; any other character
    # is part of the name, and any \ooo escape is decoded.
    mounts = f"/dev/sda1 / ext4 rw 0 0\n//srv/share {point} cifs rw 0 0\n"
    assert desktop._linux_filesystem(Path(path), mounts) == "cifs"


def test_every_octal_escape_in_a_mount_point_is_decoded():
    mounts = "\n".join(
        [
            "/dev/sda1 / ext4 rw 0 0",
            r"//nas/a /mnt/a\040b\011c\012d\134e\043f cifs rw 0 0",  # several kinds at once
            r"//nas/b /mnt/hash\043tag nfs rw 0 0",  # a character outside space, tab, newline, \
            r"//nas/c /mnt/short\04 smb3 rw 0 0",  # a backslash with only two digits after it
            r"//nas/d /mnt/caf\303\251 nfs4 rw 0 0",  # escaped UTF-8 bytes of one character
        ]
    )
    expected = {
        "/mnt/a b\tc\nd\\e#f/data": "cifs",
        "/mnt/hash#tag/data": "nfs",
        "/mnt/short\\04/data": "smb3",
        "/mnt/caf\u00e9/data": "nfs4",
    }
    for path, kind in expected.items():
        assert desktop._linux_filesystem(Path(path), mounts) == kind, repr(path)


@pytest.mark.parametrize("separator", ["\u2028", "\u0085"])  # line separator, next line
def test_a_line_after_a_unicode_line_separator_is_still_read(separator):
    # The second line is one local mount whose name contains the separator, and whose second
    # half (with no-break spaces in it) looks like a line of its own. Read as one line, it does
    # not cover /mnt/nas/data, so the network line after it still decides.
    tail = "\u00a0".join(["x", "/mnt/nas/data", "ext4"])
    mounts = "\n".join(
        [
            "/dev/sda1 / ext4 rw 0 0",
            f"/dev/sdb1 /mnt/notes{separator}{tail} ext4 rw 0 0",
            f"//nas/notes /mnt/team{separator}share smb3 rw 0 0",
            "//nas/share /mnt/nas cifs rw 0 0",
        ]
    )
    assert desktop._linux_filesystem(Path("/mnt/nas/data"), mounts) == "cifs"
    assert desktop._linux_filesystem(Path(f"/mnt/team{separator}share/data"), mounts) == "smb3"
    assert desktop._linux_filesystem(Path(f"/mnt/notes{separator}{tail}/db"), mounts) == "ext4"


def test_a_tab_between_fields_separates_them():
    mounts = "/dev/sda1\t/\text4 rw 0 0\n//nas/share\t/mnt/nas\tcifs\trw 0 0\n"
    assert desktop._linux_filesystem(Path("/mnt/nas/data"), mounts) == "cifs"
    assert desktop._linux_filesystem(Path("/home/founder"), mounts) == "ext4"


def test_the_linux_check_reads_the_mount_table_as_bytes(tmp_path, monkeypatch):
    # A mount point that is not valid UTF-8 must not stop the check, and the data folder's own
    # network mount is still found.
    here = os.fsencode(str(tmp_path.resolve()))
    table = (
        b"/dev/sda1 / ext4 rw 0 0\n//nas/x /mnt/caf\xe9 cifs rw 0 0\nserver:/home " + here + b" nfs4 rw 0 0\n"
    )
    read_bytes = Path.read_bytes
    monkeypatch.setattr(
        Path, "read_bytes", lambda self: table if str(self) == "/proc/self/mounts" else read_bytes(self)
    )
    assert desktop.is_network_path(tmp_path / "data", platform="linux") is True


@pytest.mark.skipif(sys.platform != "linux", reason="reads the kernel's own mount table")
def test_the_real_linux_mount_table_gives_a_type_for_the_root_and_the_temp_folder(tmp_path):
    # Read the way is_network_path reads it. Only that a type is found is checked: a developer's
    # temp folder may be on any filesystem, so which type, or whether it is local, is not.
    mounts = os.fsdecode(Path("/proc/self/mounts").read_bytes())
    assert desktop._linux_filesystem(Path("/"), mounts) != ""
    assert desktop._linux_filesystem(tmp_path.resolve(), mounts) != ""


def test_security_md_names_every_network_filesystem_the_linux_check_refuses():
    text = (Path(__file__).parents[1] / "SECURITY.md").read_text()
    bullet = next(line for line in text.splitlines() if line.startswith("- **Data location.**"))
    linux = bullet.split("On Linux", 1)[1]
    missing = sorted(name for name in desktop.NETWORK_FILESYSTEMS if f"`{name}`" not in linux)
    assert not missing


def test_a_network_data_dir_is_refused_before_anything_is_created(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "is_network_path", lambda path: True)
    target = tmp_path / "remote"
    with pytest.raises(DesktopError, match="network drive"):
        desktop.prepare_data_dir(target)
    assert not target.exists()


# Loopback socket ----------------------------------------------------------------------------


def test_the_server_socket_is_bound_on_loopback_port_zero_and_held():
    sock = desktop.bind_loopback()
    try:
        address, port = sock.getsockname()[:2]
        assert address == "127.0.0.1" and port != 0
        # The port is held from the moment it is chosen: nothing else can bind it.
        other = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with pytest.raises(OSError):
            other.bind(("127.0.0.1", port))
        other.close()
        # It is already listening, before any server is attached.
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
    finally:
        sock.close()


def test_the_running_server_listens_on_the_socket_it_was_given(running):
    app, url = running
    assert urlsplit(url).hostname == "127.0.0.1"
    assert urlsplit(url).port == app.port
    assert httpx.get(app.signin.origin + "/api/health").json()["status"] == "ok"


# Launch-code sign-in, over real HTTP --------------------------------------------------------


def test_the_launch_code_signs_in_once_and_leaves_the_address(running):
    app, url = running
    code = dict(p.split("=", 1) for p in urlsplit(url).query.split("&"))["code"]
    assert len(code) >= 43  # 32 random bytes, base64url
    with httpx.Client() as window:
        response = window.get(url)
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        [cookie] = session_cookie(response)
        attributes = {part.strip().split("=")[0].lower() for part in cookie.split(";")[1:]}
        assert {"httponly", "samesite"} <= attributes
        assert "samesite=strict" in cookie.lower()
        assert "domain" not in attributes  # host-only
        assert not attributes & {"max-age", "expires"}  # ends with the window
        page = window.get(app.signin.origin + "/")
        assert page.status_code == 200 and 'id="login-form"' in page.text  # the app shell
        me = window.get(app.signin.origin + "/api/me").json()
        assert me["role"] == "owner"


def test_a_reused_launch_code_is_refused(running):
    app, url = running
    with httpx.Client() as window:
        assert window.get(url).status_code == 303
    with httpx.Client() as other:
        again = other.get(url)
        assert again.status_code == 403
        assert PLAIN_PAGE_TEXT in again.text
        assert session_cookie(again) == []
        assert other.get(app.signin.origin + "/api/me").status_code == 401


def test_a_wrong_launch_code_is_refused_without_spending_the_real_one(running):
    app, url = running
    real = url.rsplit("=", 1)[1]
    other = "B" if real[-1] == "A" else "A"  # always differs from the real last character
    with httpx.Client() as guesser:
        for wrong in ["", "x", real[:-1] + other, real[:-1], real + other, "é" * 43]:
            assert wrong != real
            response = guesser.get(app.signin.origin + "/desktop/start", params={"code": wrong})
            assert response.status_code == 403
            assert session_cookie(response) == []
        assert guesser.get(app.signin.origin + "/desktop/start").status_code == 403
    with httpx.Client() as window:
        assert window.get(url).status_code == 303


def test_an_expired_launch_code_is_refused(tmp_path):
    store = Store(tmp_path / "foundry.db")
    store.bootstrap()
    clock = [1000.0]
    signin = LaunchSignIn(store, "testserver", clock=lambda: clock[0])
    url = signin.issue()
    path = url.removeprefix(signin.origin)
    clock[0] += desktop.LAUNCH_CODE_SECONDS + 0.5
    with TestClient(create_app(store.path, allowed_hosts=["testserver"], desktop=signin)) as client:
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 403
        assert PLAIN_PAGE_TEXT in response.text
        assert session_cookie(response) == []
        # Expiry spends the code: winding the clock back does not revive it.
        clock[0] = 1000.0
        assert client.get(path, follow_redirects=False).status_code == 403


def test_a_code_is_accepted_just_inside_its_time_limit(tmp_path):
    store = Store(tmp_path / "foundry.db")
    store.bootstrap()
    clock = [1000.0]
    signin = LaunchSignIn(store, "testserver", clock=lambda: clock[0])
    path = signin.issue().removeprefix(signin.origin)
    clock[0] += desktop.LAUNCH_CODE_SECONDS - 0.5
    with TestClient(create_app(store.path, allowed_hosts=["testserver"], desktop=signin)) as client:
        assert client.get(path, follow_redirects=False).status_code == 303


def test_nothing_is_accepted_before_a_code_is_issued_and_none_after_sign_in(tmp_path):
    store = Store(tmp_path / "foundry.db")
    store.bootstrap()
    signin = LaunchSignIn(store, "testserver")
    assert signin.redeem("") is None and signin.redeem("None") is None
    code = signin.issue().rsplit("=", 1)[1]
    assert code not in repr(signin)
    assert signin.redeem(code)
    with pytest.raises(RuntimeError):
        signin.issue()  # the window is signed in: no further codes this process


def test_starting_the_server_issues_no_code(tmp_path):
    # The clock starts when the window is sent the address, not when the server is ready.
    app = DesktopApp(desktop.prepare_data_dir(tmp_path / "data"))
    signin = app.start()
    try:
        assert signin._code is None and signin._issued == 0
    finally:
        app.stop()


def test_the_time_limit_counts_from_issue_not_from_creation(tmp_path):
    store = Store(tmp_path / "foundry.db")
    store.bootstrap()
    clock = [1000.0]
    signin = LaunchSignIn(store, "testserver", clock=lambda: clock[0])
    clock[0] += 10 * desktop.LAUNCH_CODE_SECONDS  # a very slow window start before issue
    code = signin.issue().rsplit("=", 1)[1]
    clock[0] += desktop.LAUNCH_CODE_SECONDS - 0.5
    assert signin.redeem(code)


def test_the_limit_is_generous():
    assert desktop.LAUNCH_CODE_SECONDS >= 120
    assert desktop.LAUNCH_ATTEMPTS >= 2


def test_a_reissued_code_works_and_the_stale_one_still_fails(tmp_path):
    store = Store(tmp_path / "foundry.db")
    store.bootstrap()
    clock = [1000.0]
    signin = LaunchSignIn(store, "testserver", clock=lambda: clock[0])
    stale = signin.issue().removeprefix(signin.origin)
    clock[0] += desktop.LAUNCH_CODE_SECONDS + 1
    with TestClient(create_app(store.path, allowed_hosts=["testserver"], desktop=signin)) as client:
        late = client.get(stale, follow_redirects=False)
        assert late.status_code == 403 and session_cookie(late) == []
        assert signin.wait(0) == signin.REFUSED  # the window's controller learns of it
        fresh = signin.issue().removeprefix(signin.origin)
        assert fresh != stale
        assert signin.wait(0) is None  # a new attempt starts with no outcome
        clock[0] = 1000.0  # even inside the old code's original time limit
        assert client.get(stale, follow_redirects=False).status_code == 403
        ok = client.get(fresh, follow_redirects=False)
        assert ok.status_code == 303 and len(session_cookie(ok)) == 1
        assert signin.wait(0) == signin.SIGNED_IN
        assert client.get(fresh, follow_redirects=False).status_code == 403  # single use
        assert client.get(stale, follow_redirects=False).status_code == 403


def test_a_new_code_replaces_an_unused_one(tmp_path):
    store = Store(tmp_path / "foundry.db")
    store.bootstrap()
    signin = LaunchSignIn(store, "testserver")
    first = signin.issue().rsplit("=", 1)[1]
    second = signin.issue().rsplit("=", 1)[1]
    assert signin.redeem(first) is None
    assert signin.wait(0) is None  # a replaced code is a guess, not the window failing
    assert signin.redeem(second)


def test_codes_are_issued_a_bounded_number_of_times(tmp_path):
    store = Store(tmp_path / "foundry.db")
    store.bootstrap()
    signin = LaunchSignIn(store, "testserver", attempts=3)
    for _ in range(3):
        signin.issue()
    with pytest.raises(RuntimeError):
        signin.issue()


def test_closing_refuses_the_current_code_and_wakes_the_waiter(tmp_path):
    store = Store(tmp_path / "foundry.db")
    store.bootstrap()
    signin = LaunchSignIn(store, "testserver")
    code = signin.issue().rsplit("=", 1)[1]
    signin.close()
    assert signin.wait(5) == signin.CLOSED
    assert signin.redeem(code) is None
    with pytest.raises(RuntimeError):
        signin.issue()


# The window's sign-in controller, and everything overlapping it --------------------------------
#
# Nothing below depends on real time passing. Codes are timed by a clock the test moves, the
# window's requests run on their own threads as a real window's do, and the test waits for
# steps it can observe: a thread asleep on the sign-in, a session write entered, a request
# finished, the controller returned.


class Progress:
    """Something a test can wait for. Every observable step in the helpers below notifies it."""

    def __init__(self):
        self.cond = threading.Condition()

    def notify(self):
        with self.cond:
            self.cond.notify_all()

    def until(self, predicate, what, timeout=20):
        # The timeout only stops a broken test from hanging; no result depends on it.
        with self.cond:
            if not self.cond.wait_for(predicate, timeout):
                raise AssertionError(f"Timed out waiting for {what}.")


class Clock:
    """A sign-in's clock that moves only when the test moves it."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Watched(threading.Condition):
    """The sign-in's condition, instrumented so the test can see a thread go to sleep on it.

    A thread that has been recorded here holds the condition until it is inside ``wait``, so a
    change the test then makes while holding the condition, and notifies, reaches it.
    """

    def __init__(self, progress):
        super().__init__()
        self.progress = progress
        self.sleepers = []  # (thread, timeout) for every sleep, in order

    def wait(self, timeout=None):
        self.sleepers.append((threading.current_thread(), timeout))
        self.progress.notify()
        return super().wait(timeout)

    def asleep(self, thread, without_limit=False):
        return any(t is thread and (timeout is None or not without_limit) for t, timeout in self.sleepers)


class Pause:
    """Holds each session write of a sign-in until the test releases it.

    ``where`` is "before" (the write has not started: as if the database were busy) or "after"
    (the row is committed, but the exchange has not yet finished).
    """

    def __init__(self, signin, progress, where="before"):
        self.progress, self.where = progress, where
        self.entered, self.finished = 0, 0  # session writes reached; exchanges that returned
        self.release = threading.Event()
        exchanging = set()
        real = signin._open_session

        def paused(*args):
            exchanging.add(threading.get_ident())
            if where == "before":
                self._hold()
            result = real(*args)
            if where == "after":
                self._hold()
            return result

        signin._open_session = paused
        real_redeem = signin.redeem

        def redeem(code):
            try:
                return real_redeem(code)
            finally:
                if threading.get_ident() in exchanging:
                    exchanging.discard(threading.get_ident())
                    self.finished += 1
                    progress.notify()

        signin.redeem = redeem

    def _hold(self):
        self.entered += 1
        self.progress.notify()
        assert self.release.wait(20), "the test never released the session write"


def watch(signin, progress, clock=None):
    signin._changed = Watched(progress)
    if clock is not None:
        signin._clock = clock
    return signin


def advance(signin, seconds):
    """Move the sign-in's clock, and wake whoever is waiting on it so it reads the clock again."""
    with signin._changed:
        signin._clock.now += seconds
        signin._changed.notify_all()


def session_rows(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    finally:
        con.close()


class Window:
    """Stands in for the pywebview window.

    ``navigate`` returns at once and the request runs on another thread, as a real window's
    does. ``before(attempt)`` runs on that thread first (to make the window late, say), and
    ``drop`` lists attempts the window never loads.
    """

    def __init__(self, app, progress, before=None, drop=()):
        self.app, self.progress, self.before, self.drop = app, progress, before, set(drop)
        self.client = httpx.Client()
        self.sent, self.visits, self.pages, self.threads = [], [], [], []
        self.gave_up = 0

    def navigate(self, url):
        self.sent.append(url)
        attempt = len(self.sent)
        if attempt not in self.drop:
            thread = threading.Thread(target=self._load, args=(attempt, url), daemon=True)
            self.threads.append(thread)
            thread.start()
        self.progress.notify()

    def _load(self, attempt, url):
        try:
            if self.before:
                self.before(attempt)
            self.visits.append((url, self._get(url)))
            if self.visits[-1][1] == 303:  # then the window loads the interface it was sent to
                self.pages.append(self._get(self.app.signin.origin + "/"))
        finally:
            self.progress.notify()

    def _get(self, url):
        try:
            return self.client.get(url).status_code
        except httpx.HTTPError as exc:  # the server went away
            return type(exc).__name__

    def give_up(self):
        self.gave_up += 1
        self.progress.notify()

    def finish(self):
        for thread in self.threads:
            thread.join(20)


class Controller(threading.Thread):
    """Runs sign_in_window on its own thread, as pywebview runs the sign-in function."""

    def __init__(self, signin, window, progress):
        super().__init__(daemon=True, name="controller")
        self.signin, self.window, self.progress = signin, window, progress
        self.result, self.done = None, threading.Event()

    def run(self):
        try:
            self.result = desktop.sign_in_window(self.signin, self.window.navigate, self.window.give_up)
        finally:
            self.done.set()
            self.progress.notify()


@pytest.fixture
def stepped(tmp_path):
    """A running app whose launch codes are timed by a clock the test moves."""
    progress, clock = Progress(), Clock()
    app = DesktopApp(desktop.prepare_data_dir(tmp_path / "data"))
    app.start()
    watch(app.signin, progress, clock)
    try:
        yield app, progress
    finally:
        app.stop()


def late(app, attempts):
    """A window that presents the listed attempts' codes after they expired."""

    def before(attempt):
        if attempt in attempts:
            app.signin._clock.now += app.signin.lifetime + 1  # a slow cold start

    return before


def test_a_window_that_arrives_late_is_sent_a_fresh_code(stepped, caplog):
    app, progress = stepped
    window = Window(app, progress, before=late(app, {1}))
    with caplog.at_level("INFO", logger="foundry.desktop"):
        assert desktop.sign_in_window(app.signin, window.navigate, window.give_up) is True
        window.finish()
    [(stale, first), (fresh, second)] = window.visits
    assert (first, second) == (403, 303) and stale != fresh
    assert window.pages == [200] and window.gave_up == 0
    assert window.client.get(app.signin.origin + "/api/me").json()["role"] == "owner"
    with httpx.Client() as other:
        assert other.get(stale).status_code == 403
        assert other.get(fresh).status_code == 403
    assert "Refused a launch code presented after it expired." in caplog.text
    assert "The window did not sign in (attempt 1): its code was refused." in caplog.text
    for url, _ in window.visits:
        assert url.rsplit("=", 1)[1] not in caplog.text


def test_a_code_the_window_never_used_is_replaced_when_its_time_is_up(stepped):
    app, progress = stepped
    window = Window(app, progress, drop={1})  # the window never loads the first address
    controller = Controller(app.signin, window, progress)
    controller.start()
    progress.until(lambda: app.signin._changed.asleep(controller), "the controller to wait")
    advance(app.signin, app.signin.lifetime + desktop.LAUNCH_GRACE_SECONDS + 1)
    controller.join(20)
    window.finish()
    assert controller.result is True and window.gave_up == 0
    dropped, used = window.sent
    assert [status for _, status in window.visits] == [303] and window.pages == [200]
    with httpx.Client() as other:
        assert other.get(dropped).status_code == 403
        assert other.get(used).status_code == 403


def test_the_window_is_told_plainly_after_the_last_attempt(stepped):
    app, progress = stepped
    window = Window(app, progress, before=late(app, {1, 2, 3}))
    assert desktop.sign_in_window(app.signin, window.navigate, window.give_up) is False
    window.finish()
    assert [status for _, status in window.visits] == [403] * desktop.LAUNCH_ATTEMPTS
    assert window.gave_up == 1
    assert window.client.get(app.signin.origin + "/api/me").status_code == 401


def test_closing_the_app_stops_the_controller_without_a_failure_page(stepped):
    app, progress = stepped
    window = Window(app, progress, drop={1, 2, 3})
    controller = Controller(app.signin, window, progress)
    controller.start()
    progress.until(lambda: app.signin._changed.asleep(controller), "the controller to wait")
    app.stop()
    controller.join(20)
    assert controller.result is False and window.gave_up == 0 and len(window.sent) == 1


def test_time_running_out_during_the_exchange_neither_replaces_the_code_nor_fails(tmp_path, monkeypatch):
    # The window's request has been accepted and is writing its session (here, held) when the
    # controller's time runs out: none at all, so it is out the moment the controller waits.
    # No replacement code may be issued, the failure page must not be shown, and the window ends
    # signed in with the one session. (At 81bfbb4 the controller sent three codes, each opened a
    # session, and the window was shown the failure page.)
    monkeypatch.setattr(desktop, "LAUNCH_GRACE_SECONDS", 0)
    progress = Progress()
    app = DesktopApp(desktop.prepare_data_dir(tmp_path / "data"), launch_seconds=0)
    app.start()
    try:
        watch(app.signin, progress, Clock())
        pause = Pause(app.signin, progress)
        window = Window(app, progress)
        navigate = window.navigate

        def navigate_and_wait_for_the_exchange(url):
            navigate(url)
            entered = len(window.sent)
            progress.until(lambda: pause.entered >= entered, "the window's request to be exchanging")

        window.navigate = navigate_and_wait_for_the_exchange
        controller = Controller(app.signin, window, progress)
        controller.start()
        # Until the controller, its time up, waits with no limit for the exchange to end (or, as
        # at 81bfbb4, has moved on and finished).
        progress.until(
            lambda: controller.done.is_set() or app.signin._changed.asleep(controller, without_limit=True),
            "the controller to wait for the exchange",
        )
        pause.release.set()
        controller.join(20)
        window.finish()
        assert controller.result is True
        assert window.gave_up == 0
        assert len(window.sent) == 1 and [status for _, status in window.visits] == [303]
        assert window.pages == [200]
        assert session_rows(app.db_path) == 1
        with pytest.raises(RuntimeError):
            app.signin.issue()
        assert window.client.get(app.signin.origin + "/api/me").json()["role"] == "owner"
    finally:
        pause.release.set()
        app.stop()


def test_no_code_is_issued_while_one_is_being_exchanged_and_none_works_after(tmp_path):
    store = Store(tmp_path / "foundry.db")
    store.bootstrap()
    progress = Progress()
    signin = watch(LaunchSignIn(store, "testserver", clock=Clock()), progress)
    pause = Pause(signin, progress)
    first = signin.issue().rsplit("=", 1)[1]
    exchanged = []
    exchange = threading.Thread(target=lambda: exchanged.append(signin.redeem(first)))
    exchange.start()
    progress.until(lambda: pause.entered == 1, "the exchange to start")
    # While it is in flight: a wrong guess and the same code again are refused at once, and
    # spend nothing.
    assert signin.redeem(first[:-1] + ("B" if first[-1] == "A" else "A")) is None
    assert signin.redeem(first) is None
    replacement = []

    def issue():
        try:
            replacement.append(signin.issue())
        except RuntimeError as exc:
            replacement.append(exc)
        progress.notify()

    issuer = threading.Thread(target=issue)
    issuer.start()
    progress.until(
        lambda: replacement or signin._changed.asleep(issuer), "the second issue to return or wait"
    )
    pause.release.set()
    exchange.join(20)
    issuer.join(20)
    [token] = exchanged
    assert token
    [second] = replacement
    if isinstance(second, str):  # 81bfbb4 issued a second code during the exchange ...
        assert signin.redeem(second.rsplit("=", 1)[1]) is None  # ... which then signed in again
    assert isinstance(second, RuntimeError)
    assert session_rows(store.path) == 1
    assert signin.wait(0) == signin.SIGNED_IN


def test_a_sign_in_followed_at_once_by_closing_is_still_reported_as_a_sign_in(tmp_path):
    # The window's request signs in and the app begins closing before the controller looks.
    store = Store(tmp_path / "foundry.db")
    store.bootstrap()
    signin = LaunchSignIn(store, "testserver")
    tokens, gave_up = [], []

    def navigate(url):
        tokens.append(signin.redeem(url.rsplit("=", 1)[1]))
        signin.close()

    assert desktop.sign_in_window(signin, navigate, lambda: gave_up.append(True)) is True
    assert tokens[0] and gave_up == []


@pytest.mark.parametrize("where", ["before", "after"])
def test_an_exchange_overtaken_by_shutdown_leaves_no_session_and_no_token(tmp_path, where):
    # "before": the app closes and deletes its sessions while the window's session write is still
    # waiting to start, so the row is written after the deletion. "after": the row is committed,
    # but the exchange has not finished when the app closes.
    store = Store(tmp_path / "foundry.db")
    store.bootstrap()
    progress = Progress()
    signin = watch(LaunchSignIn(store, "testserver", clock=Clock()), progress)
    pause = Pause(signin, progress, where=where)
    code = signin.issue().rsplit("=", 1)[1]
    exchanged = []
    exchange = threading.Thread(target=lambda: exchanged.append(signin.redeem(code)))
    exchange.start()
    progress.until(lambda: pause.entered == 1, "the exchange to reach the session write")
    waited = []
    waiter = threading.Thread(target=lambda: waited.append(signin.wait(600)))
    waiter.start()
    progress.until(lambda: signin._changed.asleep(waiter), "the controller to wait")
    signin.close()
    signin.end_sessions()  # what DesktopApp.stop does next
    waiter.join(20)
    assert waited == [signin.CLOSED]  # woken at once, though the exchange is still in flight
    pause.release.set()
    exchange.join(20)
    assert exchanged == [None]  # no token, so no cookie
    assert session_rows(store.path) == 0
    assert signin.wait(0) == signin.CLOSED
    with pytest.raises(RuntimeError):
        signin.issue()


def test_stopping_the_app_during_the_windows_exchange_sends_no_cookie_and_leaves_no_row(tmp_path):
    progress = Progress()
    app = DesktopApp(desktop.prepare_data_dir(tmp_path / "data"))
    app.start()
    watch(app.signin, progress, Clock())
    pause = Pause(app.signin, progress)
    closing = app.signin.close

    def close():
        closing()
        pause.release.set()  # the session write proceeds once the app has begun to close

    app.signin.close = close
    try:
        window = Window(app, progress)
        window.navigate(app.signin.issue())
        progress.until(lambda: pause.entered == 1, "the window's request to be exchanging")
        app.stop()
        window.finish()
        progress.until(lambda: pause.finished == 1, "the exchange to return")
        [(_, status)] = window.visits
        assert status != 303
        assert "foundry_session" not in window.client.cookies
        assert session_rows(app.db_path) == 0
    finally:
        pause.release.set()
        app.stop()


def test_closing_while_the_database_is_busy_is_prompt_and_the_late_exchange_leaves_nothing(tmp_path):
    store = Store(tmp_path / "foundry.db")
    store.bootstrap()
    progress = Progress()
    signin = watch(LaunchSignIn(store, "testserver", clock=Clock()), progress)
    pause = Pause(signin, progress)
    pause.release.set()  # not held by the test: by the database, below
    blocker = sqlite3.connect(store.path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")  # another writer holds the database
    exchanged = []
    try:

        def navigate(url):
            code = url.rsplit("=", 1)[1]
            threading.Thread(target=lambda: exchanged.append(signin.redeem(code)), daemon=True).start()

        window = types.SimpleNamespace(navigate=navigate, give_up=lambda: exchanged.append("gave up"))
        controller = Controller(signin, window, progress)
        controller.start()
        progress.until(lambda: pause.entered == 1, "the exchange to reach the busy database")
        progress.until(lambda: signin._changed.asleep(controller), "the controller to wait")
        # A wrong guess is refused at once, and spends nothing, while the database is busy.
        assert signin.redeem("not the code") is None
        signin.close()
        progress.until(controller.done.is_set, "the controller to stop")
        assert controller.result is False
        assert exchanged == []  # still waiting for the database: the close did not wait for it
    finally:
        blocker.rollback()
        blocker.close()
    progress.until(lambda: pause.finished == 1, "the exchange to finish")
    assert exchanged == [None]
    assert session_rows(store.path) == 0


# Over HTTP --------------------------------------------------------------------------------------


def test_a_browser_tab_gets_the_plain_page_and_no_session(running):
    app, _ = running
    with httpx.Client() as tab:
        page = tab.get(app.signin.origin + "/")
        assert page.status_code == 403
        assert page.text == BROWSER_PAGE
        assert "login-form" not in page.text and "<script" not in page.text
        assert session_cookie(page) == []
        assert "script-src 'self'" in page.headers["content-security-policy"]
        assert page.headers["x-frame-options"] == "DENY"
        assert tab.get(app.signin.origin + "/api/me").status_code == 401


def test_only_the_exact_loopback_host_and_port_is_accepted(running):
    app, url = running
    for host in [f"localhost:{app.port}", f"127.0.0.1:{app.port + 1}", "127.0.0.1", "evil.example"]:
        response = httpx.get(app.signin.origin + "/api/health", headers={"Host": host})
        assert response.status_code == 400, host
        assert "script-src 'self'" in response.headers["content-security-policy"]
    # A rebound DNS name cannot even reach the start route.
    assert httpx.get(url, headers={"Host": f"rebind.example:{app.port}"}).status_code == 400
    with httpx.Client() as window:
        assert window.get(url).status_code == 303


def test_a_leftover_public_origin_setting_does_not_block_desktop_saves(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("FOUNDRY_PUBLIC_ORIGIN", "https://foundry.example")
    app = DesktopApp(desktop.prepare_data_dir(tmp_path / "data"))
    with caplog.at_level("INFO", logger="foundry.desktop"):
        app.start()
    try:
        assert "FOUNDRY_PUBLIC_ORIGIN is set; the desktop app ignores it" in caplog.text
        body = {"name": "Synthetic shop", "concept": "A synthetic business used only in this test."}
        with httpx.Client() as window:
            window.get(app.signin.issue())
            ok = window.post(
                app.signin.origin + "/api/businesses",
                json=body,
                headers={"Idempotency-Key": "a", "Origin": app.signin.origin},
            )
            assert ok.status_code == 201
            assert window.post(
                app.signin.origin + "/api/businesses",
                json=body,
                headers={"Idempotency-Key": "b", "Sec-Fetch-Site": "same-origin"},
            ).status_code in {200, 201}
            # The configured value is not accepted in its place.
            refused = window.post(
                app.signin.origin + "/api/businesses",
                json=body,
                headers={"Idempotency-Key": "c", "Origin": "https://foundry.example"},
            )
            assert refused.status_code == 403
    finally:
        app.stop()


def test_cookie_writes_still_need_same_origin_evidence_in_desktop_mode(running):
    app, url = running
    body = {"name": "Synthetic shop", "concept": "A synthetic business used only in this test."}
    with httpx.Client() as window:
        window.get(url)
        bare = window.post(app.signin.origin + "/api/businesses", json=body, headers={"Idempotency-Key": "a"})
        assert bare.status_code == 403
        foreign = window.post(
            app.signin.origin + "/api/businesses",
            json=body,
            headers={"Idempotency-Key": "b", "Origin": "http://evil.example"},
        )
        assert foreign.status_code == 403
        ok = window.post(
            app.signin.origin + "/api/businesses",
            json=body,
            headers={"Idempotency-Key": "c", "Origin": app.signin.origin},
        )
        assert ok.status_code == 201


def test_the_ordinary_server_has_no_desktop_route_and_keeps_its_sign_in_page(tmp_path):
    store = Store(tmp_path / "foundry.db")
    with TestClient(create_app(store.path, allowed_hosts=["testserver"])) as client:
        assert client.get("/desktop/start?code=anything").status_code == 404
        page = client.get("/")
        assert page.status_code == 200 and 'id="login-form"' in page.text


def test_the_request_log_never_records_the_launch_code(running, caplog):
    app, url = running
    code = url.rsplit("=", 1)[1]
    with caplog.at_level("INFO", logger="foundry.desktop"), httpx.Client() as window:
        window.get(url)
        window.get(app.signin.origin + "/")
    assert "GET /desktop/start 303" in caplog.text
    assert code not in caplog.text


@pytest.fixture
def isolated_logging():
    """configure_logging changes the root logger; put it back afterwards."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        yield
    finally:
        for handler in root.handlers[:]:
            if handler not in handlers:
                root.removeHandler(handler)
                handler.close()
        root.setLevel(level)


def test_no_logger_at_any_level_writes_a_launch_code(tmp_path, capfd, isolated_logging):
    data = desktop.prepare_data_dir(tmp_path / "data")
    # A library logger set to DEBUG, with its own console handler, as pywebview's is.
    library = logging.getLogger("synthetic.window.library")
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter("[library] %(message)s"))
    library.addHandler(console)
    library.setLevel(logging.DEBUG)
    try:
        desktop.configure_logging(data)
        store = Store(data / "foundry.db")
        store.bootstrap()
        url = LaunchSignIn(store, "127.0.0.1:9").issue()
        code = url.rsplit("=", 1)[1]
        library.debug(f"Loading URL: {url}")  # in the message
        library.debug("Loading URL with arguments: %s", url)  # in the arguments
        library.debug("Loading %(where)s", {"where": url})  # in a mapping argument
        # Arguments that do not fit would be printed raw, with a logging error, to stderr.
        # (pytest's own log capture re-raises such errors; what reached the file and the console
        # is checked below.)
        with contextlib.suppress(TypeError):
            library.debug("Arguments that do not fit %s %s", url)
        library.debug("Shortened: %s", url[:-10])  # part of a code, still in the start address
        library.info("The code alone: " + code)
        try:
            raise RuntimeError(f"could not load {url}")
        except RuntimeError:
            library.exception("Load failed")  # in a logged exception
        console.flush()
    finally:
        library.removeHandler(console)
    written = (data / "desktop.log").read_text()
    out, err = capfd.readouterr()
    for where, text in (("desktop.log", written), ("stderr", err), ("stdout", out)):
        assert code not in text and code[:-10] not in text and "code=" not in text, where
    redacted = "http://127.0.0.1:9/desktop/start?" + desktop.REDACTED
    for line in ("Loading URL: ", "Loading URL with arguments: ", "Loading ", "Shortened: "):
        assert line + redacted in written and "[library] " + line + redacted in err
    assert "The code alone: " + desktop.REDACTED in written
    assert "Load failed" in written and f"RuntimeError: could not load {redacted}" in written
    assert f"RuntimeError: could not load {redacted}" in err
    assert "Arguments that do not fit" in written and "Logging error" not in err


# Fixed synthetic secrets, so that "no part of it" can be checked exactly. They are not codes the
# app ever issued.
SYNTHETIC_CODE = "4xJTwA8-0ZEyaqiy1Xi60baeetlN5QA68tkMCG6y7NY"
SYNTHETIC_TOKEN = "nGo1eX7venMoo-OkO0KuCTTX-d1Ew9GFVD0QRneR4fXSRXosp3nThQ"


def leaked_part(secret, text, run=4):
    """The first run of ``run`` consecutive characters of ``secret`` found in ``text``, or None."""
    return next(
        (secret[i : i + run] for i in range(len(secret) - run + 1) if secret[i : i + run] in text), None
    )


@pytest.fixture
def synthetic_secrets(monkeypatch):
    """A launch code and a session token protected through the app's own calls.

    The code is protected by ``LaunchSignIn.issue`` and the token by ``LaunchSignIn.redeem``,
    with ``secrets.token_urlsafe`` returning the fixed synthetic values.
    """
    real = desktop.secrets.token_urlsafe
    fixed = {32: SYNTHETIC_CODE, 40: SYNTHETIC_TOKEN}
    monkeypatch.setattr(desktop.secrets, "token_urlsafe", lambda n=None: fixed.get(n) or real(n))
    return SYNTHETIC_CODE, SYNTHETIC_TOKEN


class ThreePlaces:
    """desktop.log, a handler present when the guard ran, and one added afterwards to a child."""

    def __init__(self, data):
        self.data = data
        self.library = logging.getLogger("synthetic.round5.library")
        self.guarded_stream, self.late_stream = io.StringIO(), io.StringIO()
        self.guarded = logging.StreamHandler(self.guarded_stream)
        self.library.addHandler(self.guarded)
        self.library.setLevel(logging.DEBUG)
        desktop.configure_logging(data)
        self.child = logging.getLogger("synthetic.round5.library.child")
        self.late = logging.StreamHandler(self.late_stream)
        self.child.addHandler(self.late)

    def texts(self):
        for handler in (self.guarded, self.late):
            handler.flush()
        return {
            "desktop.log": (self.data / "desktop.log").read_text(),
            "guarded handler": self.guarded_stream.getvalue(),
            "late handler": self.late_stream.getvalue(),
        }

    def close(self):
        self.library.removeHandler(self.guarded)
        self.child.removeHandler(self.late)


@pytest.fixture
def three_places(tmp_path, isolated_logging):
    places = ThreePlaces(desktop.prepare_data_dir(tmp_path / "data"))
    try:
        yield places
    finally:
        places.close()


def signed_in_secrets(data):
    store = Store(data / "foundry.db")
    store.bootstrap()
    signin = LaunchSignIn(store, "127.0.0.1:9")
    code = signin.issue().rsplit("=", 1)[1]
    token = signin.redeem(code)
    return code, token


def test_a_format_that_shortens_an_argument_prints_no_part_of_a_secret(three_places, synthetic_secrets):
    code, token = signed_in_secrets(three_places.data)
    assert (code, token) == synthetic_secrets  # protected by issue() and by redeem()
    for secret in (code, token):
        three_places.child.warning("precision %.40s", secret)
        three_places.child.warning("%.40r", ("x", secret))
        three_places.child.warning("%-20.20s|", secret)
        three_places.child.warning("%(a).40s", {"a": secret})
    for where, text in three_places.texts().items():
        for secret in (code, token):
            assert leaked_part(secret, text) is None, where
        # The records were written, with the redacted text in place of each argument.
        assert text.count("precision [redacted]") == 2, where
        assert text.count("('x', '[redacted]')") == 2, where
        assert text.count("[redacted]          |") == 2, where
        # The mapping record too: written, not withheld, with the redacted text as its message.
        assert messages(where, text).count(desktop.REDACTED) == 2, where
        assert "withheld" not in text, where


def messages(where, text):
    """The test loggers' messages: desktop.log's lines start with the time, level and logger name,
    and it also holds the app's own lines."""
    if where != "desktop.log":
        return text.splitlines()
    lines = [line.split(": ", 1) for line in text.splitlines()]
    return [message for prefix, message in lines if prefix.rsplit(" ", 1)[1].startswith("synthetic.")]


class Keep(logging.Handler):
    """Keeps the records it is given, to look at what a handler receives."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


@contextlib.contextmanager
def kept(logger):
    keep = Keep()
    logger.addHandler(keep)
    try:
        yield keep.records
    finally:
        logger.removeHandler(keep)


def assert_finished(records, expected):
    """Each record holds only its finished text: no arguments and no exception object."""
    assert [record.getMessage() for record in records] == expected
    assert [record.msg for record in records] == expected
    assert all(type(record.msg) is str for record in records)
    assert all(record.args is None and record.exc_info is None for record in records)


def test_a_mapping_argument_without_a_secret_is_written_as_finished_text_at_every_place(
    three_places, synthetic_secrets
):
    signed_in_secrets(three_places.data)
    mapping = {"count": 7, "name": "synthetic"}
    with kept(three_places.child) as records:
        three_places.child.warning("%(count)d items for %(name)s", mapping)
    expected = "%(count)d items for %(name)s" % mapping  # noqa: UP031 (the way logging formats)
    assert_finished(records, [expected])
    for where, text in three_places.texts().items():
        assert messages(where, text) == [expected], where
        assert "withheld" not in text, where


def test_a_mapping_key_that_holds_a_secret_is_printed_with_the_secret_replaced(
    three_places, synthetic_secrets
):
    code, token = signed_in_secrets(three_places.data)
    for secret in (code, token):
        three_places.child.warning("whole mapping %s", {secret: 1})
        three_places.child.warning("by name %(name)s", {"name": "synthetic", secret: 2})
    for where, text in three_places.texts().items():
        for secret in (code, token):
            assert leaked_part(secret, text) is None, where
        assert messages(where, text) == ["whole mapping {'[redacted]': 1}", "by name synthetic"] * 2, where


def test_a_secret_in_an_extra_field_does_not_reach_the_log_file(three_places, synthetic_secrets):
    code, token = signed_in_secrets(three_places.data)
    for secret in (code, token):
        three_places.child.warning("with an extra field", extra={"synthetic_field": secret})
    written = three_places.texts()["desktop.log"]
    for secret in (code, token):
        assert leaked_part(secret, written) is None
    assert messages("desktop.log", written) == ["with an extra field"] * 2


def test_records_built_outside_the_usual_calls_reach_no_guarded_place(three_places, synthetic_secrets):
    code, token = signed_in_secrets(three_places.data)
    for secret in (code, token):
        for fields in (
            {"msg": f"in the message {secret}"},
            {"msg": "in the arguments %.40s", "args": (secret,)},
        ):
            record = logging.makeLogRecord(
                {
                    "name": three_places.library.name,
                    "levelno": logging.WARNING,
                    "levelname": "WARNING",
                    **fields,
                }
            )
            three_places.library.handle(record)
    texts = three_places.texts()
    for where in ("desktop.log", "guarded handler"):
        for secret in (code, token):
            assert leaked_part(secret, texts[where]) is None, where
        assert texts[where].count("in the message [redacted]") == 2, where
        assert texts[where].count("in the arguments [redacted]") == 2, where


def test_ordinary_arguments_are_written_as_finished_text_at_every_place(three_places, synthetic_secrets):
    signed_in_secrets(three_places.data)
    ratio, name = 2.0 / 3.0, types.SimpleNamespace(label="synthetic")
    args = (7, ratio, name)
    with kept(three_places.child) as records:
        three_places.child.warning("%d items, %.2f ratio, %s name", *args)
    expected = "%d items, %.2f ratio, %s name" % args  # noqa: UP031 (the way logging formats)
    assert_finished(records, [expected])
    for where, text in three_places.texts().items():
        assert messages(where, text) == [expected], where
        assert "withheld" not in text, where


def test_an_argument_that_cannot_be_turned_into_text_is_withheld_without_raising(
    three_places, synthetic_secrets
):
    code, _ = signed_in_secrets(three_places.data)

    class BadRepr:
        def __str__(self):
            return code

        def __repr__(self):
            raise RuntimeError("no repr")

    class BadStr:
        def __str__(self):
            raise RuntimeError("no str")

        def __repr__(self):
            return code

    # The conversion the format asks for raises: the record is withheld.
    three_places.child.warning("shortened repr %.20r", BadRepr())  # does not raise
    three_places.child.warning("shortened str %.20s", BadStr())
    # Only the conversion the format asks for is made, so these print their redacted text.
    three_places.child.warning("shortened str %.20s", BadRepr())
    three_places.child.warning("shortened repr %.20r", BadStr())
    withheld = "(a log record withheld: it could not be checked for secrets)"
    for where, text in three_places.texts().items():
        assert leaked_part(code, text) is None, where
        assert messages(where, text) == [withheld] * 2 + [
            "shortened str [redacted]",
            "shortened repr [redacted]",
        ]


class Changing:
    """A log argument whose text is the secret on some of its conversions and harmless on the others.

    ``str()`` and ``repr()`` are counted apart, from 1, so a check that converts the argument
    both ways and then leaves it to be converted again sees it harmless.
    """

    def __init__(self, secret, secret_on):
        self.secret, self.secret_on = secret, secret_on
        self.calls = {"str": 0, "repr": 0}

    def _text(self, kind):
        self.calls[kind] += 1
        return self.secret if self.secret_on(self.calls[kind]) else "harmless synthetic text"

    def __str__(self):
        return self._text("str")

    def __repr__(self):
        return self._text("repr")


CHANGING = {
    "harmless first, then the secret": lambda n: n > 1,
    "harmless on odd conversions, the secret on even ones": lambda n: n % 2 == 0,
    "the secret from the first": lambda n: True,
}


def test_an_argument_whose_text_changes_prints_no_part_of_a_secret(three_places, synthetic_secrets):
    code, token = signed_in_secrets(three_places.data)
    expected, arguments = [], []
    for secret_on in CHANGING.values():
        first = desktop.REDACTED if secret_on(1) else "harmless synthetic t"
        for secret in (code, token):
            lazy, twice, value, shown = (Changing(secret, secret_on) for _ in range(4))
            three_places.child.warning("lazy %.20s", lazy)
            three_places.child.warning("twice %.20s %.20s", twice, twice)
            three_places.child.warning("mapping %(a).20s", {"a": value})
            three_places.child.warning("repr %.20r", shown)
            # The second conversion of ``twice`` is its second str().
            second = desktop.REDACTED if secret_on(2) else "harmless synthetic t"
            expected += [f"lazy {first}", f"twice {first} {second}", f"mapping {first}", f"repr {first}"]
            arguments += [(lazy, 1, 0), (twice, 2, 0), (value, 1, 0), (shown, 0, 1)]
    for where, text in three_places.texts().items():
        for secret in (code, token):
            assert leaked_part(secret, text) is None, where
        assert messages(where, text) == expected, where
    # Each argument was converted as many times as the format names it, and no more.
    for argument, strs, reprs in arguments:
        assert argument.calls == {"str": strs, "repr": reprs}


class ChangingMessage:
    """A message object whose text is harmless the first time and holds the secret afterwards."""

    def __init__(self, secret, form):
        self.secret, self.form, self.calls = secret, form, 0

    def __str__(self):
        self.calls += 1
        return self.form % ("harmless" if self.calls == 1 else self.secret)


def test_a_message_object_whose_text_changes_prints_no_part_of_a_secret(three_places, synthetic_secrets):
    code, token = signed_in_secrets(three_places.data)
    messages_logged = []
    for secret in (code, token):
        alone, with_args = ChangingMessage(secret, "alone %s"), ChangingMessage(secret, "with %s and %%s")
        three_places.child.warning(alone)
        three_places.child.warning(with_args, "an argument")
        messages_logged += [alone, with_args]
    for where, text in three_places.texts().items():
        for secret in (code, token):
            assert leaked_part(secret, text) is None, where
        assert messages(where, text) == ["alone harmless", "with harmless and an argument"] * 2, where
    assert [message.calls for message in messages_logged] == [1] * 4


class ChangingError(Exception):
    """An exception whose text is harmless the first time and holds the secret afterwards."""

    def __init__(self, secret):
        super().__init__()
        self.secret, self.calls = secret, 0

    def __str__(self):
        self.calls += 1
        return "harmless" if self.calls == 1 else f"failed with {self.secret}"


def test_an_exception_whose_text_changes_prints_no_part_of_a_secret(three_places, synthetic_secrets):
    code, token = signed_in_secrets(three_places.data)
    errors = []
    for secret in (code, token):
        for log in (
            lambda: three_places.child.exception("failed"),
            lambda: three_places.child.warning("failed", exc_info=True),
        ):
            error = ChangingError(secret)
            errors.append(error)
            try:
                raise error
            except ChangingError:
                log()
    for where, text in three_places.texts().items():
        for secret in (code, token):
            assert leaked_part(secret, text) is None, where
        assert text.count("ChangingError: harmless") == 4, where
    assert [error.calls for error in errors] == [1] * 4


def test_an_argument_is_converted_once_and_prints_the_same_text_at_every_place(
    three_places, synthetic_secrets
):
    signed_in_secrets(three_places.data)

    class Counter:
        calls = 0

        def __str__(self):
            self.calls += 1
            return f"counter call {self.calls}"

    counter = Counter()
    three_places.child.warning("counted %s", counter)
    assert counter.calls == 1
    for where, text in three_places.texts().items():
        assert messages(where, text) == ["counted counter call 1"], where


def test_a_start_address_loses_its_whole_query_however_its_code_is_written(three_places, synthetic_secrets):
    code, _ = signed_in_secrets(three_places.data)
    start = "http://127.0.0.1:9/desktop/start?"
    # The first characters percent-encoded: two, because this code starts with "4" and "%34" ends
    # with "4", so encoding only the first would leave the whole code in the text.
    encoded = "".join(f"%{ord(character):02X}" for character in code[:2]) + code[2:]
    plain = [
        start + "code=" + encoded,
        start + "code=" + encoded[:30],
        start + "c%6Fde=" + code[:30],
        start + "other=1&code=" + encoded[:30],
        start + "code=" + code[:30],
        start + "code=" + code[:30] + "#fragment",
    ]
    for address in plain:
        three_places.child.warning("address %s end", address)
    three_places.child.warning("quoted %r end", start + "code=" + code[:30])
    three_places.child.info("%s %s %s", "GET", "/desktop/start", 303)  # the app's request line
    redacted = start + desktop.REDACTED
    for where, text in three_places.texts().items():
        lines = messages(where, text)
        assert lines == [f"address {redacted} end"] * 6 + [
            f"quoted '{redacted} end",
            "GET /desktop/start 303",
        ]
        for line in lines:
            for shown in (line, unquote(line)):
                assert leaked_part(code, shown) is None, where


def test_redacting_finished_text_again_changes_nothing(three_places, synthetic_secrets):
    code, _ = signed_in_secrets(three_places.data)
    with kept(three_places.child) as records:
        three_places.child.warning("finished %s", "has %s, %d and 100%")
        try:
            raise RuntimeError("synthetic failure, 100%")
        except RuntimeError:
            three_places.child.exception("failed at %s", "/desktop/start?code=" + code[:20], stack_info=True)
    for where, text in three_places.texts().items():
        lines = text.splitlines()
        if where == "desktop.log":  # its lines start with the time, level and logger name
            lines = [line.split(f"{three_places.child.name}: ", 1)[-1] for line in lines]
        assert "finished has %s, %d and 100%" in lines, where
        assert (
            "failed at /desktop/start?[redacted]" in text and "RuntimeError: synthetic failure, 100%" in text
        )
    assert_finished(records, ["finished has %s, %d and 100%", "failed at /desktop/start?[redacted]"])
    assert records[1].exc_text.endswith("RuntimeError: synthetic failure, 100%") and records[1].stack_info
    for record in records:
        before = (record.msg, record.args, record.exc_text, record.stack_info)
        desktop.LOG_REDACTION.scrub(record)
        assert (record.msg, record.args, record.exc_text, record.stack_info) == before
    again = desktop.LOG_REDACTION.redact("/desktop/start?" + desktop.REDACTED)
    assert again == "/desktop/start?" + desktop.REDACTED


class Shade(enum.IntEnum):
    DARK = 42


NUMBER_LIKE = [
    ("%d", (decimal.Decimal("7.9"),)),
    ("%.2f", (decimal.Decimal("2.345"),)),
    ("%d %s %x", (Shade.DARK, Shade.DARK, Shade.DARK)),
    ("%.1f", (fractions.Fraction(7, 3),)),
    ("%c", (65,)),
    ("%c", ("z",)),
    ("%*d", (6, 3)),
    ("%.*s", (3, "abcdef")),
    ("%s", (Path("synthetic") / "folder",)),
]


def test_number_like_arguments_print_as_without_the_redaction(three_places, synthetic_secrets):
    signed_in_secrets(three_places.data)
    with kept(three_places.child) as records:
        for form, args in NUMBER_LIKE:
            three_places.child.warning(form, *args)
    expected = [form % args for form, args in NUMBER_LIKE]
    assert_finished(records, expected)
    for where, text in three_places.texts().items():
        assert messages(where, text) == expected, where


def test_a_record_built_with_make_log_record_and_a_changing_argument_reaches_no_guarded_place(
    three_places, synthetic_secrets
):
    code, token = signed_in_secrets(three_places.data)
    for secret in (code, token):
        for secret_on in CHANGING.values():
            record = logging.makeLogRecord(
                {
                    "name": three_places.library.name,
                    "levelno": logging.WARNING,
                    "levelname": "WARNING",
                    "msg": "built %.20s",
                    "args": (Changing(secret, secret_on),),
                }
            )
            three_places.library.handle(record)
    texts = three_places.texts()
    for where in ("desktop.log", "guarded handler"):
        for secret in (code, token):
            assert leaked_part(secret, texts[where]) is None, where
        assert len(messages(where, texts[where])) == 6, where


def log_with_a_stack(logger, filename):
    """Log through ``logger`` with ``stack_info=True`` from a function compiled under ``filename``,
    so that a ``File "…"`` line of the stack holds that name."""
    namespace = {}
    source = "def log(logger):\n    logger.warning('with a stack', stack_info=True)\n"
    exec(compile(source, filename, "exec"), namespace)
    namespace["log"](logger)


def test_a_secret_or_a_start_address_in_the_stack_is_replaced_at_every_place(three_places, synthetic_secrets):
    code, token = signed_in_secrets(three_places.data)
    for secret in (code, token):
        log_with_a_stack(three_places.child, f"/synthetic/{secret}/mod.py")
    log_with_a_stack(three_places.child, "/synthetic/desktop/start?code=" + code[:30])
    # A record built another way, with stack text that holds the secret.
    for secret in (code, token):
        record = logging.makeLogRecord(
            {
                "name": three_places.library.name,
                "levelno": logging.WARNING,
                "levelname": "WARNING",
                "msg": "built with a stack",
                "stack_info": f'Stack (most recent call last):\n  File "/synthetic/built/{secret}/mod.py"',
            }
        )
        three_places.library.handle(record)
    texts = three_places.texts()
    for where, text in texts.items():
        for secret in (code, token):
            assert leaked_part(secret, text) is None, where
        assert text.count('File "/synthetic/[redacted]/mod.py", line 2, in log') == 2, where
        assert text.count('File "/synthetic/desktop/start?[redacted] line 2, in log') == 1, where
    for where in ("desktop.log", "guarded handler"):
        assert texts[where].count('File "/synthetic/built/[redacted]/mod.py"') == 2, where


class Text(str):
    """A ``str`` subclass: the format is given a stand-in for it, not the text itself."""


def test_a_number_format_given_a_str_subclass_is_refused_and_written_as_not_fitting(
    three_places, synthetic_secrets
):
    signed_in_secrets(three_places.data)
    forms = [("n=%d", Text("123")), ("f=%.1f", Text("1.5")), ("x=%x", Text("1"))]
    for form, value in forms:
        with pytest.raises(TypeError):
            form % value  # Python alone refuses each
    with kept(three_places.child) as records:
        for form, value in forms:
            three_places.child.warning(form, value)
    expected = ["n=%d ('123',)", "f=%.1f ('1.5',)", "x=%x ('1',)"]
    assert_finished(records, expected)
    for where, text in three_places.texts().items():
        assert messages(where, text) == expected, where


class Counted:
    """Prints ``strN`` or ``reprN``, with N counting its conversions of either kind."""

    def __init__(self):
        self.calls = 0

    def _text(self, kind):
        self.calls += 1
        return f"{kind}{self.calls}"

    def __str__(self):
        return self._text("str")

    def __repr__(self):
        return self._text("repr")


def test_arguments_that_do_not_fit_are_written_after_the_message_at_every_place(
    three_places, synthetic_secrets, capfd
):
    code, token = signed_in_secrets(three_places.data)
    counter = Counted()
    for form, args in (("two %s %s", ("one",)), ("count %s %d", (Counted(), "not a number"))):
        with pytest.raises(TypeError):
            form % args  # Python alone refuses each
    with kept(three_places.child) as records:
        three_places.child.warning("two %s %s", "one")  # does not raise
        for secret in (code, token):
            three_places.child.warning("misfit %s %s", secret)
        three_places.child.warning("count %s %d", counter, "not a number")
    expected = [
        "two %s %s ('one',)",
        "misfit %s %s ('[redacted]',)",
        "misfit %s %s ('[redacted]',)",
        "count %s %d (repr2, 'not a number')",
    ]
    assert_finished(records, expected)
    # Converted once by %s before %d failed, and once more by the arguments' repr.
    assert counter.calls == 2
    for where, text in three_places.texts().items():
        for secret in (code, token):
            assert leaked_part(secret, text) is None, where
        assert messages(where, text) == expected, where
    _, err = capfd.readouterr()
    assert "Logging error" not in err and "Traceback" not in err


class Width(enum.IntEnum):
    EIGHT = 8


class Count(int):
    pass


class Ratio(float):
    pass


# Each prints the same without the redaction and with it.
NEIGHBOURS_OF_THE_DIFFERENCE = [
    ("c=%c", (Count(66),)),
    ("f=%5.2f", (Ratio(1.25),)),
]


def test_known_difference_three_forms_that_fit_python_alone_are_written_as_not_fitting(
    three_places, synthetic_secrets, capfd
):
    signed_in_secrets(three_places.data)
    differing = [
        ("c=%c", (Text("x"),), "c=x", "c=%c ('x',)"),
        ("w=%*d", (Width.EIGHT, 5), "w=       5", "w=%*d (<Width.EIGHT: 8>, 5)"),
        ("p=%.*s", (Count(3), "abcdef"), "p=abc", "p=%.*s (3, 'abcdef')"),
    ]
    with kept(three_places.child) as records:
        for form, args, _, _ in differing:
            three_places.child.warning(form, *args)  # does not raise
        for form, args in NEIGHBOURS_OF_THE_DIFFERENCE:
            three_places.child.warning(form, *args)
    assert [form % args for form, args, _, _ in differing] == [alone for _, _, alone, _ in differing]
    expected = [written for _, _, _, written in differing]
    expected += [form % args for form, args in NEIGHBOURS_OF_THE_DIFFERENCE]
    assert expected[3:] == ["c=B", "f= 1.25"]
    assert_finished(records, expected)
    for where, text in three_places.texts().items():
        assert messages(where, text) == expected, where
    _, err = capfd.readouterr()
    assert "Logging error" not in err and "Traceback" not in err


class OwnExceptionText(logging.Formatter):
    """A formatter with its own exception text, counting the times it is asked for it."""

    def __init__(self):
        super().__init__()
        self.calls = 0

    def formatException(self, ei):
        self.calls += 1
        return "synthetic marker"


def test_a_formatter_with_its_own_exception_text_is_not_asked_for_it(three_places, synthetic_secrets):
    code, token = signed_in_secrets(three_places.data)
    stream, formatter = io.StringIO(), OwnExceptionText()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    three_places.child.addHandler(handler)
    try:
        with kept(three_places.child) as records:
            for secret in (code, token):
                try:
                    raise RuntimeError(f"failed with {secret}")
                except RuntimeError:
                    three_places.child.exception("failed")
    finally:
        three_places.child.removeHandler(handler)
    text = stream.getvalue()
    for secret in (code, token):
        assert leaked_part(secret, text) is None
    assert text.count("Traceback (most recent call last)") == 2
    assert text.count("RuntimeError: failed with [redacted]") == 2
    assert "synthetic marker" not in text and formatter.calls == 0
    assert_finished(records, ["failed", "failed"])  # exc_info is None on each


PYWEBVIEW_CHILD = textwrap.dedent("""\
    import sys
    from pathlib import Path
    from foundry import desktop
    from foundry.store import Store

    data, probe = Path(sys.argv[1]), Path(sys.argv[2])
    desktop.prepare_data_dir(data)
    desktop.configure_logging(data)
    store = Store(data / "foundry.db")
    store.bootstrap()
    url = desktop.LaunchSignIn(store, "127.0.0.1:9").issue()
    probe.write_text(url.rsplit("=", 1)[1])
    import webview  # reads PYWEBVIEW_LOG and adds its own stderr handler

    # open_window would also call desktop.guard_log_handlers(); without it, this checks that the
    # record factory alone is enough.
    window = webview.create_window("Synthetic", html="<p>synthetic</p>")
    loaded = []

    class Gui:  # stands in for the GUI backend: nothing is drawn
        def load_url(self, address, uid):
            loaded.append(address)

    window.gui = Gui()
    window.events.shown.set()
    window.load_url(url)  # pywebview's own method, which logs the address at DEBUG
    assert loaded == [url]
""")


@pytest.mark.skipif(importlib.util.find_spec("webview") is None, reason="needs the desktop extra")
def test_pywebview_debug_logging_does_not_write_the_launch_code(tmp_path):
    data, probe = tmp_path / "data", tmp_path / "code"
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYWEBVIEW")}
    child = subprocess.run(
        [sys.executable, "-c", PYWEBVIEW_CHILD, str(data), str(probe)],
        env={**env, "PYWEBVIEW_LOG": "debug"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert child.returncode == 0, child.stderr
    code = probe.read_text()
    assert code not in child.stderr and code not in child.stdout
    for path in data.rglob("*"):
        if path.is_file():
            assert code.encode() not in path.read_bytes(), path.name
    # The records were written, without the code.
    redacted = "Loading URL: http://127.0.0.1:9/desktop/start?[redacted]"
    assert redacted in (data / "desktop.log").read_text()
    assert "[pywebview] " + redacted in child.stderr


# What the desktop server answers without a session. Every route it registers must be listed
# here or refuse with 401, and each listed path must be named in SECURITY.md's desktop section.
DESKTOP_PUBLIC = {
    ("GET", "/"): 403,  # the plain instruction page
    ("GET", "/desktop/start"): 403,  # the plain page, without a valid launch code
    ("GET", "/static/"): 200,  # the interface's own scripts and styles
    ("GET", "/api/health"): 200,  # status and version
    ("GET", "/api/openapi.json"): 200,  # the API's schema
    ("POST", "/api/login"): 401,  # signs in only with a valid access key
}


def desktop_section():
    text = (Path(__file__).parents[1] / "SECURITY.md").read_text()
    return text.split("### Desktop app", 1)[1].split("\n## ", 1)[0]


def test_without_a_session_the_desktop_server_answers_only_its_documented_public_routes(tmp_path):
    from starlette.routing import Mount

    store = Store(tmp_path / "foundry.db")
    store.bootstrap()
    signin = LaunchSignIn(store, "testserver")
    signin.issue()  # a code exists, as it does while the window signs in
    app = create_app(store.path, allowed_hosts=["testserver"], desktop=signin)
    seen = set()
    with TestClient(app) as client:
        for route in app.routes:
            if isinstance(route, Mount):
                found = [("GET", route.path + "/", "/static/app.js")]
            else:
                path = route.path.replace("{", "").replace("}", "")  # {bid} -> bid, for example
                found = [(m, route.path, path) for m in sorted(route.methods - {"HEAD"})]
            for method, key, path in found:
                seen.add((method, key))
                body = {"token": "bf_" + "0" * 40} if path == "/api/login" else {}  # not a real key
                response = client.request(
                    method,
                    path,
                    json=body if method != "GET" else None,
                    headers={"Origin": signin.origin, "Idempotency-Key": "walk"},
                    follow_redirects=False,
                )
                expected = DESKTOP_PUBLIC.get((method, key), 401)
                assert response.status_code == expected, (method, key, response.status_code)
                assert session_cookie(response) == []
    assert set(DESKTOP_PUBLIC) <= seen  # the list names only routes that exist
    section = desktop_section()
    for _, path in DESKTOP_PUBLIC:
        assert f"`{path}`" in section, path


# Shutdown and single instance ---------------------------------------------------------------


def test_stopping_closes_the_port_ends_the_session_and_checkpoints(tmp_path):
    data = desktop.prepare_data_dir(tmp_path / "data")
    app = DesktopApp(data)
    app.start()
    url = app.signin.issue()
    # Another open connection (for example a CLI backup) keeps SQLite from folding the WAL
    # back on its own, so the shutdown checkpoint is what moves the data into the main file.
    keeper = sqlite3.connect(data / "foundry.db")
    keeper.execute("SELECT 1 FROM metadata").fetchall()
    try:
        with httpx.Client() as window:
            token = window.get(url).cookies["foundry_session"]
            window.post(
                app.signin.origin + "/api/businesses",
                json={"name": "Synthetic shop", "concept": "A synthetic business used only in this test."},
                headers={"Idempotency-Key": "k", "Origin": app.signin.origin},
            ).raise_for_status()
        wal = data / "foundry.db-wal"
        assert wal.stat().st_size > 0
        app.stop()
        app.stop()  # idempotent
        assert wal.stat().st_size == 0
    finally:
        keeper.close()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", app.port), timeout=1)
    # Read the main database file alone, ignoring any WAL: everything is in it.
    main_file = sqlite3.connect((data / "foundry.db").as_uri() + "?immutable=1", uri=True)
    try:
        assert main_file.execute("SELECT COUNT(*) FROM businesses").fetchone() == (1,)
        count = main_file.execute("SELECT COUNT(*) FROM sessions WHERE token_hash=?", (digest(token),))
        assert count.fetchone() == (0,)
    finally:
        main_file.close()


# Runs desktop.main() in a child process with a stand-in window. A thread is started before main,
# as a packaged app's bootstrap or an early GUI import would, and SIGTERM is blocked on the main
# thread, so the operating system must deliver the signal to that foreign thread. Before this
# was handled, that killed the process with no cleanup. The mode says what the window and the
# app's shutdown do once the signal has arrived.
SIGNAL_CHILD = textwrap.dedent("""\
    import signal, sys, threading, time
    from pathlib import Path
    import httpx
    from foundry import desktop

    threading.Thread(target=time.sleep, args=(600,), daemon=True).start()  # foreign, unmasked
    mode, data = sys.argv[1], Path(sys.argv[2])
    desktop.SIGNAL_CLOSE_SECONDS, desktop.SIGNAL_EXIT_SECONDS = float(sys.argv[3]), float(sys.argv[4])
    forever = threading.Event()  # never set

    def fake_window(app, signals):
        closed = threading.Event()

        def destroy():
            if mode == "ignored":
                return  # a wedged GUI that returns at once and closes nothing
            if mode == "destroy-hangs":
                forever.wait()  # never returns (pywebview's destroy can wait on the GUI thread)
            if mode == "destroy-raises":
                raise RuntimeError("synthetic window failure")
            if mode == "stop-hangs":
                # The window's own closing handler starts stopping and is stuck inside it,
                # holding the stop lock.
                app.signin.end_sessions = forever.wait
                threading.Thread(target=app.stop, daemon=True).start()
                return
            app.stop()  # what the window's closing event does
            if mode != "gui-hangs":  # gui-hangs: cleaned up, but the GUI loop never returns
                closed.set()

        signals.close_window = destroy
        client = httpx.Client()
        assert desktop.sign_in_window(app.signin, lambda url: client.get(url), lambda: None)
        client.post(
            app.signin.origin + "/api/businesses",
            json={"name": "Synthetic shop", "concept": "A synthetic business used only in a test."},
            headers={"Idempotency-Key": "k", "Origin": app.signin.origin},
        ).raise_for_status()
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
        print("READY", flush=True)
        closed.wait()

    if mode == "early":
        made, real_init, real_start = [], desktop.SignalShutdown.__init__, desktop.DesktopApp.start

        def init(self, *args, **kwargs):
            real_init(self, *args, **kwargs)
            made.append(self)

        def start_then_signal(self):
            signin = real_start(self)
            signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
            print("READY", flush=True)
            assert made[0].received.wait(60)  # the signal arrives while the server is starting
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
            return signin

        desktop.SignalShutdown.__init__ = init
        desktop.DesktopApp.start = start_then_signal
        desktop.open_window = lambda app, signals: print("WINDOW OPENED", flush=True)
    else:
        desktop.open_window = fake_window

    if mode in ("stop-hangs", "gui-hangs"):
        # These cases end at the reader's limit, and what they check must have happened before
        # it: the second stage's warning written (stop-hangs), or the cleanup finished
        # (gui-hangs). Left to the scheduler, a child that is not run for a moment can wake past
        # its deadline and end before the other thread has acted. So once the reader has taken
        # the signal's time, each later look at its clock waits until that has happened. In
        # stop-hangs the reader is also let go when the shutdown thread ends, which the app's
        # code does only after the warning, so a version that skips the warning fails at once.
        # (In gui-hangs the shutdown thread can end before the cleanup has finished, when it
        # wakes past the deadline, so only the finished cleanup lets the reader go.) The deadline
        # and the os._exit are still the reader's own: it never ends the process before its
        # limit, and once let go it ends it as soon as the limit is reached.
        made, happened = [], threading.Event()
        real_init, real_shut_down = desktop.SignalShutdown.__init__, desktop.SignalShutdown._shut_down

        def init(self, *args, **kwargs):
            made.append(self)
            real_init(self, *args, **kwargs)

        def shut_down(self):
            try:
                real_shut_down(self)
            finally:
                if mode == "stop-hangs":
                    happened.set()

        class Log:
            def __getattr__(self, name):
                return getattr(real_log, name)

            def warning(self, msg, *args, **kwargs):
                real_log.warning(msg, *args, **kwargs)  # written to desktop.log when this returns
                if mode == "stop-hangs" and "stopping now" in msg:
                    happened.set()

        class Clock:
            def __getattr__(self, name):
                return getattr(time, name)

            def monotonic(self):
                reader = threading.current_thread().name == "foundry-signals"
                if reader and made and made[0].received.is_set():
                    happened.wait()
                return time.monotonic()

        real_stop = desktop.DesktopApp.stop

        def stop(self):
            real_stop(self)
            if mode == "gui-hangs" and self.stopped:
                happened.set()

        real_log = desktop.log
        desktop.SignalShutdown.__init__, desktop.SignalShutdown._shut_down = init, shut_down
        desktop.log, desktop.time, desktop.DesktopApp.stop = Log(), Clock(), stop
    desktop.main(["--data-dir", str(data)])
    print("MAIN RETURNED", flush=True)
""")


def run_until_signalled(tmp_path, mode, close_seconds=60, exit_seconds=120):
    """Returns the exit code, stdout, data folder and seconds from the signal to the exit.

    With the defaults neither limit is reached, so nothing depends on timing; a mode that tests
    a limit sets that limit short.
    """
    data = tmp_path / "data"
    child = subprocess.Popen(
        [sys.executable, "-c", SIGNAL_CHILD, mode, str(data), str(close_seconds), str(exit_seconds)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "READY"
        signalled = time.monotonic()
        child.send_signal(signal.SIGTERM)
        out, _ = child.communicate(timeout=exit_seconds + 60)
        elapsed = time.monotonic() - signalled
    finally:
        child.kill()
    return child.returncode, out, data, elapsed


def read_log(data):
    return (data / "desktop.log").read_text()


def assert_cleaned_up(data):
    log = read_log(data)
    assert "Received signal 15" in log and "Stopped cleanly." in log
    wal = data / "foundry.db-wal"
    assert not wal.exists() or wal.stat().st_size == 0  # checkpointed (or removed on last close)
    main_file = sqlite3.connect((data / "foundry.db").as_uri() + "?immutable=1", uri=True)
    try:
        assert main_file.execute("SELECT COUNT(*) FROM sessions").fetchone() == (0,)
        return main_file.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]
    finally:
        main_file.close()


posix_signals = pytest.mark.skipif(not hasattr(signal, "pthread_sigmask"), reason="POSIX signals")


@posix_signals
def test_sigterm_delivered_to_a_foreign_thread_still_shuts_down_cleanly(tmp_path):
    code, out, data, _ = run_until_signalled(tmp_path, "normal")
    assert code == 0 and "MAIN RETURNED" in out
    assert assert_cleaned_up(data) == 1
    assert "stopping now" not in read_log(data)


@posix_signals
@pytest.mark.parametrize("mode", ["ignored", "destroy-hangs", "destroy-raises"])
def test_sigterm_still_cleans_up_and_exits_when_the_window_will_not_close(tmp_path, mode):
    # The window returns without closing, never returns, or fails. The app runs the cleanup
    # itself once the window's time is up, and exits as soon as it is done: in about a second,
    # long before the reader's limit of 120 seconds would end it. (A shutdown thread that only
    # waited would be ended by the reader at 120 seconds; a pause of a few seconds is far from
    # either.)
    code, out, data, elapsed = run_until_signalled(tmp_path, mode, close_seconds=0.5)
    assert code == 128 + signal.SIGTERM and "MAIN RETURNED" not in out
    assert elapsed < 60
    assert assert_cleaned_up(data) == 1
    assert "did not stop within 0.5 seconds of the signal; stopping now" in read_log(data)


@posix_signals
@pytest.mark.parametrize("mode", ["stop-hangs", "gui-hangs"])
def test_sigterm_ends_the_process_within_the_limit_whatever_shutdown_is_doing(tmp_path, mode):
    # stop-hangs: a stop already in progress is stuck inside the cleanup, holding the stop lock.
    # gui-hangs: the cleanup finished, but the GUI loop never returns, so main() never does.
    # Either way the process ends, and not before SIGNAL_EXIT_SECONDS have passed since the
    # signal. The child holds the reader until what the case checks has happened (see
    # SIGNAL_CHILD), so no result depends on which thread the computer runs first.
    close_seconds = 0.5 if mode == "stop-hangs" else 2
    code, out, data, elapsed = run_until_signalled(tmp_path, mode, close_seconds, exit_seconds=2)
    assert code == 128 + signal.SIGTERM and "MAIN RETURNED" not in out
    assert 2 <= elapsed < 2 + 30  # the upper margin only allows for a slow machine
    log = read_log(data)
    assert "Received signal 15" in log
    if mode == "gui-hangs":
        assert assert_cleaned_up(data) == 1
    else:
        assert "Stopped cleanly." not in log and "stopping now" in log


@posix_signals
def test_sigterm_before_the_window_opens_skips_the_window(tmp_path):
    code, out, data, _ = run_until_signalled(tmp_path, "early")
    assert code == 0 and "WINDOW OPENED" not in out and "MAIN RETURNED" in out
    assert assert_cleaned_up(data) == 0
    assert "Stopping before the window opened." in read_log(data)


class _Exited(Exception):
    pass


class _Patched:
    """A module with some attributes replaced; every other attribute is the real module's."""

    def __init__(self, real, **replaced):
        self._real, self._replaced = real, replaced

    def __getattr__(self, name):
        return self._replaced[name] if name in self._replaced else getattr(self._real, name)


def test_the_signal_watcher_ends_the_process_exactly_at_the_limit(monkeypatch):
    # A controlled clock, so the deadline itself is checked and no wall-clock margin is needed.
    # Sleeps may return early (each advances the clock by at most 7 seconds); the process must
    # still end when the clock reads the signal's time plus SIGNAL_EXIT_SECONDS, not earlier and
    # not later.
    clock = {"now": 1000.0}
    exits = []

    def sleep(seconds):
        assert seconds > 0
        clock["now"] += min(seconds, 7.0)

    def exit_now(status):
        exits.append((clock["now"], status))
        raise _Exited

    monkeypatch.setattr(desktop, "time", _Patched(time, monotonic=lambda: clock["now"], sleep=sleep))
    monkeypatch.setattr(desktop, "os", _Patched(os, _exit=exit_now))
    watcher = object.__new__(desktop.SignalShutdown)
    watcher._reader, writer = socket.socketpair()
    try:
        watcher.signals = [signal.SIGTERM]
        watcher.received, watcher._go = threading.Event(), threading.Event()
        writer.send(bytes([signal.SIGTERM]))
        with pytest.raises(_Exited):
            watcher._watch()
    finally:
        watcher._reader.close()
        writer.close()
    assert exits == [(1000.0 + desktop.SIGNAL_EXIT_SECONDS, 128 + signal.SIGTERM)]
    assert watcher.received.is_set() and watcher._exit_at == 1000.0 + desktop.SIGNAL_EXIT_SECONDS


class _StoppingApp:
    """DesktopApp as the shutdown thread sees it, on a clock the test moves.

    A wait that times out moves the clock through its whole timeout and returns False. Once
    ``stops_after`` is set, a wait first lets the stop the shutdown thread started be called,
    then moves the clock by that many seconds (the cleanup's duration) and returns True.
    """

    def __init__(self, clock, *, stopping, stops_after=None):
        self.clock, self.stopping, self.stops_after = clock, stopping, stops_after
        self.waits, self.stops, self.stop_called = [], [], threading.Event()

    def wait_stopped(self, timeout):
        self.waits.append((self.clock["now"], timeout))
        if self.stops_after is not None and len(self.waits) == 2:
            # The test's limit only stops a broken version from hanging; no result depends on it.
            assert self.stop_called.wait(20), "the shutdown thread never started the stop"
            self.clock["now"] += self.stops_after
            return True
        self.clock["now"] += timeout
        return False

    def stop(self):
        self.stops.append(threading.current_thread().name)
        self.stop_called.set()


def shutdown_thread(monkeypatch, app, clock, exits):
    """A SignalShutdown whose signal (SIGTERM) arrived at 1000.0 on the test's clock, with limits
    of 4 seconds to close and 25 to exit; its ``os._exit`` is noted in ``exits`` and raises."""

    def exit_now(status):
        exits.append((clock["now"], status))
        raise _Exited

    monkeypatch.setattr(desktop, "SIGNAL_CLOSE_SECONDS", 4)
    monkeypatch.setattr(desktop, "SIGNAL_EXIT_SECONDS", 25)
    monkeypatch.setattr(desktop, "time", _Patched(time, monotonic=lambda: clock["now"]))
    monkeypatch.setattr(desktop, "os", _Patched(os, _exit=exit_now))
    shutdown = object.__new__(desktop.SignalShutdown)
    shutdown.app, shutdown.close_window = app, None
    shutdown._signum, shutdown._exit_at = signal.SIGTERM, 1025.0
    shutdown._go = threading.Event()
    shutdown._go.set()
    return shutdown


def test_the_shutdown_thread_stops_the_app_itself_and_exits_as_soon_as_that_is_done(monkeypatch, caplog):
    # The window has not closed when its 4 seconds are up: the shutdown thread runs the stop
    # itself and ends the process the moment the stop has finished (here 1.5 seconds later),
    # not at the reader's limit.
    clock = {"now": 1000.0}
    exits = []
    app = _StoppingApp(clock, stopping=False, stops_after=1.5)
    shutdown = shutdown_thread(monkeypatch, app, clock, exits)
    with caplog.at_level("INFO", logger="foundry.desktop"), pytest.raises(_Exited):
        shutdown._shut_down()
    assert app.waits == [(1000.0, 4.0), (1004.0, 21.0)]
    assert app.stops == ["foundry-stop"]
    assert exits == [(1005.5, 143)]
    assert "The app did not stop within 4 seconds of the signal; stopping now." in caplog.text


def test_the_second_stage_starts_although_a_stop_is_already_in_progress(monkeypatch, caplog):
    # A stop is in progress and never finishes. When the window's 4 seconds are up the warning
    # is still written and a stop is still started (it waits for the one in progress); the
    # shutdown thread then waits out the rest of the 25 seconds and leaves the exit to the reader.
    clock = {"now": 1000.0}
    exits = []
    app = _StoppingApp(clock, stopping=True)
    shutdown = shutdown_thread(monkeypatch, app, clock, exits)
    with caplog.at_level("INFO", logger="foundry.desktop"):
        shutdown._shut_down()
    assert "The app did not stop within 4 seconds of the signal; stopping now." in caplog.text
    assert app.stop_called.wait(20)  # the stop runs on a thread of its own
    assert app.waits == [(1000.0, 4.0), (1004.0, 21.0)]
    assert app.stops == ["foundry-stop"]
    assert exits == [] and clock["now"] == 1025.0


@pytest.mark.parametrize(
    "starts_at, waits",
    [
        # 1.5 seconds late: 2.5 of the window's 4 seconds are left, then 21 to the reader's limit.
        (1001.5, [(1001.5, 2.5), (1004.0, 21.0)]),
        # 7 seconds late, after the window's time is up: no wait, and the stop starts at once.
        (1007.0, [(1007.0, 0.0), (1007.0, 18.0)]),
    ],
)
def test_the_windows_time_to_close_is_counted_from_the_signal(monkeypatch, caplog, starts_at, waits):
    # The signal arrived at 1000.0, but the shutdown thread only gets to work later (for example
    # because its log call was held). The window's 4 seconds still end at 1004.0, not 4 seconds
    # after the thread started; a stop that never finishes leaves the exit to the reader at 1025.0.
    clock = {"now": starts_at}
    exits = []
    app = _StoppingApp(clock, stopping=True)
    shutdown = shutdown_thread(monkeypatch, app, clock, exits)
    with caplog.at_level("INFO", logger="foundry.desktop"):
        shutdown._shut_down()
    assert "The app did not stop within 4 seconds of the signal; stopping now." in caplog.text
    assert app.stop_called.wait(20)  # the stop runs on a thread of its own
    assert app.waits == waits
    assert app.stops == ["foundry-stop"]
    assert exits == [] and clock["now"] == 1025.0


# A signal raised at the moment SignalShutdown installs the handler for it, before the
# installation has finished. At 81bfbb4 the handlers were installed before the wakeup socket, so a
# signal in between reached only the no-op Python handler and was lost.
INSTALL_CHILD = textwrap.dedent("""\
    import signal, sys, threading, types
    from foundry import desktop

    target = getattr(signal, sys.argv[1])
    install = signal.signal

    def install_then_signal(sig, handler):
        previous = install(sig, handler)
        if sig == target and handler is desktop.SignalShutdown._noted:
            signal.raise_signal(target)
        return previous

    signal.signal = install_then_signal
    stopped = threading.Event()
    app = types.SimpleNamespace(stop=stopped.set, wait_stopped=stopped.wait)
    shutdown = desktop.SignalShutdown(app)
    print("RECEIVED" if shutdown.received.wait(30) else "LOST", flush=True)
""")


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
@pytest.mark.parametrize("name", ["SIGINT", "SIGTERM", "SIGHUP"])
def test_a_signal_during_installation_is_not_lost(name):
    child = subprocess.run(
        [sys.executable, "-c", INSTALL_CHILD, name], capture_output=True, text=True, timeout=120
    )
    assert child.stdout.strip() == "RECEIVED", child.stderr


def test_a_second_instance_is_refused_plainly(tmp_path):
    data = desktop.prepare_data_dir(tmp_path / "data")
    with desktop.instance_lock(data) as lock_file:
        if os.name == "posix":
            assert lock_file.stat().st_mode & 0o777 == 0o600
        with pytest.raises(DesktopError, match="already open"), desktop.instance_lock(data):
            pass
        script = (
            "import sys; from pathlib import Path; from foundry import desktop\n"
            "try:\n"
            "    with desktop.instance_lock(Path(sys.argv[1])): sys.exit(0)\n"
            "except desktop.DesktopError: sys.exit(3)\n"
        )
        assert subprocess.run([sys.executable, "-c", script, str(data)]).returncode == 3
    with desktop.instance_lock(data):  # released on exit
        pass


# Entry point and window guards --------------------------------------------------------------


def test_main_serves_signs_in_the_window_and_shuts_down(tmp_path, monkeypatch):
    seen = {}

    def fake_window(app, signals):
        with httpx.Client() as window:

            def navigate(url):
                seen["start"] = window.get(url).status_code

            assert desktop.sign_in_window(app.signin, navigate, lambda: None)
            seen["page"] = window.get(app.signin.origin + "/").status_code
            seen["me"] = window.get(app.signin.origin + "/api/me").json()["role"]
        seen["app"] = app
        seen["handler"] = signal.getsignal(signal.SIGTERM)

    before = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(desktop, "open_window", fake_window)
    monkeypatch.setattr(desktop, "configure_logging", lambda data_dir: data_dir / "desktop.log")
    monkeypatch.chdir("/")
    desktop.main(["--data-dir", str(tmp_path / "data"), "-psn_0_12345"])
    assert seen["start"] == 303 and seen["page"] == 200 and seen["me"] == "owner"
    assert seen["handler"] == desktop.SignalShutdown._noted
    assert signal.getsignal(signal.SIGTERM) == before  # restored on the way out
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", seen["app"].port), timeout=1)


def test_main_reports_a_second_launch(tmp_path, monkeypatch):
    shown = []
    monkeypatch.setattr(desktop, "show_error", shown.append)
    monkeypatch.setattr(desktop, "configure_logging", lambda data_dir: data_dir / "desktop.log")
    data = desktop.prepare_data_dir(tmp_path / "data")
    with desktop.instance_lock(data), pytest.raises(SystemExit) as exit_info:
        desktop.main(["--data-dir", str(data)])
    assert exit_info.value.code == 1
    assert shown and "already open" in shown[0]


def test_missing_streams_are_replaced(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    desktop.replace_missing_streams()
    print("written nowhere")
    print("written nowhere", file=sys.stderr)
    assert sys.stdout is not None and sys.stderr is not None


def test_windows_refuses_the_internet_explorer_engine(monkeypatch):
    guilib = types.ModuleType("webview.guilib")
    guilib.initialize = lambda gui: types.SimpleNamespace(renderer="mshtml")
    monkeypatch.setitem(sys.modules, "webview", types.ModuleType("webview"))
    monkeypatch.setitem(sys.modules, "webview.guilib", guilib)
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    with pytest.raises(DesktopError, match="WebView2"):
        desktop._require_edge_chromium()
    guilib.initialize = lambda gui: types.SimpleNamespace(renderer="edgechromium")
    desktop._require_edge_chromium()


# The backup command the window shows --------------------------------------------------------
#
# The owner's /api/me names the database this server opened and the command that backs it up.
# Expected values are written out here; none is built with the quoting the code uses.

BUSINESS = {"name": "Synthetic backup shop", "concept": "A synthetic business used only in this test."}


@contextlib.contextmanager
def window_session(app):
    """An HTTP client signed in the way the window signs in, with the launch code."""
    with httpx.Client() as window:
        assert window.get(app.signin.issue()).status_code == 303
        yield window


def add_business_through_the_window(app, window, key="backup-business"):
    response = window.post(
        app.signin.origin + "/api/businesses",
        json=BUSINESS,
        headers={"Idempotency-Key": key, "Origin": app.signin.origin},
    )
    assert response.status_code == 201
    return response.json()["id"]


def owner_token(data: Path) -> str:
    return (data / "owner.key").read_text().strip()


def test_the_owners_answer_names_this_servers_database_and_other_roles_get_no_field(tmp_path):
    data = desktop.prepare_data_dir(tmp_path / "my data")
    app = DesktopApp(data)
    app.start()
    try:
        expected_path = os.path.realpath(tmp_path) + "/my data/foundry.db"
        with window_session(app) as window:
            mine = window.get(app.signin.origin + "/api/me").json()
            bid = add_business_through_the_window(app, window)
        expected_command = (
            "uv run foundry --db '" + expected_path + "' backup /private/path/backup.db"
            if os.name == "posix"
            else None
        )
        assert mine["desktop"] == {"database": expected_path, "backup_command": expected_command}
        assert expected_path != str(desktop.default_data_dir() / "foundry.db")

        # The rest of the answer is what the ordinary server gives the same owner.
        with TestClient(
            create_app(app.signin.store.path, allowed_hosts=["testserver"]),
            headers={"Authorization": "Bearer " + owner_token(data)},
        ) as ordinary:
            plain = ordinary.get("/api/me").json()
        assert "desktop" not in plain
        assert {k: v for k, v in mine.items() if k != "desktop"} == plain
        assert set(plain) == {"id", "name", "role", "business_ids", "ai_configured"}

        bearer = {"Authorization": "Bearer " + owner_token(data)}
        by_key = httpx.get(app.signin.origin + "/api/me", headers=bearer).json()
        assert by_key["desktop"] == mine["desktop"]

        for role in ("operator", "reviewer", "viewer", "agent"):
            made = httpx.post(
                app.signin.origin + "/api/principals",
                json={"name": f"Synthetic {role}", "role": role, "business_ids": [bid]},
                headers=bearer | {"Origin": app.signin.origin},
            )
            assert made.status_code == 201, made.text
            token = made.json()["token"]
            answer = httpx.get(app.signin.origin + "/api/me", headers={"Authorization": "Bearer " + token})
            assert answer.status_code == 200
            assert answer.json()["role"] == role
            assert "desktop" not in answer.json()
            assert set(answer.json()) == {"id", "name", "role", "business_ids", "ai_configured"}
            with httpx.Client() as person:
                signed_in = person.post(
                    app.signin.origin + "/api/login",
                    json={"token": token},
                    headers={"Origin": app.signin.origin},
                )
                assert signed_in.status_code == 200
                by_session = person.get(app.signin.origin + "/api/me").json()
                assert by_session["role"] == role and "desktop" not in by_session
    finally:
        app.stop()


def test_the_ordinary_server_gives_no_owner_the_desktop_field(tmp_path):
    store = Store(tmp_path / "foundry.db")
    token = store.bootstrap()["token"]
    with TestClient(
        create_app(store.path, allowed_hosts=["testserver"]), headers={"Authorization": "Bearer " + token}
    ) as client:
        answer = client.get("/api/me").json()
        assert answer["role"] == "owner"
        assert "desktop" not in answer
        login = client.post("/api/login", json={"token": token})
        assert login.status_code == 200
        client.headers.pop("Authorization")
        assert "desktop" not in client.get("/api/me").json()


def test_the_ordinary_server_still_does_not_import_the_desktop_module(tmp_path):
    script = (
        "import sys; from foundry.api import create_app; create_app(sys.argv[1]); "
        "print('foundry.desktop' in sys.modules)"
    )
    env = {k: v for k, v in os.environ.items() if k != "FOUNDRY_DB"}
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "foundry.db")], capture_output=True, text=True, env=env
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "False\n"


@pytest.mark.parametrize(
    "path, expected",
    [
        (
            "/Users/founder/Library/Application Support/Business Foundry/foundry.db",
            "uv run foundry --db '/Users/founder/Library/Application Support/Business Foundry/foundry.db' "
            "backup /private/path/backup.db",
        ),
        (
            "/srv/foundry/data/foundry.db",
            "uv run foundry --db /srv/foundry/data/foundry.db backup /private/path/backup.db",
        ),
        (
            "/Users/founder/Founder's Records/foundry.db",
            "uv run foundry --db '/Users/founder/Founder'\"'\"'s Records/foundry.db' backup /private/path/backup.db",
        ),
        (
            '/tmp/$HOME `id`; rm & "x"/foundry.db',
            "uv run foundry --db '/tmp/$HOME `id`; rm & \"x\"/foundry.db' backup /private/path/backup.db",
        ),
        (
            "/Users/zoë/Données/foundry.db",
            "uv run foundry --db '/Users/zoë/Données/foundry.db' backup /private/path/backup.db",
        ),
        ("/tmp/two\nlines/foundry.db", None),
        ("/tmp/tab\there/foundry.db", None),
        ("/tmp/\u202ebd.yrdnuof/foundry.db", None),
    ],
    ids=[
        "spaces",
        "nothing to quote",
        "single quote",
        "shell characters",
        "non-ascii",
        "newline",
        "tab",
        "rlo",
    ],
)
def test_the_backup_command_is_quoted_for_a_posix_shell(path, expected):
    assert desktop.backup_command(path, "posix") == expected


def test_no_backup_command_is_given_off_posix():
    assert (
        desktop.backup_command("C:\\Users\\Founder\\AppData\\Local\\Business Foundry\\foundry.db", "nt")
        is None
    )
    assert desktop.backup_command("/srv/foundry/data/foundry.db", "nt") is None


@pytest.mark.skipif(os.name != "posix", reason="the window gives a ready command only on POSIX shells")
@pytest.mark.parametrize(
    "folder",
    ["Foundry data", "plain", "Founder's data", "$HOME data", "back`tick` data"],
    ids=["space", "plain", "single quote", "dollar", "backtick"],
)
def test_the_shown_command_backs_up_the_windows_database_through_a_real_shell(tmp_path, folder):
    import shlex

    data = desktop.prepare_data_dir(tmp_path / folder)
    app = DesktopApp(data)
    app.start()
    try:
        with window_session(app) as window:
            bid = add_business_through_the_window(app, window)
            command = window.get(app.signin.origin + "/api/me").json()["desktop"]["backup_command"]
        assert command.startswith("uv run foundry --db ") and command.endswith(
            " backup /private/path/backup.db"
        )
        destination = tmp_path / "backups" / "backup.db"
        line = command.replace("/private/path/backup.db", shlex.quote(str(destination)))
        tools = fake_uv(tmp_path / "tools")
        work = tmp_path / "work"
        work.mkdir()
        env = {k: v for k, v in os.environ.items() if k != "FOUNDRY_DB"}
        env["PATH"] = str(tools) + os.pathsep + env["PATH"]
        result = subprocess.run(
            ["/bin/sh", "-c", line], cwd=work, env=env, capture_output=True, text=True, timeout=60
        )
        assert result.returncode == 0, result.stderr
        assert os.listdir(work) == []  # no .foundry/ in the working folder
        expected_db = os.path.realpath(tmp_path) + "/" + folder + "/foundry.db"
        assert result.stdout == (
            f"Saved {os.path.realpath(tmp_path)}/backups/backup.db: a copy of {expected_db} holding 1 business. "
            "This private backup includes credential hashes and business data.\n"
        )
        con = sqlite3.connect(destination)
        try:
            assert [row[0] for row in con.execute("SELECT id FROM businesses")] == [bid]
        finally:
            con.close()
    finally:
        app.stop()


def test_a_backup_restored_into_a_new_folder_opens_in_the_desktop_app(tmp_path, capsys):
    from foundry import cli

    first = desktop.prepare_data_dir(tmp_path / "first")
    with desktop.instance_lock(first):
        app = DesktopApp(first)
        app.start()
        try:
            with window_session(app) as window:
                bid = add_business_through_the_window(app, window)
            backup = tmp_path / "backups" / "backup.db"
            cli.main(["--db", str(first / "foundry.db"), "backup", str(backup)])
            restored = tmp_path / "restored"
            cli.main(["--db", str(restored / "foundry.db"), "restore", str(backup)])
            capsys.readouterr()
            assert sorted(os.listdir(restored)) == ["foundry.db"]  # no owner.key
            # The lock is per data folder: the restored folder opens while the first is still open.
            data = desktop.prepare_data_dir(restored)
            with desktop.instance_lock(data):
                second = DesktopApp(data)
                second.start()
                try:
                    with window_session(second) as window:
                        me = window.get(second.signin.origin + "/api/me").json()
                        businesses = window.get(second.signin.origin + "/api/businesses").json()
                    assert me["role"] == "owner"
                    assert me["desktop"]["database"] == os.path.realpath(tmp_path) + "/restored/foundry.db"
                    assert [b["id"] for b in businesses] == [bid]
                    assert [b["data"]["name"] for b in businesses] == [BUSINESS["name"]]
                    assert not (data / "owner.key").exists()
                finally:
                    second.stop()
        finally:
            app.stop()
