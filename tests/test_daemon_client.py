import os
import threading
import time

import pytest

from pywaylandauto import protocol
from pywaylandauto.backends.portal import PortalSession
from pywaylandauto.client import Client
from pywaylandauto.daemon import Daemon
from pywaylandauto.token_cache import TokenCache

from .fakes import FakePortalClient


@pytest.fixture
def ctx(tmp_path):
    """Daemon with a fake portal running in a background thread."""
    fake = FakePortalClient()
    portal = PortalSession(portal=fake,
                           token_cache=TokenCache(str(tmp_path / "token")))
    socket_path = str(tmp_path / "test.sock")
    daemon = Daemon(portal=portal, socket_path=socket_path,
                    pid_path=str(tmp_path / "test.pid"),
                    auto_start_session=False)
    thread = threading.Thread(target=daemon.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not os.path.exists(socket_path):
        time.sleep(0.02)
    assert os.path.exists(socket_path), "daemon socket did not appear"
    yield {"fake": fake, "portal": portal, "daemon": daemon,
           "thread": thread, "socket": socket_path}
    daemon.stop()
    thread.join(timeout=3.0)
    assert not os.path.exists(socket_path), "socket should be cleaned up"


def make_client(ctx, **kw):
    kw.setdefault("auto_spawn", False)
    return Client(socket_path=ctx["socket"], **kw)


def test_ping(ctx):
    client = make_client(ctx)
    assert client.ping()["version"]
    client.close()


def test_status_shape(ctx):
    client = make_client(ctx)
    result = client.status()
    assert result["session"]["state"] == "init"
    assert result["daemon"]["pid"] == os.getpid() or isinstance(result["daemon"]["pid"], int)
    client.close()


def test_input_before_session_yields_permission_pending_then_works(ctx):
    client = make_client(ctx)
    with pytest.raises(protocol.RemoteError) as exc:
        client.click()
    assert exc.value.code == protocol.ERR_PERMISSION_PENDING

    # Grant the dialog on the fake side.
    fake = ctx["fake"]
    fake.fire(fake.last_create_token, 0, {"session_handle": fake.session_handle})
    fake.fire(fake.last_select_token, 0, {})
    fake.fire(fake.last_start_token, *fake.start_response)

    assert client.click() == {}
    buttons = fake.notify_calls("notify_pointer_button")
    assert buttons == [("notify_pointer_button", 0x110, 1),
                       ("notify_pointer_button", 0x110, 0)]
    client.close()


def test_denied_dialog_yields_permission_denied(ctx):
    client = make_client(ctx)
    with pytest.raises(protocol.RemoteError) as exc:
        client.click()
    assert exc.value.code == protocol.ERR_PERMISSION_PENDING

    fake = ctx["fake"]
    fake.fire(fake.last_create_token, 0, {"session_handle": fake.session_handle})
    fake.fire(fake.last_select_token, 0, {})
    fake.fire(fake.last_start_token, 1, {})  # user denied

    with pytest.raises(protocol.RemoteError) as exc:
        client.click()
    assert exc.value.code == protocol.ERR_PERMISSION_DENIED
    client.close()


def test_unknown_method(ctx):
    client = make_client(ctx)
    with pytest.raises(protocol.RemoteError) as exc:
        client.request("no.such.method")
    assert exc.value.code == protocol.ERR_METHOD_NOT_FOUND
    client.close()


def test_invalid_params(ctx):
    client = make_client(ctx)
    with pytest.raises(protocol.RemoteError) as exc:
        client.request("input.move_abs", {"x": "banana", "y": 1})
    assert exc.value.code == protocol.ERR_INVALID_PARAMS
    client.close()


def test_bad_json_gets_invalid_json(ctx):
    import socket
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(ctx["socket"])
    sock.sendall(b"{not json}\n")
    response = sock.recv(4096).decode()
    resp = protocol.decode_response(response)
    assert resp["ok"] is False
    assert resp["code"] == protocol.ERR_INVALID_JSON
    sock.close()


def test_daemon_stop_responds_then_quits(ctx):
    client = make_client(ctx)
    assert client.daemon_stop() == {"stopped": True}
    client.close()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and os.path.exists(ctx["socket"]):
        time.sleep(0.02)
    assert not os.path.exists(ctx["socket"])


def test_client_without_daemon_and_no_spawn_raises(tmp_path):
    client = Client(socket_path=str(tmp_path / "missing.sock"), auto_spawn=False)
    with pytest.raises(ConnectionError):
        client.request("ping")
    client.close()


def test_key_chord_decomposition(ctx):
    client = make_client(ctx)
    fake = ctx["fake"]
    # grant
    with pytest.raises(protocol.RemoteError):
        client.key("ctrl", "press")
    fake.fire(fake.last_create_token, 0, {"session_handle": fake.session_handle})
    fake.fire(fake.last_select_token, 0, {})
    fake.fire(fake.last_start_token, *fake.start_response)

    client.key("ctrl", "press")
    client.tap("t")
    client.key("ctrl", "release")
    keys = fake.notify_calls("notify_keyboard_keysym")
    assert keys == [
        ("notify_keyboard_keysym", 0xFFE3, 1),
        ("notify_keyboard_keysym", ord("t"), 1),
        ("notify_keyboard_keysym", ord("t"), 0),
        ("notify_keyboard_keysym", 0xFFE3, 0),
    ]
    client.close()
