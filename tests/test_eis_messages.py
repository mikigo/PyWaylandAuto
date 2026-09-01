import struct

import pytest

from pywaylandauto.backends import eis_messages as m


def roundtrip(object_id, opcode, sig, args):
    data = m.pack_message(object_id, opcode, sig, args)
    obj_id, length, op = m.unpack_header(data)
    assert (obj_id, op) == (object_id, opcode)
    assert length == len(data)
    unpacked, offset = m.unpack_args(sig, data, 16, [])
    assert offset == len(data)
    return unpacked


@pytest.mark.parametrize("sig,args", [
    ("u", (7,)),
    ("uu", (7, 9)),
    ("ff", (1.5, -2.25)),
    ("ii", (-3, 4)),
    ("ut", (1, 2 ** 40 + 5)),
    ("su", ("ei_pointer", 1)),
    ("unu", (1, 2, 1)),
    ("uuuuf", (0, 0, 4096, 2160, 1.0)),
    ("", ()),
])
def test_roundtrip(sig, args):
    assert roundtrip(42, 3, sig, args) == list(args)


def test_string_padding():
    # 1-char string: len 2 (+NUL) padded to 4 -> payload 4+4=8
    data = m.pack_message(0, 3, "s", ("x",))
    assert len(data) == 16 + 8
    # 3-char string: len 4 -> no extra padding
    data = m.pack_message(0, 3, "s", ("xyz",))
    assert len(data) == 16 + 8


def test_header_layout():
    data = m.pack_message(0x0102030405060708, 0x09, "u", (1,))
    assert data[:8] == struct.pack("=Q", 0x0102030405060708)
    assert struct.unpack("=I", data[8:12])[0] == 20  # 16 header + 4 arg
    assert struct.unpack("=I", data[12:16])[0] == 0x09


def test_fd_arg_has_zero_payload_bytes():
    data = m.pack_message(1, 1, "uuh", (1, 100, -1))
    assert len(data) == 16 + 8  # two u32s, fd carries no payload


def test_unpack_short_header_raises():
    with pytest.raises(ValueError):
        m.unpack_header(b"\x00" * 8)


def test_unknown_signature_char_raises():
    with pytest.raises(ValueError):
        m.pack_message(0, 0, "z", (1,))


def test_opcode_tables_declaration_order():
    # opcodes are declaration order in protocol.xml @ 1.5.0
    assert m.HANDSHAKE_REQ["handshake_version"] == (0, "u")
    assert m.HANDSHAKE_REQ["finish"] == (1, "")
    assert m.HANDSHAKE_REQ["context_type"] == (2, "u")
    assert m.HANDSHAKE_REQ["name"] == (3, "s")
    assert m.HANDSHAKE_REQ["interface_version"] == (4, "su")
    assert m.HANDSHAKE_EVT["connection"] == (2, "unu")
    assert m.DEVICE_REQ["start_emulating"] == (1, "uu")
    assert m.DEVICE_REQ["stop_emulating"] == (2, "u")
    assert m.DEVICE_REQ["frame"] == (3, "ut")
    assert m.KEYBOARD_EVT["keymap"] == (1, "uuh")


def test_sender_context_value():
    assert m.CTX_SENDER == 2
