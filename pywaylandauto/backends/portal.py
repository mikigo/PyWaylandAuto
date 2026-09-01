"""XDG Desktop Portal RemoteDesktop backend (GNOME 50, notify transport).

Ground truth from Spike 0 on this machine:
  * handle_token / session_handle_token must be valid D-Bus object-path
    elements — [A-Za-z0-9_] only (a '-' crashes older portals, is rejected
    by newer ones).
  * CreateSession ALSO goes through the request/response flow: it returns a
    REQUEST object path; the real session handle arrives in the Response
    signal as results["session_handle"].
  * Request object paths include the caller's unique name:
    /org/freedesktop/portal/desktop/request/{SENDER}/{TOKEN}
    with SENDER = unique name, ':' stripped, '.' -> '_'.
  * The session stays in notify mode as long as ConnectToEIS is never
    called; after ConnectToEIS every Notify* call fails with Error.Failed
    ("Session is not allowed to call Notify* methods").  M1 uses notify
    exclusively; the EIS transport lands in M2.
  * On denial the portal destroys the session object (Session.Close then
    reports "Object does not exist") — close must tolerate that.
  * PropertiesChanged on the RemoteDesktop.Session interface was never
    observed; the state machine trusts Response codes and flow position.
"""

import logging
import secrets

import dbus
import dbus.exceptions
import dbus.mainloop.glib

from .base import (
    AXIS_HORIZONTAL,
    AXIS_VERTICAL,
    BUTTONS,
    PRESS,
    RELEASE,
    Backend,
    BackendError,
    CancelledError,
    MonitorOnlyTranslator,
    PermissionDeniedError,
    PermissionPendingError,
    PortalFailedError,
    PortalUnavailableError,
    SessionNotStartedError,
    UnsupportedCharacterError,
)
from ..keysyms import lookup as lookup_keysym
from ..token_cache import TokenCache
from .eis import EisClient, EisError
from .xkb import build_resolver

log = logging.getLogger(__name__)

PORTAL_DEST = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
RD_IFACE = "org.freedesktop.portal.RemoteDesktop"
SESSION_IFACE = "org.freedesktop.portal.Session"

ERR_NOT_ALLOWED = "org.freedesktop.portal.Error.NotAllowed"
ERR_CANCELLED = "org.freedesktop.portal.Error.Cancelled"
ERR_INVALID_ARG = "org.freedesktop.portal.Error.InvalidArgument"
ERR_EXISTS = "org.freedesktop.portal.Error.Exists"
ERR_NOT_FOUND = "org.freedesktop.portal.Error.NotFound"
ERR_FAILED = "org.freedesktop.portal.Error.Failed"
ERR_WINDOW_DESTROYED = "org.freedesktop.portal.Error.WindowDestroyed"

PERSIST_MODE = 2  # until revoked
# keyboard(1) | pointer(2) only; AvailableDeviceTypes on GNOME 50 is 7
# (1|2|4 — no screen cast bit), so 8 would be rejected.
DEVICE_TYPES = 1 | 2


def new_handle_token() -> str:
    """D-Bus object-path-safe handle token: [A-Za-z0-9_] only."""
    return "pywaylandauto_" + secrets.token_hex(8)


def map_dbus_error(e: dbus.exceptions.DBusException) -> BackendError:
    name = e.get_dbus_name()
    if name in (ERR_NOT_ALLOWED, "org.freedesktop.DBus.Error.AccessDenied"):
        return PermissionDeniedError(str(e))
    if name == ERR_CANCELLED:
        return CancelledError(str(e))
    if name in (ERR_INVALID_ARG, ERR_EXISTS, ERR_NOT_FOUND,
                ERR_WINDOW_DESTROYED, ERR_FAILED):
        return PortalFailedError(str(e))
    if name in ("org.freedesktop.DBus.Error.ServiceUnknown",
                "org.freedesktop.DBus.Error.UnknownMethod",
                "org.freedesktop.DBus.Error.UnknownInterface"):
        return PortalUnavailableError(str(e))
    return PortalFailedError(str(e))


