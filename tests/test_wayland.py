import logging
import os
import socket
import struct

import pytest

from pywaylandauto.backends import wayland as w


def test_header_roundtrip():
    data = w.pack_message(7, 3, "u", (42,))
    assert w.unpack_header(data) == (7, 12, 3)  # obj, length incl. header, opcode


def test_pack_unpack_all_sig_types():
    msg = w.pack_message(1, 0, "uifs",
                         (5, -2, w.to_fixed(1.5), "héllo"))  # f = wl_fixed 24.8
    obj_id, size, opcode = w.unpack_header(msg)
    args, offset = w.unpack_args("uifs", msg, 8, [])
    assert args == [5, -2, 384, "héllo"]  # 1.5 * 256 = 384
    assert offset == size  # consumed exactly the payload


def test_fixed_conversions():
    assert w.to_fixed(1.5) == 384
    assert w.from_fixed(384) == 1.5


def test_o_n_h_args():
    msg = w.pack_message(1, 1, "onh", (2, 3, -1))  # h: zero payload bytes
    args, offset = w.unpack_args("onh", msg, 8, [9])
    assert args == [2, 3, 9]
    assert offset == 8 + 8  # only o and n occupy bytes


def test_string_padded_to_4():
    msg = w.pack_message(1, 0, "s", ("abc",))
    assert len(msg) == 8 + 4 + 4  # header + u32 len + "abc\0" (4 bytes, 4-aligned)
    args, _ = w.unpack_args("s", msg, 8, [])
    assert args == ["abc"]


def test_fd_passes_via_scm_rights():
    a, b = socket.socketpair()
    rfd, wfd = os.pipe()  # a real fd: the kernel rejects SCM_RIGHTS with a
    payload = w.pack_message(1, 0, "h", (-1,))  # non-existent fd (EBADF)
    a.sendmsg([payload], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                           struct.pack("i", rfd))])
    data, ancdata, _f, _addr = b.recvmsg(1024, socket.CMSG_SPACE(4))
    fds = []
    for level, ctype, cdata in ancdata:
        if level == socket.SOL_SOCKET and ctype == socket.SCM_RIGHTS:
            fds.extend(struct.unpack(f"{len(cdata) // 4}i", cdata))
    assert len(fds) == 1 and fds[0] != -1
    # the fd number is reallocated on recv, so compare the underlying file
    assert os.fstat(fds[0]).st_ino == os.fstat(rfd).st_ino  # same pipe
    assert data == payload  # fd arg occupies zero payload bytes
    os.close(rfd); os.close(wfd); os.close(fds[0])
    a.close(); b.close()


def test_sync_roundtrip_against_socketpair():
    """Connection.sync() resolves when wl_callback.done arrives."""
    from pywaylandauto.backends.wayland import CALLBACK_EVT, WaylandConnection

    a, b = socket.socketpair()

    def serve():
        buf = bytearray()
        while True:
            data = b.recv(65536)
            if not data:
                break
            buf.extend(data)
            while len(buf) >= 8:
                obj_id, size, opcode = w.unpack_header(bytes(buf[:8]))
                if len(buf) < size:
                    break
                payload = bytes(buf[8:size])
                del buf[:size]
                if opcode == 0:  # wl_display.sync: arg[0] = callback id
                    (cb_id,) = struct.unpack("=I", payload)
                    b.sendall(w.pack_message(cb_id, *CALLBACK_EVT["done"], (0,)))

    import threading
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    conn = WaylandConnection(a.detach())
    conn.sync()
    assert conn.display_id == 1
    a.close(); b.close()


def test_seat_opcode_order_matches_wayland_xml(caplog):
    """wayland.xml wl_seat declares get_pointer (0), get_keyboard (1),
    get_touch (2), release (3)."""
    assert w.SEAT_REQ["get_pointer"][0] == 0
    assert w.SEAT_REQ["get_keyboard"][0] == 1
    assert w.SEAT_REQ["get_touch"][0] == 2
    assert w.SEAT_REQ["release"][0] == 3
    # unknown events are skipped (never raised) and logged at debug
    a, b = socket.socketpair()
    conn = w.WaylandConnection(a.detach())
    b.sendall(w.pack_message(99, 42, "u", (1,)))  # unregistered object, unknown opcode
    with caplog.at_level(logging.DEBUG, logger="pywaylandauto.backends.wayland"):
        conn.pump()
    assert any("ignoring Wayland event" in m for m in caplog.messages)
    a.close(); b.close()
