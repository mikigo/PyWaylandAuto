import pytest

from pywaylandauto.keysyms import lookup


@pytest.mark.parametrize("name,expected", [
    ("return", 0xFF0D), ("Return", 0xFF0D), ("RETURN", 0xFF0D), ("enter", 0xFF0D),
    ("tab", 0xFF09), ("escape", 0xFF1B), ("esc", 0xFF1B),
    ("backspace", 0xFF08), ("delete", 0xFFFF), ("del", 0xFFFF),
    ("left", 0xFF51), ("up", 0xFF52), ("right", 0xFF53), ("down", 0xFF54),
    ("home", 0xFF50), ("end", 0xFF57), ("page_up", 0xFF55), ("pgdn", 0xFF56),
    ("f1", 0xFFBE), ("f5", 0xFFC2), ("f12", 0xFFC9),
    ("ctrl", 0xFFE3), ("control", 0xFFE3), ("control_l", 0xFFE3),
    ("shift", 0xFFE1), ("alt", 0xFFE9), ("super", 0xFFEB), ("win", 0xFFEB),
    ("caps_lock", 0xFFE5), ("space", 0x20), ("minus", 0x2D),
])
def test_named_keysyms(name, expected):
    assert lookup(name) == expected


@pytest.mark.parametrize("name,expected", [
    ("a", 0x61), ("A", 0x41), ("0", 0x30), (".", 0x2E),
])
def test_single_char_ascii(name, expected):
    assert lookup(name) == expected


def test_single_char_latin1():
    assert lookup("é") == 0xE9


def test_single_char_unicode_keysym():
    # beyond Latin-1: X11 Unicode keysym 0x01000000 | codepoint
    assert lookup("你") == 0x01000000 + ord("你")


def test_hex_form():
    assert lookup("0xff0d") == 0xFF0D
    assert lookup("0x110") == 0x110


def test_int_passthrough():
    assert lookup(0xFF0D) == 0xFF0D


@pytest.mark.parametrize("name", ["nosuchkey", "", "xy", "0x", None, 3.14])
def test_unknown_returns_none(name):
    assert lookup(name) is None
