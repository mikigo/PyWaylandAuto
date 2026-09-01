import time

import pytest

from pywaylandauto.backends.eis import EisClient, EisError
from pywaylandauto.backends.eis_messages import BUTTON_REQ, KEYBOARD_REQ

from .fakes import FakeEisServer


@pytest.fixture
def eis():
    server = FakeEisServer()
    server.start()
    client = EisClient(server.client_sock.detach())
    client.handshake()
    yield client, server
    client.close()


def test_handshake_state(eis):
    client, server = eis
    assert client._connection_id == 1
    assert client._seat_id == 2
    assert client._bound_mask == 0x1F  # all five capabilities
    assert server.bind_mask == 0x1F
    # device tree
    assert client._pointer is not None
    assert client._pointer_abs is not None
    assert client._keyboard is not None
    assert client._pointer.sub["ei_pointer"] == 10
    assert client._pointer_abs.sub["ei_pointer_absolute"] == 11
    assert client._keyboard.sub["ei_keyboard"] == 14
    # region announced
    assert client._pointer_abs.regions == [(0, 0, 4096, 2160, 1.0)]


def test_keymap_received_and_parsed(eis):
    client, _server = eis
    assert client.keymap_text is not None
    assert "xkb_symbols" in client.keymap_text


def test_absolute_motion_is_framed(eis):
    client, server = eis
    client.pointer_motion_absolute(100.0, 200.0)
    frames = server.wait_frames(1)
    assert len(frames) == 1
    frame = frames[0]
    assert frame["start"][0] == 1  # serial
    assert frame["events"] == [("ei_pointer_absolute", "motion_absolute", [100.0, 200.0])]
    assert frame["frame"] is not None
    assert frame["stop"] == [1]


def test_absolute_motion_outside_regions_raises(eis):
    client, _server = eis
    with pytest.raises(EisError):
        client.pointer_motion_absolute(99999.0, 99999.0)


def test_click_press_release_in_separate_frames(eis):
    client, server = eis
    client.button(0x110, 1)  # press
    client.button(0x110, 0)  # release
    frames = server.wait_frames(2)
    assert len(frames) == 2
    assert frames[0]["events"] == [("ei_button", "button", [0x110, 1])]
    assert frames[1]["events"] == [("ei_button", "button", [0x110, 0])]


def test_relative_motion_and_scroll(eis):
    client, server = eis
    client.pointer_motion_relative(-3.5, 7.5)
    client.scroll_discrete(0, -3)
    frames = server.wait_frames(2)
    assert frames[0]["events"] == [("ei_pointer", "motion_relative", [-3.5, 7.5])]
    assert frames[1]["events"] == [("ei_scroll", "scroll_discrete", [0, -3])]


def test_keyboard_key_uses_keycode(eis):
    client, server = eis
    client.keyboard_key(38, 1)
    frames = server.wait_frames(1)
    assert frames[0]["events"] == [("ei_keyboard", "key", [38, 1])]


def test_handshake_fails_when_server_dead():
    import socket as s
    a, b = s.socketpair()
    b.close()  # no server side at all
    client = EisClient(a.detach())
    with pytest.raises(EisError):
        client.handshake()
    client.close()