class PortalClient:
    """Thin D-Bus glue — the only place dbus-python is touched.

    PortalSession depends on this narrow interface, so the state machine is
    testable against a fake and the D-Bus layer can be swapped (e.g. to
    gi.Gio.DBus) without touching the flow logic.
    """

    def __init__(self, bus=None):
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self.bus = bus if bus is not None else dbus.SessionBus()
        obj = self.bus.get_object(PORTAL_DEST, PORTAL_PATH)
        self._rd = dbus.Interface(obj, RD_IFACE)
        unique = self.bus.get_unique_name()
        self._sender = unique.lstrip(":").replace(".", "_")

    @property
    def sender(self) -> str:
        return self._sender

    def request_path(self, token: str) -> str:
        return f"/org/freedesktop/portal/desktop/request/{self._sender}/{token}"

    def add_response_listener(self, token: str, callback) -> None:
        """Register the Response receiver BEFORE the call is made (no race).

        With persist_mode set, the request object implements
        PersistentRequest whose Response carries an extra token argument;
        register both interfaces — exactly one fires per signal.
        """
        path = self.request_path(token)
        self.bus.add_signal_receiver(
            lambda response, results: callback(int(response), results),
            signal_name="Response",
            dbus_interface="org.freedesktop.portal.Request",
            path=path,
        )
        self.bus.add_signal_receiver(
            lambda _token, response, results: callback(int(response), results),
            signal_name="Response",
            dbus_interface="org.freedesktop.portal.PersistentRequest",
            path=path,
        )

    def add_session_closed_listener(self, session_path: str, callback) -> None:
        self.bus.add_signal_receiver(
            lambda details: callback(),
            signal_name="Closed",
            dbus_interface=SESSION_IFACE,
            path=session_path,
        )

    def create_session(self, options: dict) -> str:
        return str(self._rd.CreateSession(options))

    def select_devices(self, session_path: str, options: dict) -> None:
        self._rd.SelectDevices(session_path, options)

    def start(self, session_path: str, parent_window: str, options: dict) -> None:
        self._rd.Start(session_path, parent_window, options)

    def notify_pointer_motion(self, session_path: str, dx: float, dy: float) -> None:
        self._rd.NotifyPointerMotion(session_path, {}, dx, dy)

    def notify_pointer_motion_absolute(self, session_path: str, x: float, y: float) -> None:
        self._rd.NotifyPointerMotionAbsolute(session_path, {}, 0, x, y)  # stream 0

    def notify_pointer_button(self, session_path: str, button: int, state: int) -> None:
        self._rd.NotifyPointerButton(session_path, {}, button, state)

    def notify_pointer_axis(self, session_path: str, dx: float, dy: float) -> None:
        self._rd.NotifyPointerAxis(session_path, {}, dx, dy)

    def notify_pointer_axis_discrete(self, session_path: str, axis: int, steps: int) -> None:
        self._rd.NotifyPointerAxisDiscrete(session_path, {}, axis, steps)

    def notify_keyboard_keycode(self, session_path: str, keycode: int, state: int) -> None:
        self._rd.NotifyKeyboardKeycode(session_path, {}, keycode, state)

    def notify_keyboard_keysym(self, session_path: str, keysym: int, state: int) -> None:
        self._rd.NotifyKeyboardKeysym(session_path, {}, keysym, state)

    def connect_to_eis(self, session_path: str) -> int:
        """ConnectToEIS returns an fd; take() transfers ownership to us."""
        return self._rd.ConnectToEIS(session_path, {}).take()

    def close_session(self, session_path: str) -> None:
        try:
            obj = self.bus.get_object(PORTAL_DEST, session_path)
            dbus.Interface(obj, SESSION_IFACE).Close()
        except dbus.exceptions.DBusException as e:
            # The portal destroys the session object on denial; closing a
            # gone session is not an error.
            log.debug("Session.Close on %s: %s", session_path, e)


