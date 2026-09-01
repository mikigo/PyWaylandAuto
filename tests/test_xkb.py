import pytest

from pywaylandauto.backends.xkb import (
    Keymap,
    build_resolver,
    parse_keymap_text,
    us_fallback,
)

SAMPLE = """xkb_keymap {
xkb_keycodes "evdev+aliases(qwerty)" {
    minimum = 8; maximum = 255;
    <ESC> = 9;
    <AE01> = 10;
    <AD01> = 38;
    <TLDE> = 49;
    alias <HZTG> = <TLDE>;
};
xkb_types "complete" {
    type "TWO_LEVEL" { modifiers = Shift; map[Shift] = Level2; };
};
xkb_symbols "pc+us" {
    key <AE01> { [ q, Q ] };
    key <AD01> { [ a, A ] };
    key <HZTG> { [ grave, asciitilde, U+4E00 ] };
    key <ESC> { [ Escape ] };
};
};
"""


def test_parse_and_resolve_levels():
    keymap = parse_keymap_text(SAMPLE)
    assert keymap.resolve(ord("q")) == (10, 0)
    assert keymap.resolve(ord("Q")) == (10, 1)
    assert keymap.resolve(ord("a")) == (38, 0)
    assert keymap.resolve(ord("A")) == (38, 1)


def test_parse_alias_and_unicode():
    keymap = parse_keymap_text(SAMPLE)
    # alias <HZTG> = <TLDE> -> keycode 49
    assert keymap.resolve(0x60) == (49, 0)          # grave
    assert keymap.resolve(0x7E) == (49, 1)          # asciitilde
    assert keymap.resolve(0x01000000 + 0x4E00) == (49, 2)  # U+4E00


def test_parse_control_names():
    keymap = parse_keymap_text(SAMPLE)
    assert keymap.resolve(0xFF1B) == (9, 0)  # Escape


def test_garbage_text_yields_empty_keymap():
    assert not parse_keymap_text("not a keymap at all")
    assert not parse_keymap_text("")


def test_us_fallback_basic():
    fb = us_fallback()
    assert fb.resolve(ord("a")) == (30, 0)
    assert fb.resolve(ord("A")) == (30, 1)
    assert fb.resolve(ord("z")) == (44, 0)
    assert fb.resolve(ord("1")) == (2, 0)
    assert fb.resolve(ord("!")) == (2, 1)
    assert fb.resolve(0xFF0D) == (28, 0)   # Return
    assert fb.resolve(0xFFE3) == (29, 0)   # Control_L
    assert fb.resolve(0xFFE1) == (42, 0)   # Shift_L
    assert fb.resolve(ord(" ")) == (57, 0)


def test_build_resolver_chains_partial_keymap_to_fallback():
    resolve = build_resolver(SAMPLE)
    # in the keymap: exact hit
    assert resolve(ord("q")) == (10, 0)
    # missing from the keymap: falls back to US table
    assert resolve(ord("h")) == (35, 0)
    assert resolve(ord("H")) == (35, 1)
    assert resolve(ord("?")) == (53, 1)
    # truly unknown (0xFFFF is Delete — avoid real keysyms here)
    assert resolve(0x12345678) is None


def test_build_resolver_without_keymap_uses_fallback():
    resolve = build_resolver(None)
    assert resolve(ord("a")) == (30, 0)
    resolve = build_resolver("garbage")
    assert resolve(ord("a")) == (30, 0)


def test_keymap_bool():
    assert not Keymap()
    km = Keymap()
    km.add_key(10, [ord("q")])
    assert km
