"""Client library: UNIX socket + JSON protocol, with daemon auto-spawn."""

import os
import socket
import subprocess
import sys
import time

from . import protocol
from .daemon import default_socket_path

DEFAULT_CONNECT_TIMEOUT = 2.0
DEFAULT_REQUEST_TIMEOUT = 30.0


class Client:
    def __init__(self, socket_path: str | None = None,
                 auto_spawn: bool = True,
                 connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
                 request_timeout: float = DEFAULT_REQUEST_TIMEOUT):
        self.socket_path = socket_path
        self.auto_spawn = auto_spawn
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self._sock: socket.socket | None = None
        self._buf = bytearray()
        self._req_id = 0

    def connect(self) -> None:
        if self._sock is not None:
            return
        path = self.socket_path or default_socket_path()
        try:
            self._sock = self._try_connect(path)
        except (ConnectionRefusedError, FileNotFoundError) as e:
            if not self.auto_spawn:
                raise ConnectionError(f"daemon not reachable at {path}: {e}") from e
            self._spawn_daemon()
            deadline = time.monotonic() + self.connect_timeout
            while True:
                try:
                    self._sock = self._try_connect(path)
                    break
                except (ConnectionRefusedError, FileNotFoundError):
                    if time.monotonic() >= deadline:
                        raise ConnectionError(
                            f"daemon did not come up at {path} within "
                            f"{self.connect_timeout}s"
                        ) from e
                    time.sleep(0.1)

    @staticmethod
    def _try_connect(path: str) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(path)
        return sock

    @staticmethod
    def _spawn_daemon() -> None:
        subprocess.Popen(
            [sys.executable, "-m", "pywaylandauto", "daemon", "start"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._buf = bytearray()

    def request(self, method: str, params: dict | None = None) -> dict:
        """Send one request and return its result; raises RemoteError."""
        self.connect()
        self._req_id += 1
        self._sock.sendall(protocol.encode_request(self._req_id, method, params))
        deadline = time.monotonic() + self.request_timeout
        while True:
            line = self._read_line(deadline)
            resp = protocol.decode_response(line)
            if resp["id"] != self._req_id:
                continue  # late/stale response; not expected in M1
            if resp["ok"]:
                return resp["result"]
            raise protocol.RemoteError(resp["code"], resp["message"])

    def _read_line(self, deadline: float) -> str:
        while b"\n" not in self._buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"daemon did not respond within {self.request_timeout}s"
                )
            self._sock.settimeout(remaining)
            try:
                data = self._sock.recv(65536)
            except socket.timeout:
                raise TimeoutError(
                    f"daemon did not respond within {self.request_timeout}s"
                ) from None
            if not data:
                raise ConnectionError("daemon closed the connection")
            self._buf.extend(data)
        line, _, rest = self._buf.partition(b"\n")
        del self._buf[: len(line) + 1]
        return line.decode("utf-8")

    # -- convenience wrappers --------------------------------------------

    def ping(self) -> dict:
        return self.request("ping")

    def status(self) -> dict:
        return self.request("status")

    def session_start(self) -> dict:
        return self.request("session.start")

    def move_abs(self, x: float, y: float) -> dict:
        return self.request("input.move_abs", {"x": x, "y": y})

    def move_rel(self, dx: float, dy: float) -> dict:
        return self.request("input.move_rel", {"dx": dx, "dy": dy})

    def button(self, button, state: str) -> dict:
        return self.request("input.button", {"button": button, "state": state})

    def click(self, button="left") -> dict:
        return self.request("input.click", {"button": button})

    def scroll(self, dx: int = 0, dy: int = -1, discrete: bool = True) -> dict:
        return self.request("input.scroll", {"dx": dx, "dy": dy, "discrete": discrete})

    def key(self, keysym, state: str) -> dict:
        return self.request("input.key", {"keysym": keysym, "state": state})

    def tap(self, keysym) -> dict:
        return self.key(keysym, "tap")

    def type_text(self, text: str) -> dict:
        return self.request("input.type_text", {"text": text})

    def daemon_stop(self) -> dict:
        return self.request("daemon.stop")
