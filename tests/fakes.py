"""Scripted doubles: FakePortalClient stands in for the D-Bus layer,
FakeEisServer plays the EIS end of the ConnectToEIS socket."""

import os
import socket
import struct
import threading

import dbus

from pywaylandauto.backends import eis_messages as m


class FakePortalClient:
    """Records calls and fires response listeners synchronously."""

    def __init__(self):
        self.sender = "1_1"
        self.calls: list = []
        self.listeners: dict = {}
        self.closed_listeners: list = []
        self.session_handle = (
            "/org/freedesktop/portal/desktop/session/1_1/pywaylandauto_fake01"
        )
        self.start_response = (
            0,
            {
                "devices": dbus.Array(
                    [dbus.UInt32(1), dbus.UInt32(2)], signature="u"
                ),
                "restore_token": dbus.String("token-from-start"),
            },
        )
        self.create_error = None
        self.select_error = None
        self.start_error = None
        self.notify_error = None
        self.eis_server: FakeEisServer | None = None
        self.closed_sessions: list = []
        self.last_create_token = None
        self.last_select_token = None
        self.last_start_token = None

    # PortalClient interface --------------------------------------------

    def request_path(self, token: str) -> str:
        return f"/org/freedesktop/portal/desktop/request/{self.sender}/{token}"

    def add_response_listener(self, token: str, callback) -> None:
        self.listeners[token] = callback

    def add_session_closed_listener(self, session_path: str, callback) -> None:
        self.closed_listeners.append(callback)

    def create_session(self, options: dict) -> str:
        self.calls.append(("create_session", options))
        self._raise_once("create_error")
        self.last_create_token = str(options["handle_token"])
        return self.request_path(self.last_create_token)

    def select_devices(self, session_path: str, options: dict) -> None:
        self.calls.append(("select_devices", str(session_path), options))
        self._raise_once("select_error")
        self.last_select_token = str(options["handle_token"])

    def start(self, session_path: str, parent_window: str, options: dict) -> None:
        self.calls.append(("start", str(session_path), str(parent_window), options))
        self._raise_once("start_error")
        self.last_start_token = str(options["handle_token"])

    def notify_pointer_motion(self, session_path: str, dx: float, dy: float) -> None:
        self.calls.append(("notify_pointer_motion", dx, dy))
        self._raise_if_scripted()

    def notify_pointer_motion_absolute(self, session_path: str, x: float, y: float) -> None:
        self.calls.append(("notify_pointer_motion_absolute", x, y))
        self._raise_if_scripted()

    def notify_pointer_button(self, session_path: str, button: int, state: int) -> None:
        self.calls.append(("notify_pointer_button", button, state))
        self._raise_if_scripted()

    def notify_pointer_axis(self, session_path: str, dx: float, dy: float) -> None:
        self.calls.append(("notify_pointer_axis", dx, dy))
        self._raise_if_scripted()

    def notify_pointer_axis_discrete(self, session_path: str, axis: int, steps: int) -> None:
        self.calls.append(("notify_pointer_axis_discrete", axis, steps))
        self._raise_if_scripted()

    def notify_keyboard_keycode(self, session_path: str, keycode: int, state: int) -> None:
        self.calls.append(("notify_keyboard_keycode", keycode, state))
        self._raise_if_scripted()

    def notify_keyboard_keysym(self, session_path: str, keysym: int, state: int) -> None:
        self.calls.append(("notify_keyboard_keysym", keysym, state))
        self._raise_if_scripted()

    def close_session(self, session_path: str) -> None:
        self.closed_sessions.append(str(session_path))

    def connect_to_eis(self, session_path: str) -> int:
        self.calls.append(("connect_to_eis", str(session_path)))
        if self.eis_server is None:
            raise dbus_error("org.freedesktop.portal.Error.Failed",
                             "no fake EIS server")
        # detach(): the fd is handed to EisClient, which owns it from here.
        return self.eis_server.client_sock.detach()

    # scripting helpers --------------------------------------------------

    def _raise_if_scripted(self) -> None:
        self._raise_once("notify_error")

    def _raise_once(self, attr: str) -> None:
        """Scripted errors are one-shot: after raising, they are cleared."""
        error = getattr(self, attr)
        if error is not None:
            setattr(self, attr, None)
            raise error

    def fire(self, token: str, code: int, results: dict) -> None:
        """Deliver a Response signal for the given handle token."""
        self.listeners.pop(token)(code, results)

    def fire_closed(self) -> None:
        for cb in self.closed_listeners:
            cb()

    def notify_calls(self, method: str) -> list:
        return [c for c in self.calls if c[0] == method]


def dbus_error(name: str, message: str = "scripted") -> dbus.exceptions.DBusException:
    # dbus-python quirk: the error NAME must be passed as a keyword, or
    # get_dbus_name() returns None.
    return dbus.exceptions.DBusException(message, name=name)


# request tables the server side needs to recognize (opcode -> name)
_SERVER_TABLES: dict[str, dict] = {
    "ei_handshake": m.HANDSHAKE_REQ,
    "ei_connection": m.CONNECTION_REQ,
    "ei_seat": m.SEAT_REQ,
    "ei_device": m.DEVICE_REQ,
    "ei_pointer": m.POINTER_REQ,
    "ei_pointer_absolute": m.POINTER_ABS_REQ,
    "ei_scroll": m.SCROLL_REQ,
    "ei_button": m.BUTTON_REQ,
    "ei_keyboard": m.KEYBOARD_REQ,
}


