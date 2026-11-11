"""The desktop shell: data location, loopback binding, launch-code sign-in and shutdown.

These tests run the real local server over real HTTP, without opening a window; pywebview is
not imported.
"""

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
    with httpx.Client() as guesser:
        for wrong in ["", "x", url.rsplit("=", 1)[1][:-1] + "A", "é" * 43]:
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


# The window's sign-in controller, against the real server -----------------------------------


@pytest.fixture
def quick(tmp_path, monkeypatch):
    """A running app whose launch codes last a fraction of a second."""
    monkeypatch.setattr(desktop, "LAUNCH_GRACE_SECONDS", 0.2)
    app = DesktopApp(desktop.prepare_data_dir(tmp_path / "data"), launch_seconds=0.4)
    app.start()
    try:
        yield app
    finally:
        app.stop()


class FakeWindow:
    """Stands in for the pywebview window: navigate() loads a URL the way the window would."""

    def __init__(self, app, late=()):
        self.app, self.late = app, set(late)
        self.client = httpx.Client()
        self.visits, self.gave_up = [], 0

    def navigate(self, url):
        attempt = len(self.visits) + 1
        if attempt in self.late:
            time.sleep(self.app.launch_seconds + 0.1)  # a slow cold start
        response = self.client.get(url)
        self.visits.append((url, response.status_code))
        if response.status_code == 303:
            assert self.client.get(self.app.signin.origin + "/").status_code == 200

    def give_up(self):
        self.gave_up += 1


def test_a_window_that_arrives_late_is_sent_a_fresh_code(quick, caplog):
    window = FakeWindow(quick, late={1})
    with caplog.at_level("INFO", logger="foundry.desktop"):
        assert desktop.sign_in_window(quick.signin, window.navigate, window.give_up) is True
    [(stale, first), (fresh, second)] = window.visits
    assert (first, second) == (403, 303) and stale != fresh
    assert window.gave_up == 0
    assert window.client.get(quick.signin.origin + "/api/me").json()["role"] == "owner"
    with httpx.Client() as other:
        assert other.get(stale).status_code == 403
        assert other.get(fresh).status_code == 403
    assert "Refused a launch code presented after it expired." in caplog.text
    assert "The window did not sign in (attempt 1): its code was refused." in caplog.text
    for url, _ in window.visits:
        assert url.rsplit("=", 1)[1] not in caplog.text


def test_a_code_the_window_never_used_is_replaced_when_its_time_is_up(quick):
    dropped = []
    window = FakeWindow(quick)

    def navigate(url):
        if not dropped:
            dropped.append(url)  # the window never loaded it
            return
        window.navigate(url)

    assert desktop.sign_in_window(quick.signin, navigate, window.give_up) is True
    assert [status for _, status in window.visits] == [303]
    with httpx.Client() as other:
        assert other.get(dropped[0]).status_code == 403


def test_the_window_is_told_plainly_after_the_last_attempt(quick):
    window = FakeWindow(quick, late={1, 2, 3})
    assert desktop.sign_in_window(quick.signin, window.navigate, window.give_up) is False
    assert [status for _, status in window.visits] == [403] * desktop.LAUNCH_ATTEMPTS
    assert window.gave_up == 1
    assert window.client.get(quick.signin.origin + "/api/me").status_code == 401


def test_closing_the_app_stops_the_controller_without_a_failure_page(quick):
    calls, result = [], []
    controller = threading.Thread(
        target=lambda: result.append(
            desktop.sign_in_window(quick.signin, calls.append, lambda: calls.append("gave up"))
        )
    )
    controller.start()
    while not calls:
        time.sleep(0.01)
    quick.stop()
    controller.join(5)
    assert result == [False] and "gave up" not in calls and len(calls) == 1


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
# was handled, that killed the process with no cleanup.
SIGNAL_CHILD = textwrap.dedent("""\
    import signal, sys, threading, time
    from pathlib import Path
    import httpx
    from foundry import desktop

    threading.Thread(target=time.sleep, args=(600,), daemon=True).start()  # foreign, unmasked
    mode, data = sys.argv[1], Path(sys.argv[2])
    desktop.SIGNAL_EXIT_SECONDS = 1.5

    def fake_window(app, signals):
        closed = threading.Event()

        def destroy():
            if mode == "stuck":
                return  # a wedged GUI that ignores the request
            app.stop()  # what the window's closing event does
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
        real_start = desktop.DesktopApp.start

        def start_then_signal(self):
            signin = real_start(self)
            signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
            print("READY", flush=True)
            time.sleep(1.0)  # the signal arrives while the server is starting
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
            return signin

        desktop.DesktopApp.start = start_then_signal
        desktop.open_window = lambda app, signals: print("WINDOW OPENED", flush=True)
    else:
        desktop.open_window = fake_window
    desktop.main(["--data-dir", str(data)])
    print("MAIN RETURNED", flush=True)
""")


def run_until_signalled(tmp_path, mode):
    data = tmp_path / "data"
    child = subprocess.Popen(
        [sys.executable, "-c", SIGNAL_CHILD, mode, str(data)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert child.stdout.readline().strip() == "READY"
        child.send_signal(signal.SIGTERM)
        out, _ = child.communicate(timeout=30)
    finally:
        child.kill()
    return child.returncode, out, data


def assert_cleaned_up(data):
    log = (data / "desktop.log").read_text()
    assert "Received signal 15" in log and "Stopped cleanly." in log
    wal = data / "foundry.db-wal"
    assert not wal.exists() or wal.stat().st_size == 0  # checkpointed (or removed on last close)
    main_file = sqlite3.connect((data / "foundry.db").as_uri() + "?immutable=1", uri=True)
    try:
        assert main_file.execute("SELECT COUNT(*) FROM sessions").fetchone() == (0,)
        return main_file.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]
    finally:
        main_file.close()


@pytest.mark.skipif(not hasattr(signal, "pthread_sigmask"), reason="POSIX signals")
def test_sigterm_delivered_to_a_foreign_thread_still_shuts_down_cleanly(tmp_path):
    code, out, data = run_until_signalled(tmp_path, "normal")
    assert code == 0 and "MAIN RETURNED" in out
    assert assert_cleaned_up(data) == 1


@pytest.mark.skipif(not hasattr(signal, "pthread_sigmask"), reason="POSIX signals")
def test_sigterm_still_cleans_up_and_exits_when_the_window_will_not_close(tmp_path):
    code, out, data = run_until_signalled(tmp_path, "stuck")
    assert code == 128 + signal.SIGTERM and "MAIN RETURNED" not in out
    assert assert_cleaned_up(data) == 1
    assert "stopping now" in (data / "desktop.log").read_text()


@pytest.mark.skipif(not hasattr(signal, "pthread_sigmask"), reason="POSIX signals")
def test_sigterm_before_the_window_opens_skips_the_window(tmp_path):
    code, out, data = run_until_signalled(tmp_path, "early")
    assert code == 0 and "WINDOW OPENED" not in out and "MAIN RETURNED" in out
    assert assert_cleaned_up(data) == 0
    assert "Stopping before the window opened." in (data / "desktop.log").read_text()


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
