"""Daemon: GLib main loop + UNIX socket server + request dispatch.

Holds ONE portal session per daemon (the token flow is per-session, so one
session means one authorization dialog ever) and serves input requests over
a newline-delimited JSON socket.  Single-threaded: a request is fully
processed before the next one is read.
"""

import errno
import logging
import os
import socket

import dbus.mainloop.glib
from gi.repository import GLib

try:
    from gi.repository import GLibUnix  # modern PyGObject signal API
except ImportError:  # pragma: no cover
    GLibUnix = None

from . import __version__, protocol
from .backends.base import (
    BackendError,
    CancelledError,
    PermissionDeniedError,
    PermissionPendingError,
    PortalFailedError,
    PortalUnavailableError,
    SessionNotStartedError,
    UnsupportedCharacterError,
)
from .backends.portal import PortalSession
from .monitors import MonitorLayoutUnavailableError, get_monitor_layout
from .token_cache import TokenCache

log = logging.getLogger(__name__)

RECV_CHUNK = 65536


class AlreadyRunningError(Exception):
    """A live daemon already owns the socket."""


def default_socket_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return os.path.join(runtime, "pywaylandauto.sock")
    return f"/tmp/pywaylandauto-{os.getuid()}.sock"


def default_pid_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return os.path.join(runtime, "pywaylandauto.pid")
    return f"/tmp/pywaylandauto-{os.getuid()}.pid"


def error_code_for(exc: Exception) -> str:
    if isinstance(exc, protocol.ProtocolError):
        return exc.code
    if isinstance(exc, PermissionPendingError):
        return protocol.ERR_PERMISSION_PENDING
    if isinstance(exc, PermissionDeniedError):
        return protocol.ERR_PERMISSION_DENIED
    if isinstance(exc, CancelledError):
        return protocol.ERR_CANCELLED
    if isinstance(exc, PortalUnavailableError):
        return protocol.ERR_PORTAL_UNAVAILABLE
    if isinstance(exc, PortalFailedError):
        return protocol.ERR_PORTAL_FAILED
    if isinstance(exc, SessionNotStartedError):
        return protocol.ERR_SESSION_NOT_STARTED
    if isinstance(exc, UnsupportedCharacterError):
        return protocol.ERR_UNSUPPORTED_CHAR
    if isinstance(exc, MonitorLayoutUnavailableError):
        return protocol.ERR_MONITOR_LAYOUT
    if isinstance(exc, BackendError):
        return protocol.ERR_BACKEND
    if isinstance(exc, ValueError):
        return protocol.ERR_INVALID_PARAMS
    return protocol.ERR_INTERNAL


def _param(params: dict, key: str, types, default=None):
    """Validated param lookup — JSON ints arrive as int, not bool."""
    value = params.get(key, default)
    if value is None:
        raise protocol.ProtocolError(
            protocol.ERR_INVALID_PARAMS, f"missing required param {key!r}"
        )
    if not isinstance(value, types) or isinstance(value, bool):
        raise protocol.ProtocolError(
            protocol.ERR_INVALID_PARAMS, f"param {key!r} has invalid type"
        )
    return value


