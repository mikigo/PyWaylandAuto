import pytest

from pywaylandauto.backends.base import (
    BackendError,
    PermissionDeniedError,
    PermissionPendingError,
    PortalFailedError,
    SessionNotStartedError,
    UnsupportedCharacterError,
)
from pywaylandauto.backends.portal import PortalSession
from pywaylandauto.token_cache import TokenCache

from .fakes import FakePortalClient, dbus_error


@pytest.fixture
def fake():
    return FakePortalClient()


@pytest.fixture
def session(fake, tmp_path):
    return PortalSession(portal=fake, token_cache=TokenCache(str(tmp_path / "token")))


def drive_to_started(session, fake):
    """Run the async flow to completion against the fake."""
    session.start()
    fake.fire(fake.last_create_token, 0, {"session_handle": fake.session_handle})
    fake.fire(fake.last_select_token, 0, {})
    fake.fire(fake.last_start_token, *fake.start_response)


def test_happy_path_reaches_started(session, fake):
    assert session.state == "init"
    drive_to_started(session, fake)
    assert session.state == "started"
    assert session.transport == "notify"
    assert session.status()["has_token"] is True
    assert session.status()["persist_mode"] == 2
    # zero-motion transport probe was sent
    assert fake.notify_calls("notify_pointer_motion") == [("notify_pointer_motion", 0.0, 0.0)]


def test_token_saved_from_start_response(session, fake, tmp_path):
    drive_to_started(session, fake)
    assert session.token_cache.load() == "token-from-start"


def test_session_handle_token_and_tokens_are_object_path_safe(session, fake):
    import re
    drive_to_started(session, fake)
    for token in (fake.last_create_token, fake.last_select_token, fake.last_start_token):
        assert re.fullmatch(r"[A-Za-z0-9_]+", token)
    create_opts = fake.calls[0][1]
    assert re.fullmatch(r"[A-Za-z0-9_]+", str(create_opts["session_handle_token"]))


def test_denial_marks_stopped_with_permission_denied(session, fake):
    fake.start_response = (1, {})
    session.start()
    fake.fire(fake.last_create_token, 0, {"session_handle": fake.session_handle})
    fake.fire(fake.last_select_token, 0, {})
    fake.fire(fake.last_start_token, 1, {})
    assert session.state == "stopped"
    assert isinstance(session.last_error, PermissionDeniedError)


def test_notallowed_select_with_token_revokes_and_retries_once(session, fake, tmp_path):
    session.token_cache.save("stale-token")
    fake.select_error = dbus_error("org.freedesktop.portal.Error.NotAllowed", "stale")

    session.start()
    fake.fire(fake.last_create_token, 0, {"session_handle": fake.session_handle})
    # first select_devices raised NotAllowed -> revoke + retry without token
    assert session.token_cache.load() is None
    fake.fire(fake.last_select_token, 0, {})  # second (tokenless) attempt
    fake.fire(fake.last_start_token, *fake.start_response)
    assert session.state == "started"

    select_calls = [c for c in fake.calls if c[0] == "select_devices"]
    assert len(select_calls) == 2
    assert "restore_token" in select_calls[0][2]
    assert "restore_token" not in select_calls[1][2]


def test_notallowed_without_token_fails(session, fake):
    fake.select_error = dbus_error("org.freedesktop.portal.Error.NotAllowed", "denied")
    session.start()
    fake.fire(fake.last_create_token, 0, {"session_handle": fake.session_handle})
    assert session.state == "stopped"
    assert isinstance(session.last_error, PermissionDeniedError)


def test_create_session_service_unknown_means_unavailable(session, fake):
    fake.create_error = dbus_error("org.freedesktop.DBus.Error.ServiceUnknown")
    session.start()
    assert session.state == "stopped"
    assert type(session.last_error).__name__ == "PortalUnavailableError"