class PortalSession(Backend):
    """RemoteDesktop portal session: state machine + notify transport."""

    name = "portal"

    def __init__(self, portal: PortalClient | None = None,
                 token_cache: TokenCache | None = None,
                 transport: str | None = None):
        self.portal = portal if portal is not None else PortalClient()
        self.token_cache = token_cache if token_cache is not None else TokenCache()
        self.transport_override = transport
        self._state = "init"
        self._last_error: BackendError | None = None
        self.session_path: str | None = None
        self.devices: list = []
        self.transport: str | None = None
        self.eis_client: EisClient | None = None
        self._resolver = None
        self._has_token = self.token_cache.load() is not None
        self._retry_without_token = False

    # -- state -----------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def last_error(self) -> BackendError | None:
        return self._last_error

    def start(self) -> None:
        if self._state in ("starting", "started"):
            return
        self._state = "starting"
        self._last_error = None
        self.session_path = None
        self.devices = []
        self.transport = None
        self.eis_client = None
        self._resolver = None
        # Re-read the cache at every start: a CI-injected token file or a
        # revocation between attempts must be honored.
        self._has_token = self.token_cache.load() is not None
        self._retry_without_token = False
        token = new_handle_token()
        self.portal.add_response_listener(token, self._on_create_session)
        options = {
            "handle_token": dbus.String(token),
            "session_handle_token": dbus.String(new_handle_token()),
        }
        try:
            self.portal.create_session(options)
        except dbus.exceptions.DBusException as e:
            self._fail(map_dbus_error(e))

    def stop(self) -> None:
        if self.eis_client is not None:
            self.eis_client.close()
            self.eis_client = None
        if self.session_path is not None:
            self.portal.close_session(self.session_path)
        self._state = "stopped"
        self.session_path = None
        self.devices = []
        # The token was already persisted on Start; it stays cached.

    def status(self) -> dict:
        return {
            "state": self._state,
            "devices": list(self.devices),
            "transport": self.transport,
            "has_token": self._has_token,
            "persist_mode": PERSIST_MODE,
            "error": type(self._last_error).__name__ if self._last_error else None,
        }

    # -- flow callbacks (invoked from the main loop) ---------------------

    def _on_create_session(self, code: int, results: dict) -> None:
        try:
            if code != 0:
                self._fail(PortalFailedError(f"CreateSession failed with code {code}"))
                return
            self.session_path = str(results["session_handle"])
            self.portal.add_session_closed_listener(self.session_path, self._on_closed)
            self._select_devices(use_token=self._has_token)
        except Exception as e:  # noqa: BLE001 — callbacks must never raise
            log.error("CreateSession callback failed: %s", e)
            self._fail(PortalFailedError(str(e)))

    def _select_devices(self, use_token: bool) -> None:
        token = new_handle_token()
        self.portal.add_response_listener(token, self._on_select_devices)
        options = {
            "types": dbus.UInt32(DEVICE_TYPES),
            "persist_mode": dbus.UInt32(PERSIST_MODE),
            "handle_token": dbus.String(token),
        }
        if use_token:
            cached = self.token_cache.load()
            if cached:
                options["restore_token"] = dbus.String(cached)
        try:
            self.portal.select_devices(self.session_path, options)
        except dbus.exceptions.DBusException as e:
            if (e.get_dbus_name() == ERR_NOT_ALLOWED and use_token
                    and not self._retry_without_token):
                log.warning("Cached token rejected — revoking and retrying once")
                self.token_cache.revoke()
                self._has_token = False
                self._retry_without_token = True
                self._select_devices(use_token=False)
                return
            self._fail(map_dbus_error(e))

    def _on_select_devices(self, code: int, results: dict) -> None:
        try:
            if code != 0:
                self._fail(PortalFailedError(f"SelectDevices failed with code {code}"))
                return
            token = new_handle_token()
            self.portal.add_response_listener(token, self._on_start)
            try:
                self.portal.start(self.session_path, "",  # no X11 parent
                                  {"handle_token": dbus.String(token)})
            except dbus.exceptions.DBusException as e:
                self._fail(map_dbus_error(e))
        except Exception as e:  # noqa: BLE001
            log.error("SelectDevices callback failed: %s", e)
            self._fail(PortalFailedError(str(e)))

    def _on_start(self, code: int, results: dict) -> None:
        try:
            if code == 0:
                if "restore_token" in results:
                    self.token_cache.save(str(results["restore_token"]))
                    self._has_token = True
                devices = results.get("devices", [])
                # GNOME 50 returns a single uint32 here, not an array of
                # uint32 as the spec says; normalize both.
                if isinstance(devices, (int, dbus.UInt32)):
                    devices = [devices]
                self.devices = [str(d) for d in devices]
                self._state = "started"
                log.info("Session %s started; devices=%s", self.session_path, self.devices)
                self._choose_transport()
            elif code == 1:
                self._fail(PermissionDeniedError("user denied the interaction dialog"))
            else:
                self._fail(PortalFailedError(f"Start failed with code {code}"))
        except Exception as e:  # noqa: BLE001
            log.error("Start callback failed: %s", e)
            self._fail(PortalFailedError(str(e)))

    def _choose_transport(self) -> None:
        """Default to EIS (the only path with working absolute motion on
        GNOME — notify's absolute motion needs a screen cast stream the
        RemoteDesktop portal can't provide).  notify is an explicit opt-out
        via PYWAYLANDAUTO_TRANSPORT, and the fallback if ConnectToEIS fails.
        """
        if self.transport_override == "notify":
            self._probe_notify()
            return
        try:
            fd = self.portal.connect_to_eis(self.session_path)
        except dbus.exceptions.DBusException as e:
            log.warning("ConnectToEIS failed (%s) — falling back to notify", e)
            self._probe_notify()
            return
        try:
            self.eis_client = EisClient(fd)
            self.eis_client.handshake()
        except EisError as e:
            # The session is EIS-mode now; notify is dead for its lifetime.
            raise PortalFailedError(f"EIS handshake failed: {e}") from e
        self.transport = "eis"
        log.info("Transport: eis (keymap=%s)",
                 "parsed" if self.eis_client.keymap_text else "fallback")

    def _probe_notify(self) -> None:
        # A zero-motion probe: success means the portal accepts Notify*.
        try:
            self.portal.notify_pointer_motion(self.session_path, 0.0, 0.0)
            self.transport = "notify"
            log.info("Transport: notify")
        except dbus.exceptions.DBusException as e:
            log.error("Notify probe failed: %s", e)
            self._fail(PortalFailedError(f"portal rejected Notify* calls: {e}"))

    def _on_closed(self) -> None:
        log.info("Session %s closed externally", self.session_path)
        if self._state != "stopped":
            self._state = "stopped"
            self.session_path = None
            self.devices = []
            if self._last_error is None:
                self._last_error = SessionNotStartedError("session closed by the portal")

    def _fail(self, error: BackendError) -> None:
        self._state = "stopped"
        self._last_error = error
        self.session_path = None
        self.devices = []
        log.error("Session failed: %s", error)

    # -- input (raise typed errors unless started) -----------------------

    def _require_started(self) -> None:
        if self._state == "started":
            return
        if self._state == "starting":
            raise PermissionPendingError("session start in progress — grant the dialog")
        if isinstance(self._last_error, PermissionDeniedError):
            raise self._last_error
        raise SessionNotStartedError("no portal session — call session.start first")

    def _session(self) -> str:
        self._require_started()
        assert self.session_path is not None
        return self.session_path

    def move_abs(self, x: float, y: float,
                 translator: MonitorOnlyTranslator | None = None) -> None:
        if translator is not None:
            x, y = translator.translate(x, y)
        self._require_started()
        if self.transport == "eis":
            try:
                self.eis_client.pointer_motion_absolute(float(x), float(y))
            except EisError as e:
                raise PortalFailedError(str(e)) from e
            return
        raise BackendError(
            "absolute pointer motion needs the EIS transport on GNOME "
            "(mutter validates Notify* positions against a screen cast "
            "stream the RemoteDesktop portal cannot provide)"
        )

    def move_rel(self, dx: float, dy: float) -> None:
        self._require_started()
        if self.transport == "eis":
            try:
                self.eis_client.pointer_motion_relative(float(dx), float(dy))
            except EisError as e:
                raise PortalFailedError(str(e)) from e
            return
        try:
            self.portal.notify_pointer_motion(self._session(), float(dx), float(dy))
        except dbus.exceptions.DBusException as e:
            raise map_dbus_error(e) from e

    @staticmethod
    def _button_code(button: str | int) -> int:
        if isinstance(button, int):
            return button
        code = BUTTONS.get(str(button).lower())
        if code is None:
            raise ValueError(f"unknown button {button!r}")
        return code

    @staticmethod
    def _press_state(state: str) -> int:
        if state == "press":
            return PRESS
        if state == "release":
            return RELEASE
        raise ValueError(f"state must be 'press' or 'release', got {state!r}")

    def button(self, button: str | int, state: str) -> None:
        code = self._button_code(button)
        press = self._press_state(state)
        self._require_started()
        if self.transport == "eis":
            try:
                self.eis_client.button(code, press)
            except EisError as e:
                raise PortalFailedError(str(e)) from e
            return
        try:
            self.portal.notify_pointer_button(self._session(), code, press)
        except dbus.exceptions.DBusException as e:
            raise map_dbus_error(e) from e

    def scroll(self, dx: int = 0, dy: int = -1, discrete: bool = True) -> None:
        self._require_started()
        if self.transport == "eis":
            try:
                if discrete:
                    self.eis_client.scroll_discrete(int(dx), int(dy))
                else:
                    self.eis_client.scroll_smooth(float(dx), float(dy))
            except EisError as e:
                raise PortalFailedError(str(e)) from e
            return
        try:
            if discrete:
                if dy:
                    self.portal.notify_pointer_axis_discrete(
                        self._session(), AXIS_VERTICAL, int(dy))
                if dx:
                    self.portal.notify_pointer_axis_discrete(
                        self._session(), AXIS_HORIZONTAL, int(dx))
            else:
                self.portal.notify_pointer_axis(self._session(), float(dx), float(dy))
        except dbus.exceptions.DBusException as e:
            raise map_dbus_error(e) from e

    # -- EIS keysym -> keycode translation -------------------------------

    def _resolve_key(self, ks: int) -> tuple[int, int]:
        if self._resolver is None:
            self._resolver = build_resolver(
                self.eis_client.keymap_text if self.eis_client else None)
        resolved = self._resolver(ks)
        if resolved is None:
            raise UnsupportedCharacterError(
                f"keysym 0x{ks:04x} is not in the keymap")
        return resolved

    def _key_eis(self, ks: int, press: int) -> None:
        """One EIS key event; level-1 keysyms get a managed Shift around
        the key (mutter #3375: EIS clients can't trust modifier state)."""
        keycode, level = self._resolve_key(ks)
        if level == 0:
            self.eis_client.keyboard_key(keycode, press)
            return
        if level == 1:
            shift_keycode, _ = self._resolve_key(0xFFE1)  # Shift_L
            if press:
                self.eis_client.keyboard_key(shift_keycode, PRESS)
                self.eis_client.keyboard_key(keycode, PRESS)
            else:
                self.eis_client.keyboard_key(keycode, RELEASE)
                self.eis_client.keyboard_key(shift_keycode, RELEASE)
            return
        raise UnsupportedCharacterError(
            f"keysym 0x{ks:04x} needs modifier level {level} (unsupported)")

    def key(self, keysym: str | int, state: str) -> None:
        ks = lookup_keysym(keysym)
        if ks is None:
            raise ValueError(f"unknown keysym {keysym!r}")
        if state == "tap":
            self.key(ks, "press")
            self.key(ks, "release")
            return
        press = self._press_state(state)
        self._require_started()
        if self.transport == "eis":
            try:
                self._key_eis(ks, press)
            except EisError as e:
                raise PortalFailedError(str(e)) from e
            return
        try:
            self.portal.notify_keyboard_keysym(self._session(), ks, press)
        except dbus.exceptions.DBusException as e:
            raise map_dbus_error(e) from e

    def type_text(self, text: str) -> None:
        self._require_started()
        if self.transport == "eis":
            self._type_text_eis(text)
            return
        for ch in text:
            codepoint = ord(ch)
            if codepoint > 0xFF:
                raise UnsupportedCharacterError(
                    f"character {ch!r} (U+{codepoint:04X}) is beyond Latin-1")
            try:
                self.portal.notify_keyboard_keysym(self._session(), codepoint, PRESS)
                self.portal.notify_keyboard_keysym(self._session(), codepoint, RELEASE)
            except dbus.exceptions.DBusException as e:
                raise map_dbus_error(e) from e

    def _type_text_eis(self, text: str) -> None:
        try:
            for ch in text:
                codepoint = ord(ch)
                if codepoint > 0xFF:
                    raise UnsupportedCharacterError(
                        f"character {ch!r} (U+{codepoint:04X}) is beyond Latin-1")
                self._key_eis(codepoint, PRESS)
                self._key_eis(codepoint, RELEASE)
        except EisError as e:
            raise PortalFailedError(str(e)) from e
