"""Client protocol behavior against a plain scripted socket server."""

import socket
import threading

import pytest

from pywaylandauto import protocol
from pywaylandauto.client import Client


class ScriptedServer:
    """Minimal line-protocol server: responds per a script, or on request."""

    def __init__(self, socket_path, handler):
        self.path = socket_path
        self.handler = handler  # (method, params) -> response dict or None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self._thread.start()
        if not self._ready.wait(timeout=2.0):
            raise RuntimeError("scripted server did not start")
        return self

    def __exit__(self, *exc):
        # Daemon thread dies with the process; nothing to clean up.
        pass

    def _serve(self):
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.path)
        srv.listen(4)
        self._ready.set()
        while True:
            conn, _ = srv.accept()
            buf = bytearray()
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                buf.extend(data)
                while b"\n" in buf:
                    line, _, rest = buf.partition(b"\n")
                    del buf[: len(line) + 1]
                    req = protocol.decode_request(line.decode())
                    response = self.handler(req["method"], req["params"])
                    if response is not None:
                        conn.sendall(protocol.encode_response(req["id"], **response))
            conn.close()


def test_request_response_round_trip(tmp_path):
    with ScriptedServer(
        str(tmp_path / "s.sock"),
        lambda method, params: {"ok": True, "result": {"echo": params}},
    ):
        client = Client(socket_path=str(tmp_path / "s.sock"), auto_spawn=False)
        assert client.request("input.move_rel", {"dx": 1, "dy": 2}) == {
            "echo": {"dx": 1, "dy": 2}
        }
        client.close()


def test_remote_error_is_raised(tmp_path):
    with ScriptedServer(
        str(tmp_path / "s.sock"),
        lambda method, params: {"ok": False, "error": {"code": "portal_failed", "message": "boom"}},
    ):
        client = Client(socket_path=str(tmp_path / "s.sock"), auto_spawn=False)
        with pytest.raises(protocol.RemoteError) as exc:
            client.request("ping")
        assert exc.value.code == "portal_failed"
        assert "boom" in exc.value.message
        client.close()


def test_id_matching_skips_stale_responses(tmp_path):
    # Server always answers for id 1 with a wrong (stale) line first.
    state = {"n": 0}
    def handler(method, params):
        return {"ok": True, "result": {"n": state["n"]}}
    with ScriptedServer(str(tmp_path / "s.sock"), handler):
        client = Client(socket_path=str(tmp_path / "s.sock"), auto_spawn=False)
        assert client.request("ping")["n"] == 0
        state["n"] = 1
        assert client.request("ping")["n"] == 1
        client.close()


def test_timeout_when_daemon_silent(tmp_path):
    with ScriptedServer(str(tmp_path / "s.sock"), lambda m, p: None):  # never reply
        client = Client(socket_path=str(tmp_path / "s.sock"), auto_spawn=False,
                        request_timeout=0.3)
        with pytest.raises(TimeoutError):
            client.request("ping")
        client.close()