class Daemon:
    def __init__(self, portal: PortalSession | None = None,
                 socket_path: str | None = None,
                 pid_path: str | None = None,
                 auto_start_session: bool = True):
        self.portal = portal if portal is not None else PortalSession()
        self.socket_path = socket_path or default_socket_path()
        self.pid_path = pid_path or default_pid_path()
        self.auto_start_session = auto_start_session
        self._loop: GLib.MainLoop | None = None
        self._server: socket.socket | None = None
        self._buffers: dict[int, bytearray] = {}
        self._eis_watched = False

    # -- lifecycle -------------------------------------------------------

    def run(self) -> int:
        self._server = self._bind()
        self._write_pid()
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self._loop = GLib.MainLoop()
        GLib.io_add_watch(self._server, GLib.PRIORITY_DEFAULT, GLib.IO_IN,
                          self._on_server_ready)
        for signum in (15, 2):  # SIGTERM, SIGINT
            if GLibUnix is not None:
                GLibUnix.signal_add_full(GLib.PRIORITY_DEFAULT, signum,
                                         self._on_signal, None)
            else:  # pragma: no cover
                GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, self._on_signal)
        if self.auto_start_session:
            self.portal.start()
        log.info("daemon listening on %s (pid %d)", self.socket_path, os.getpid())
        try:
            self._loop.run()
        finally:
            self._cleanup()
        return 0

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.quit()

    def _on_signal(self, *args) -> bool:
        log.info("signal received, shutting down")
        self.stop()
        return False

    def _bind(self) -> socket.socket:
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            srv.bind(self.socket_path)
        except OSError as e:
            if e.errno != errno.EADDRINUSE:
                raise
            if self._probe_live():
                raise AlreadyRunningError(
                    f"a live daemon already owns {self.socket_path}"
                ) from e
            os.unlink(self.socket_path)  # stale socket file
            srv.bind(self.socket_path)
        srv.listen(8)
        os.chmod(self.socket_path, 0o600)
        return srv

    def _probe_live(self) -> bool:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.2)
        try:
            probe.connect(self.socket_path)
            return True
        except OSError:
            return False
        finally:
            probe.close()

    def _write_pid(self) -> None:
        try:
            with open(self.pid_path, "w") as f:
                f.write(str(os.getpid()))
            os.chmod(self.pid_path, 0o600)
        except OSError as e:
            log.warning("cannot write pid file %s: %s", self.pid_path, e)

    def _cleanup(self) -> None:
        self.portal.stop()
        for path in (self.socket_path, self.pid_path):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError as e:
                log.warning("cannot unlink %s: %s", path, e)

    # -- socket handling -------------------------------------------------

    def _on_server_ready(self, channel, cond) -> bool:
        try:
            conn, _ = channel.accept()
        except OSError:
            return True
        conn.settimeout(1.0)
        self._buffers[conn.fileno()] = bytearray()
        GLib.io_add_watch(conn, GLib.PRIORITY_DEFAULT,
                          GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR,
                          self._on_client_ready)
        return True

    def _on_client_ready(self, channel, cond) -> bool:
        if cond & (GLib.IO_HUP | GLib.IO_ERR):
            self._drop_client(channel)
            return False
        try:
            data = channel.recv(RECV_CHUNK)
        except (socket.timeout, OSError):
            return True
        if not data:
            self._drop_client(channel)
            return False
        buf = self._buffers[channel.fileno()]
        buf.extend(data)
        while b"\n" in buf:
            line, _, rest = buf.partition(b"\n")
            del buf[: len(line) + 1]
            response = self._handle_line(bytes(line))
            if response is not None:
                try:
                    channel.sendall(response)
                except OSError:
                    self._drop_client(channel)
                    return False
        if len(buf) > protocol.MAX_LINE:
            log.warning("client exceeded max line length; dropping")
            self._drop_client(channel)
            return False
        return True

    def _drop_client(self, channel) -> None:
        self._buffers.pop(channel.fileno(), None)
        try:
            channel.close()
        except OSError:
            pass

    # -- dispatch --------------------------------------------------------

    def _handle_line(self, line: bytes) -> bytes | None:
        try:
            req = protocol.decode_request(line.decode("utf-8"))
        except (protocol.ProtocolError, UnicodeDecodeError) as e:
            code = getattr(e, "code", protocol.ERR_INVALID_JSON)
            return protocol.encode_response(
                0, ok=False, error={"code": code, "message": str(e)}
            )
        quit_after = req["method"] == "daemon.stop"
        try:
            result = self._dispatch(req["method"], req["params"])
        except Exception as e:  # noqa: BLE001
            code = error_code_for(e)
            if code == protocol.ERR_INTERNAL:
                log.exception("internal error handling %r", req["method"])
            else:
                log.info("%s -> %s: %s", req["method"], code, e)
            return protocol.encode_response(
                req["id"], ok=False, error={"code": code, "message": str(e)}
            )
        response = protocol.encode_response(req["id"], ok=True, result=result)
        if quit_after:
            # The response has not been written yet (caller writes it after
            # this returns); defer the quit so the write happens first.
            GLib.timeout_add(50, self._quit_soon)
        return response

    def _quit_soon(self) -> bool:
        self.stop()
        return False

    def _dispatch(self, method: str, params: dict) -> dict:
        if method == "ping":
            return {"version": __version__}
        if method == "status":
            return self._status()
        if method == "session.start":
            self.portal.start()
            return {"state": self.portal.state}
        if method == "daemon.stop":
            return {"stopped": True}
        if method == "input.move_abs":
            x = float(_param(params, "x", (int, float)))
            y = float(_param(params, "y", (int, float)))
            self._ensure_started()
            self.portal.move_abs(x, y)
            return {}
        if method == "input.move_rel":
            dx = float(_param(params, "dx", (int, float)))
            dy = float(_param(params, "dy", (int, float)))
            self._ensure_started()
            self.portal.move_rel(dx, dy)
            return {}
        if method == "input.button":
            button = _param(params, "button", (str, int))
            state = _param(params, "state", (str,))
            self._ensure_started()
            self.portal.button(button, state)
            return {}
        if method == "input.click":
            button = params.get("button", "left")
            self._ensure_started()
            self.portal.click(button)
            return {}
        if method == "input.scroll":
            dx = int(params.get("dx", 0))
            dy = int(params.get("dy", -1))
            discrete = bool(params.get("discrete", True))
            self._ensure_started()
            self.portal.scroll(dx, dy, discrete)
            return {}
        if method == "input.key":
            keysym = _param(params, "keysym", (str, int))
            state = _param(params, "state", (str,))
            self._ensure_started()
            if state == "tap":
                self.portal.tap(keysym)
            else:
                self.portal.key(keysym, state)
            return {}
        if method == "input.type_text":
            text = _param(params, "text", (str,))
            self._ensure_started()
            self.portal.type_text(text)
            return {}
        raise protocol.ProtocolError(
            protocol.ERR_METHOD_NOT_FOUND, f"unknown method {method!r}"
        )

    def _maybe_watch_eis(self) -> None:
        """Once the EIS transport is up, pump its fd on the main loop
        (keymap/modifier/ping events — keymap may arrive after dispatch)."""
        eis = getattr(self.portal, "eis_client", None)
        if eis is None or self._eis_watched:
            return
        GLib.io_add_watch(eis.fd, GLib.PRIORITY_DEFAULT, GLib.IO_IN,
                          self._on_eis_ready)
        self._eis_watched = True

    def _on_eis_ready(self, fd, cond) -> bool:
        eis = getattr(self.portal, "eis_client", None)
        if eis is None:
            return False
        try:
            eis.pump()
        except Exception:  # noqa: BLE001
            log.exception("EIS pump failed")
        return True

    def _ensure_started(self) -> None:
        self._maybe_watch_eis()
        state = self.portal.state
        if state == "started":
            return
        if state == "starting":
            raise PermissionPendingError("dialog pending — poll status")
        if isinstance(self.portal.last_error, PermissionDeniedError):
            raise PermissionDeniedError(
                f"{self.portal.last_error}; run session.start to re-prompt"
            )
        # init, or stopped by an external close: kick off the flow; the
        # dialog (if any) will pop and the client polls status.
        self.portal.start()
        raise PermissionPendingError("session start initiated — grant the dialog")

    def _status(self) -> dict:
        self._maybe_watch_eis()
        layout = None
        try:
            layout = get_monitor_layout()
        except MonitorLayoutUnavailableError:
            pass
        return {
            "session": self.portal.status(),
            "daemon": {"pid": os.getpid(), "socket": self.socket_path},
            "layout": layout,
        }
