"""The desktop shell: data location, loopback binding, launch-code sign-in and shutdown.

These tests run the real local server over real HTTP, without opening a window; pywebview is
not imported.
"""

import os
import socket
import sqlite3
import subprocess
import sys
import textwrap
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
    url = app.start()
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


def test_nothing_is_accepted_before_a_code_is_issued_and_one_is_issued_once(tmp_path):
    store = Store(tmp_path / "foundry.db")
    store.bootstrap()
    signin = LaunchSignIn(store, "testserver")
    assert signin.redeem("") is None and signin.redeem("None") is None
    code = signin.issue().rsplit("=", 1)[1]
    with pytest.raises(RuntimeError):
        signin.issue()
    assert code not in repr(signin)


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
    url = app.start()
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

    def fake_window(app, start_url):
        with httpx.Client() as window:
            seen["start"] = window.get(start_url).status_code
            seen["page"] = window.get(app.signin.origin + "/").status_code
            seen["me"] = window.get(app.signin.origin + "/api/me").json()["role"]
        seen["app"] = app

    monkeypatch.setattr(desktop, "open_window", fake_window)
    monkeypatch.setattr(desktop.signal, "pthread_sigmask", lambda *a: set(), raising=False)
    monkeypatch.setattr(desktop, "configure_logging", lambda data_dir: data_dir / "desktop.log")
    monkeypatch.chdir("/")
    desktop.main(["--data-dir", str(tmp_path / "data"), "-psn_0_12345"])
    assert seen["start"] == 303 and seen["page"] == 200 and seen["me"] == "owner"
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", seen["app"].port), timeout=1)


def test_main_reports_a_second_launch(tmp_path, monkeypatch):
    shown = []
    monkeypatch.setattr(desktop, "show_error", shown.append)
    monkeypatch.setattr(desktop.signal, "pthread_sigmask", lambda *a: set(), raising=False)
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
