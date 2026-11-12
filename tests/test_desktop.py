"""The desktop shell: data location, loopback binding, launch-code sign-in and shutdown.

These tests run the real local server over real HTTP, without opening a window. pywebview is
imported by one test only, which is skipped when the desktop extra is not installed.
"""

import contextlib
import importlib.util
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
from urllib.parse import urlsplit

import httpx
import pytest
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
    # itself once the window's time is up, and exits as soon as it is done.
    code, out, data, _ = run_until_signalled(tmp_path, mode, close_seconds=0.5)
    assert code == 128 + signal.SIGTERM and "MAIN RETURNED" not in out
    assert assert_cleaned_up(data) == 1
    assert "did not stop within 0.5 seconds of the signal; stopping now" in read_log(data)


@posix_signals
@pytest.mark.parametrize("mode", ["stop-hangs", "gui-hangs"])
def test_sigterm_ends_the_process_within_the_limit_whatever_shutdown_is_doing(tmp_path, mode):
    # stop-hangs: a stop already in progress is stuck inside the cleanup, holding the stop lock.
    # gui-hangs: the cleanup finished, but the GUI loop never returns, so main() never does.
    # Either way the process is gone once SIGNAL_EXIT_SECONDS have passed since the signal.
    # (In gui-hangs the window may take all the time: whatever it does, the exit is at the limit.)
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
