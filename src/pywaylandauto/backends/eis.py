"""Minimal EIS (Emulated Input Server) client — libei 1.5.0, sender context.

Pure stdlib (socket + struct).  Lifecycle: the portal hands us the fd from
ConnectToEIS; the server immediately sends handshake_version; we reply and
drive the handshake to completion, bind the seat capabilities, collect the
device tree (sub-interfaces arrive via ei_device.interface events) and the
keyboard keymap (SCM_RIGHTS fd).

Event frames follow wdotool's pattern (verified in libei 1.5.0 source —
ei_device_frame() is a no-op unless the device is EMULATING):
    start_emulating(last_serial, seq) -> events -> frame(last_serial, ts)
    -> stop_emulating(last_serial)
`last_serial` is the serial of the last event RECEIVED from the server
(client-sent events carry no serials on the wire); `ts` is CLOCK_MONOTONIC
in microseconds.
"""

import logging
import os
import socket
import struct
import time

from . import eis_messages as m

log = logging.getLogger(__name__)

# interface name -> (version we advertise, request table, event table)
_INTERFACES = {
    "ei_connection": (1, m.CONNECTION_REQ, m.CONNECTION_EVT),
    "ei_callback": (1, {}, {"done": (0, "")}),
    "ei_pingpong": (1, m.PINGPONG_REQ, {}),
    "ei_seat": (1, m.SEAT_REQ, m.SEAT_EVT),
    "ei_device": (1, m.DEVICE_REQ, m.DEVICE_EVT),
    "ei_pointer": (1, m.POINTER_REQ, {}),
    "ei_pointer_absolute": (1, m.POINTER_ABS_REQ, {}),
    "ei_scroll": (1, m.SCROLL_REQ, {}),
    "ei_button": (1, m.BUTTON_REQ, {}),
    "ei_keyboard": (1, m.KEYBOARD_REQ, m.KEYBOARD_EVT),
}

_RECV_CHUNK = 65536


class EisError(Exception):
    """EIS connection-level failure."""


class EisDevice:
    def __init__(self, object_id: int):
        self.object_id = object_id
        self.name = ""
        self.device_type = m.DEVICE_TYPE_VIRTUAL
        self.regions: list[tuple[int, int, int, int, float]] = []
        self.sub: dict[str, int] = {}  # interface name -> sub-object id
        self._seq = 0

    def next_sequence(self) -> int:
        self._seq += 1
        return self._seq