class FakeEisServer:
    """Scripted EIS server on a socketpair; records client messages."""

    KEYMAP = b"""xkb_keymap {
xkb_keycodes "evdev" {
    minimum = 8; maximum = 255;
    <AE01> = 10; <AD01> = 38;
};
xkb_types "complete" {
    type "ONE_LEVEL" { modifiers = none; level_name[Level1] = "Any"; };
    type "TWO_LEVEL" { modifiers = Shift; map[Shift] = Level2; };
};
xkb_symbols "pc+us" {
    key <AE01> { [ q, Q ] };
    key <AD01> { [ a, A ] };
};
};
"""

    OBJECT_IDS = {1: "ei_connection", 2: "ei_seat", 3: "ei_device",
                  10: "ei_pointer", 11: "ei_pointer_absolute",
                  12: "ei_scroll", 13: "ei_button", 14: "ei_keyboard"}

    def __init__(self, regions=((0, 0, 4096, 2160, 1.0),)):
        self.client_sock, self.server_sock = socket.socketpair()
        self.regions = regions
        self.received: list = []  # (object_id, iface, name, args)
        self.bind_mask = None
        self.finished = threading.Event()  # set once device.done is sent
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _send(self, object_id, opcode, sig, args, fds=()):
        payload = m.pack_message(object_id, opcode, sig, args)
        if fds:
            self.server_sock.sendmsg(
                [payload],
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                  struct.pack(f"{len(fds)}i", *fds))])
        else:
            self.server_sock.sendall(payload)

    def _serve(self):
        try:
            self._send(0, *m.HANDSHAKE_EVT["handshake_version"], (1,))
            buf = bytearray()
            pending_fds = []
            while True:
                data, ancdata, _flags, _addr = self.server_sock.recvmsg(
                    65536, socket.CMSG_SPACE(16 * 4))
                if not data:
                    break
                for level, ctype, cdata in ancdata:
                    if level == socket.SOL_SOCKET and ctype == socket.SCM_RIGHTS:
                        pending_fds.extend(
                            struct.unpack(f"{len(cdata) // 4}i", cdata))
                buf.extend(data)
                while len(buf) >= 16:
                    obj_id, length, opcode = m.unpack_header(bytes(buf[:16]))
                    if len(buf) < length:
                        break
                    payload = bytes(buf[16:length])
                    del buf[:length]
                    self._handle(obj_id, opcode, payload, pending_fds)
        except OSError:
            pass  # client closed

    def _handle(self, obj_id, opcode, payload, pending_fds):
        iface = self.OBJECT_IDS.get(obj_id, "ei_handshake")
        table = _SERVER_TABLES.get(iface, {})
        name = sig = None
        for req_name, (req_opcode, req_sig) in table.items():
            if req_opcode == opcode:
                name, sig = req_name, req_sig
                break
        if name is None:
            return
        args, _ = m.unpack_args(sig, payload, 0, pending_fds)
        self.received.append((obj_id, iface, name, args))

        if iface == "ei_handshake" and name == "finish":
            self._send(0, *m.HANDSHAKE_EVT["connection"], (1, 1, 1))
            self._send(1, *m.CONNECTION_EVT["seat"], (2, 1))
            self._send(2, *m.SEAT_EVT["name"], ("fake-seat",))
            for mask, ifn in [(0x1, "ei_pointer"), (0x2, "ei_pointer_absolute"),
                              (0x4, "ei_scroll"), (0x8, "ei_button"),
                              (0x10, "ei_keyboard")]:
                self._send(2, *m.SEAT_EVT["capability"], (mask, ifn))
            self._send(2, *m.SEAT_EVT["done"], ())
        elif iface == "ei_seat" and name == "bind":
            self.bind_mask = args[0]
            self._send(2, *m.SEAT_EVT["device"], (3, 1))
            self._send(3, *m.DEVICE_EVT["name"], ("fake virtual device",))
            self._send(3, *m.DEVICE_EVT["device_type"], (m.DEVICE_TYPE_VIRTUAL,))
            for region in self.regions:
                self._send(3, *m.DEVICE_EVT["region"], tuple(region))
            for sub_id, ifn in [(10, "ei_pointer"), (11, "ei_pointer_absolute"),
                                (12, "ei_scroll"), (13, "ei_button"),
                                (14, "ei_keyboard")]:
                self._send(3, *m.DEVICE_EVT["interface"], (sub_id, ifn, 1))
            self._send(3, *m.DEVICE_EVT["done"], ())
            keymap_fd = os.memfd_create("keymap")
            os.write(keymap_fd, self.KEYMAP)
            os.lseek(keymap_fd, 0, os.SEEK_SET)
            self._send(14, *m.KEYBOARD_EVT["keymap"],
                       (m.KEYMAP_XKB, len(self.KEYMAP), -1), fds=[keymap_fd])
            os.close(keymap_fd)
            self.finished.set()

    def frames(self):
        """Group received messages into (start, events..., frame, stop)
        batches; events live on sub-objects but arrive inside the frame."""
        batches = []
        current = None
        for obj_id, iface, name, args in self.received:
            if iface == "ei_device" and name == "start_emulating":
                current = {"start": args, "events": [], "frame": None,
                           "stop": None}
            elif iface == "ei_device" and name == "frame" and current is not None:
                current["frame"] = args
                batches.append(current)
                current = None
            elif iface == "ei_device" and name == "stop_emulating" and batches:
                batches[-1]["stop"] = args
            elif current is not None:
                current["events"].append((iface, name, args))
        return batches

    def wait_frames(self, count: int, timeout: float = 3.0):
        """Poll until the server thread has seen `count` complete frames."""
        import time as _time
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            if len(self.frames()) >= count:
                return self.frames()
            _time.sleep(0.01)
        raise AssertionError(
            f"expected {count} EIS frames, got {len(self.frames())}")

