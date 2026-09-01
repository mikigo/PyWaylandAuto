"""PortalSession with the EIS transport end to end (fake portal + fake EIS)."""

import pytest

from pywaylandauto.backends.portal import PortalSession
from pywaylandauto.token_cache import TokenCache

from .fakes import FakeEisServer, FakePortalClient


@pytest.fixture
def ctx(tmp_path):
    fake = FakePortalClient()
    eis_server = FakeEisServer()
    eis_server.start()
    fake.eis_server = eis_server
    session = PortalSession(portal=fake, token_cache=TokenCache(str(tmp_path / "token")))
    return session, fake, eis_server


def drive_to_started(session, fake):
    session.start()
    fake.fire(fake.last_create_token, 0, {"session_handle": fake.session_handle})
    fake.fire(fake.last_select_token, 0, {})
    fake.fire(fake.last_start_token, *fake.start_response)


def test_default_transport_is_eis(ctx):
    session, fake, eis = ctx
    drive_to_started(session, fake)
    assert session.state == "started"
    assert session.transport == "eis"
    assert eis.finished.wait(3.0), "fake EIS handshake did not complete"


def test_move_abs_goes_over_eis(ctx):
    session, fake, eis = ctx
    drive_to_started(session, fake)
    session.move_abs(500, 300)
    frames = eis.wait_frames(1)
    assert frames[-1]["events"] == [
        ("ei_pointer_absolute", "motion_absolute", [500.0, 300.0])
    ]


def test_move_rel_click_scroll_over_eis(ctx):
    session, fake, eis = ctx
    drive_to_started(session, fake)
    session.move_rel(10, 10)
    session.click()  # left
    session.scroll(dy=-2)
    frames = eis.wait_frames(4)
    events = [f["events"][0] for f in frames]
    assert ("ei_pointer", "motion_relative", [10.0, 10.0]) in events
    assert ("ei_button", "button", [0x110, 1]) in events
    assert ("ei_button", "button", [0x110, 0]) in events
    assert ("ei_scroll", "scroll_discrete", [0, -2]) in events


def test_type_text_with_shift_over_eis(ctx):
    session, fake, eis = ctx
    drive_to_started(session, fake)
    session.type_text("Hi")
    frames = eis.wait_frames(6)
    events = [f["events"][0] for f in frames]
    # H -> shift down, h down, h up, shift up (fallback US: H=(35,1))
    assert events == [
        ("ei_keyboard", "key", [42, 1]),    # Shift_L down
        ("ei_keyboard", "key", [35, 1]),    # h down
        ("ei_keyboard", "key", [35, 0]),    # h up
        ("ei_keyboard", "key", [42, 0]),    # Shift_L up
        ("ei_keyboard", "key", [23, 1]),    # i down
        ("ei_keyboard", "key", [23, 0]),    # i up
    ]


def test_key_uses_keymap_when_available(ctx):
    session, fake, eis = ctx
    drive_to_started(session, fake)
    session.key("q", "tap")
    frames = eis.wait_frames(2)
    events = [f["events"][0] for f in frames]
    assert events == [
        ("ei_keyboard", "key", [10, 1]),  # keymap: <AE01> = 10
        ("ei_keyboard", "key", [10, 0]),
    ]


def test_stop_closes_eis(ctx):
    session, fake, eis = ctx
    drive_to_started(session, fake)
    session.stop()
    assert session.eis_client is None
    assert fake.closed_sessions == [fake.session_handle]


def test_notify_override_skips_eis(ctx, monkeypatch):
    session, fake, eis = ctx
    session.transport_override = "notify"
    drive_to_started(session, fake)
    assert session.transport == "notify"
    assert session.eis_client is None