class EisClient:
    def __init__(self, fd: int):
        # The fd from the portal is already a connected UNIX socket.
        self._sock = socket.socket(fileno=fd)
        self._sock.setblocking(False)
        self._buf = bytearray()
        self._pending_fds: list = []
        self._objects: dict[int, str] = {}  # object id -> interface name
        self._devices: dict[int, EisDevice] = {}
        self._keyboard: EisDevice | None = None
        self._pointer: EisDevice | None = None
        self._pointer_abs: EisDevice | None = None
        self._connection_id: int | None = None
        self._seat_id: int | None = None
        self._seat_mask: int = 0
        self._bound_mask: int | None = None
        self._last_serial: int = 0
        self._server_version: int | None = None
        self.keymap_text: str | None = None
        self.modifiers: tuple[int, int, int, int, int] | None = None

    # -- lifecycle -------------------------------------------------------

    @property
    def fd(self) -> int:
        return self._sock.fileno()

    def close(self) -> None:
        try:
            if self._connection_id is not None:
                self._send(self._connection_id, *m.CONNECTION_REQ["disconnect"], ())
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    def handshake(self) -> None:
        """Run the sender handshake.  The server sends its
        handshake_version event first; we answer with ours (min = 1)."""
        # 1. wait for the server's version, then reply
        deadline = time.monotonic() + 5.0
        while self._server_version is None:
            self._pump_once()
            if time.monotonic() >= deadline:
                raise EisError("EIS server did not send handshake_version")
        self._send(0, *m.HANDSHAKE_REQ["handshake_version"],
                   (min(self._server_version, 1),))
        # 2. describe ourselves
        self._send(0, *m.HANDSHAKE_REQ["context_type"], (m.CTX_SENDER,))
        self._send(0, *m.HANDSHAKE_REQ["name"], ("pywaylandauto",))
        for name, (version, _req, _evt) in _INTERFACES.items():
            self._send(0, *m.HANDSHAKE_REQ["interface_version"], (name, version))
        self._send(0, *m.HANDSHAKE_REQ["finish"], ())
        # 3. wait for the connection object + seat + devices + keymap
        #    (the keymap is the last piece mutter sends) — with a timeout
        #    so a broken EIS can't wedge the daemon forever
        deadline = time.monotonic() + 5.0
        while (self._connection_id is None or self._bound_mask is None
               or self._keyboard is None or self._pointer_abs is None
               or self.keymap_text is None):
            self._pump_once(deadline - time.monotonic())
            if time.monotonic() >= deadline:
                raise EisError("EIS handshake timed out")

    def pump(self, timeout: float = 0.0) -> None:
        """Drain pending events (non-blocking by default)."""
        self._pump_once(timeout)

    # -- sending ---------------------------------------------------------

    def _send(self, object_id: int, opcode: int, sig: str, args: tuple) -> None:
        try:
            self._sock.sendall(m.pack_message(object_id, opcode, sig, args))
        except OSError as e:
            raise EisError(f"EIS send failed: {e}") from e

    @staticmethod
    def _monotonic_us() -> int:
        return time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1000

    def _begin_frame(self, device: EisDevice) -> None:
        self._send(device.object_id, *m.DEVICE_REQ["start_emulating"],
                   (self._last_serial, device.next_sequence()))

    def _end_frame(self, device: EisDevice) -> None:
        self._send(device.object_id, *m.DEVICE_REQ["frame"],
                   (self._last_serial, self._monotonic_us()))
        self._send(device.object_id, *m.DEVICE_REQ["stop_emulating"],
                   (self._last_serial,))

    # -- input (each call is one framed event batch) ---------------------

    def pointer_motion_relative(self, dx: float, dy: float) -> None:
        self._begin_frame(self._pointer)
        self._send(self._pointer.sub["ei_pointer"],
                   *m.POINTER_REQ["motion_relative"], (dx, dy))
        self._end_frame(self._pointer)

    def pointer_motion_absolute(self, x: float, y: float) -> None:
        # Positions must lie inside one of the device's regions (mutter
        # announces one region covering the whole logical layout).
        if not self._in_regions(self._pointer_abs, x, y):
            raise EisError(f"position ({x}, {y}) outside device regions "
                           f"{self._pointer_abs.regions}")
        self._begin_frame(self._pointer_abs)
        self._send(self._pointer_abs.sub["ei_pointer_absolute"],
                   *m.POINTER_ABS_REQ["motion_absolute"], (x, y))
        self._end_frame(self._pointer_abs)

    def button(self, button: int, state: int) -> None:
        self._begin_frame(self._pointer)
        self._send(self._pointer.sub["ei_button"],
                   *m.BUTTON_REQ["button"], (button, state))
        self._end_frame(self._pointer)

    def scroll_discrete(self, dx: int, dy: int) -> None:
        self._begin_frame(self._pointer)
        self._send(self._pointer.sub["ei_scroll"],
                   *m.SCROLL_REQ["scroll_discrete"], (dx, dy))
        self._end_frame(self._pointer)

    def scroll_smooth(self, dx: float, dy: float) -> None:
        self._begin_frame(self._pointer)
        self._send(self._pointer.sub["ei_scroll"], *m.SCROLL_REQ["scroll"], (dx, dy))
        self._end_frame(self._pointer)

    def keyboard_key(self, keycode: int, state: int) -> None:
        self._begin_frame(self._keyboard)
        self._send(self._keyboard.sub["ei_keyboard"],
                   *m.KEYBOARD_REQ["key"], (keycode, state))
        self._end_frame(self._keyboard)

    @staticmethod
    def _in_regions(device: EisDevice, x: float, y: float) -> bool:
        for rx, ry, rw, rh, _scale in device.regions:
            if rx <= x < rx + rw and ry <= y < ry + rh:
                return True
        return False

    # -- receiving -------------------------------------------------------

    def _pump_once(self, timeout: float = 0.0) -> None:
        try:
            data, ancdata, _flags, _addr = self._sock.recvmsg(
                _RECV_CHUNK, socket.CMSG_SPACE(16 * 4))
        except BlockingIOError:
            return
        except OSError as e:
            raise EisError(f"EIS recv failed: {e}") from e
        if not data:
            raise EisError("EIS server closed the connection")
        if timeout > 0:
            # recvmsg with a nonblocking socket never blocks, but pump()
            # callers pass a timeout for handshake polling — treat it as a
            # retry loop bound instead (callers re-pump on EAGAIN).
            pass
        for level, ctype, cdata in ancdata:
            if level == socket.SOL_SOCKET and ctype == socket.SCM_RIGHTS:
                count = len(cdata) // 4
                self._pending_fds.extend(struct.unpack(f"{count}i", cdata))
        self._buf.extend(data)
        while len(self._buf) >= 16:
            obj_id, length, opcode = m.unpack_header(bytes(self._buf[:16]))
            if len(self._buf) < length:
                break
            payload = bytes(self._buf[16:length])
            del self._buf[:length]
            self._dispatch(obj_id, opcode, payload)

    def _dispatch(self, obj_id: int, opcode: int, payload: bytes) -> None:
        iface = self._objects.get(obj_id, "ei_handshake")
        _, _req, evts = _INTERFACES.get(iface, (1, {}, {}))
        name = None
        sig = None
        for evt_name, (evt_opcode, evt_sig) in evts.items():
            if evt_opcode == opcode:
                name, sig = evt_name, evt_sig
                break
        if iface == "ei_handshake":
            for evt_name, (evt_opcode, evt_sig) in m.HANDSHAKE_EVT.items():
                if evt_opcode == opcode:
                    name, sig = evt_name, evt_sig
                    break
        if name is None:
            # Unknown/unadvertised event (e.g. ei_device v2 additions) —
            # skip it; we never advertised those versions.
            log.debug("ignoring EIS event iface=%s opcode=%d", iface, opcode)
            return
        args, _ = m.unpack_args(sig, payload, 0, self._pending_fds)
        handler = getattr(self, f"_on_{iface.replace('ei_', '')}_{name}", None)
        if handler is not None:
            handler(obj_id, args)

    # -- event handlers --------------------------------------------------

    def _on_handshake_handshake_version(self, obj_id, args):
        self._server_version = args[0]
        log.debug("EIS server speaks version %d", args[0])

    def _on_handshake_connection(self, obj_id, args):
        serial, conn_id, _version = args
        self._connection_id = conn_id
        self._objects[conn_id] = "ei_connection"
        self._last_serial = serial

    def _on_connection_seat(self, obj_id, args):
        seat_id, _version = args
        self._seat_id = seat_id
        self._objects[seat_id] = "ei_seat"

    def _on_seat_capability(self, obj_id, args):
        mask, iface = args
        self._seat_mask |= mask

    def _on_seat_done(self, obj_id, args):
        # Bind everything advertised: we are a full input injector.
        self._bound_mask = self._seat_mask
        self._send(self._seat_id, *m.SEAT_REQ["bind"], (self._seat_mask,))

    def _on_seat_device(self, obj_id, args):
        device_id, _version = args
        self._objects[device_id] = "ei_device"
        self._devices[device_id] = EisDevice(device_id)

    def _on_device_name(self, obj_id, args):
        self._devices[obj_id].name = args[0]

    def _on_device_device_type(self, obj_id, args):
        self._devices[obj_id].device_type = args[0]

    def _on_device_region(self, obj_id, args):
        self._devices[obj_id].regions.append(tuple(args))

    def _on_device_interface(self, obj_id, args):
        sub_id, iface_name, _version = args
        self._objects[sub_id] = iface_name
        device = self._devices[obj_id]
        device.sub[iface_name] = sub_id
        if iface_name == "ei_keyboard":
            self._keyboard = device
        elif iface_name == "ei_pointer":
            self._pointer = device
        elif iface_name == "ei_pointer_absolute":
            self._pointer_abs = device

    def _on_keyboard_keymap(self, obj_id, args):
        keymap_type, size, fd = args
        try:
            # The keymap fd is a memfd: blocking reads complete in full.
            data = b""
            while len(data) < size:
                chunk = os.read(fd, size - len(data))
                if not chunk:
                    break
                data += chunk
        except OSError as e:
            log.warning("EIS keymap read failed: %s", e)
            data = b""
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        if keymap_type == m.KEYMAP_XKB and data:
            self.keymap_text = data.decode("utf-8", errors="replace")
            log.info("EIS keymap: %d bytes of xkb text", len(data))
        else:
            log.warning("EIS keymap type %d unsupported; using fallback", keymap_type)

    def _on_keyboard_modifiers(self, obj_id, args):
        self.modifiers = tuple(args[:5])
        self._last_serial = args[0]

    def _on_connection_ping(self, obj_id, args):
        ping_id, _version = args
        self._objects[ping_id] = "ei_pingpong"
        self._send(ping_id, *m.PINGPONG_REQ["done"], ())

    def _on_connection_disconnected(self, obj_id, args):
        raise EisError(f"EIS disconnected: reason={args[1]} {args[2]!r}")

    def _on_seat_destroyed(self, obj_id, args):
        self._last_serial = args[0]

    def _on_device_destroyed(self, obj_id, args):
        self._last_serial = args[0]
