import re

import pytest

from pywaylandauto.backends.base import (
    CancelledError,
    PermissionDeniedError,
    PortalFailedError,
    PortalUnavailableError,
)
from pywaylandauto.backends.portal import (
    PERSIST_MODE,
    map_dbus_error,
    new_handle_token,
)
from pywaylandauto.token_cache import TokenCache

from .fakes import FakePortalClient, dbus_error


@pytest.mark.parametrize("name,expected", [
    ("org.freedesktop.portal.Error.NotAllowed", PermissionDeniedError),
    ("org.freedesktop.DBus.Error.AccessDenied", PermissionDeniedError),
    ("org.freedesktop.portal.Error.Cancelled", CancelledError),
    ("org.freedesktop.portal.Error.InvalidArgument", PortalFailedError),
    ("org.freedesktop.portal.Error.Exists", PortalFailedError),
    ("org.freedesktop.portal.Error.NotFound", PortalFailedError),
    ("org.freedesktop.portal.Error.Failed", PortalFailedError),
    ("org.freedesktop.portal.Error.WindowDestroyed", PortalFailedError),
    ("org.freedesktop.DBus.Error.ServiceUnknown", PortalUnavailableError),
    ("org.freedesktop.DBus.Error.UnknownMethod", PortalUnavailableError),
    ("org.freedesktop.DBus.Error.UnknownInterface", PortalUnavailableError),
    ("com.example.SomethingElse", PortalFailedError),
])
def test_error_mapping(name, expected):
    assert isinstance(map_dbus_error(dbus_error(name)), expected)


def test_handle_tokens_are_object_path_safe():
    for _ in range(50):
        assert re.fullmatch(r"[A-Za-z0-9_]+", new_handle_token())


def test_create_session_options_shape():
    from pywaylandauto.backends.portal import PortalSession
    fake = FakePortalClient()
    session = PortalSession(portal=fake, token_cache=TokenCache("/nonexistent/token"))
    session.start()
    _method, options = fake.calls[0]
    assert str(options["handle_token"]).startswith("pywaylandauto_")
    assert str(options["session_handle_token"]).startswith("pywaylandauto_")


def test_select_devices_options_shape(tmp_path):
    from pywaylandauto.backends.portal import PortalSession
    fake = FakePortalClient()
    cache = TokenCache(str(tmp_path / "token"))
    cache.save("cached-grant")
    session = PortalSession(portal=fake, token_cache=cache)
    session.start()
    fake.fire(fake.last_create_token, 0, {"session_handle": fake.session_handle})
    _m, _sp, options = fake.calls[1]
    assert int(options["types"]) == 3
    assert int(options["persist_mode"]) == PERSIST_MODE == 2
    assert str(options["restore_token"]) == "cached-grant"


def test_select_devices_options_without_cached_token(tmp_path):
    from pywaylandauto.backends.portal import PortalSession
    fake = FakePortalClient()
    session = PortalSession(portal=fake, token_cache=TokenCache(str(tmp_path / "token")))
    session.start()
    fake.fire(fake.last_create_token, 0, {"session_handle": fake.session_handle})
    _m, _sp, options = fake.calls[1]
    assert "restore_token" not in options


def test_start_passes_empty_parent_window():
    from pywaylandauto.backends.portal import PortalSession
    fake = FakePortalClient()
    session = PortalSession(portal=fake, token_cache=TokenCache("/nonexistent/token"))
    session.start()
    fake.fire(fake.last_create_token, 0, {"session_handle": fake.session_handle})
    fake.fire(fake.last_select_token, 0, {})
    _m, _sp, parent_window, _options = fake.calls[2]
    assert parent_window == ""


def test_start_is_idempotent_while_starting_or_started():
    from pywaylandauto.backends.portal import PortalSession
    fake = FakePortalClient()
    session = PortalSession(portal=fake, token_cache=TokenCache("/nonexistent/token"))
    session.start()
    session.start()  # no-op while starting
    assert len(fake.calls) == 1
    fake.fire(fake.last_create_token, 0, {"session_handle": fake.session_handle})
    fake.fire(fake.last_select_token, 0, {})
    fake.fire(fake.last_start_token, 0, {"restore_token": "t"})
    session.start()  # no-op while started
    assert len(fake.calls) == 4  # create, select, start, probe


@pytest.mark.parametrize("name,code", [
    ("left", 0x110), ("right", 0x111), ("middle", 0x112),
    ("back", 0x113), ("forward", 0x114), ("LEFT", 0x110),
])
def test_button_names(name, code):
    from pywaylandauto.backends.portal import PortalSession
    assert PortalSession._button_code(name) == code
    assert PortalSession._button_code(code) == code


def test_button_unknown_raises_value_error():
    from pywaylandauto.backends.portal import PortalSession
    with pytest.raises(ValueError):
        PortalSession._button_code("sideways")


def test_press_state_validation():
    from pywaylandauto.backends.portal import PortalSession
    assert PortalSession._press_state("press") == 1
    assert PortalSession._press_state("release") == 0
    with pytest.raises(ValueError):
        PortalSession._press_state("squeeze")