def test_notify_probe_failure_fails_session(session, fake):
    fake.notify_error = dbus_error("org.freedesktop.portal.Error.Failed", "EIS mode")
    drive_to_started(session, fake)
    assert session.state == "stopped"
    assert isinstance(session.last_error, PortalFailedError)


def test_input_before_start_raises(session, fake):
    with pytest.raises(SessionNotStartedError):
        session.click()
    with pytest.raises(SessionNotStartedError):
        session.move_abs(10, 10)


def test_input_while_starting_raises_pending(session, fake):
    session.start()
    with pytest.raises(PermissionPendingError):
        session.click()


def test_input_after_denial_raises_permission_denied(session, fake):
    fake.start_response = (1, {})
    session.start()
    fake.fire(fake.last_create_token, 0, {"session_handle": fake.session_handle})
    fake.fire(fake.last_select_token, 0, {})
    fake.fire(fake.last_start_token, 1, {})
    with pytest.raises(PermissionDeniedError):
        session.click()


def test_devices_scalar_from_gnome_50_is_normalized(session, fake):
    # Real GNOME 50 returns "devices" as a single uint32, not an array.
    import dbus
    fake.start_response = (0, {"devices": dbus.UInt32(3),
                               "restore_token": dbus.String("t")})
    drive_to_started(session, fake)
    assert session.state == "started"
    assert session.devices == ["3"]


def test_external_close_stops_session(session, fake):
    drive_to_started(session, fake)
    fake.fire_closed()
    assert session.state == "stopped"
    assert isinstance(session.last_error, SessionNotStartedError)


def test_stop_closes_portal_session(session, fake):
    drive_to_started(session, fake)
    session.stop()
    assert session.state == "stopped"
    assert fake.closed_sessions == [fake.session_handle]


def test_click_sends_press_and_release(session, fake):
    drive_to_started(session, fake)
    session.click("right")
    buttons = fake.notify_calls("notify_pointer_button")
    assert buttons == [
        ("notify_pointer_button", 0x111, 1),
        ("notify_pointer_button", 0x111, 0),
    ]


def test_move_abs_on_notify_transport_raises_clear_error(session, fake):
    drive_to_started(session, fake)
    assert session.transport == "notify"
    with pytest.raises(BackendError, match="EIS"):
        session.move_abs(5, 6)


def test_scroll_discrete_vs_smooth(session, fake):
    drive_to_started(session, fake)
    session.scroll(dy=3)
    assert fake.notify_calls("notify_pointer_axis_discrete") == [
        ("notify_pointer_axis_discrete", 0, 3)
    ]
    session.scroll(dx=-1, dy=2, discrete=False)
    assert fake.notify_calls("notify_pointer_axis") == [("notify_pointer_axis", -1.0, 2.0)]


def test_key_tap_and_chord_decomposition(session, fake):
    drive_to_started(session, fake)
    session.key("Return", "press")
    session.key("Return", "release")
    keys = fake.notify_calls("notify_keyboard_keysym")
    assert keys == [
        ("notify_keyboard_keysym", 0xFF0D, 1),
        ("notify_keyboard_keysym", 0xFF0D, 0),
    ]


def test_type_text_sends_press_release_pairs(session, fake):
    drive_to_started(session, fake)
    session.type_text("Hi!")
    keys = fake.notify_calls("notify_keyboard_keysym")
    assert keys == [
        ("notify_keyboard_keysym", ord("H"), 1), ("notify_keyboard_keysym", ord("H"), 0),
        ("notify_keyboard_keysym", ord("i"), 1), ("notify_keyboard_keysym", ord("i"), 0),
        ("notify_keyboard_keysym", ord("!"), 1), ("notify_keyboard_keysym", ord("!"), 0),
    ]


def test_type_text_rejects_beyond_latin1(session, fake):
    drive_to_started(session, fake)
    with pytest.raises(UnsupportedCharacterError):
        session.type_text("你好")
