"""Backend abstraction for input injection.

M1 ships the portal backend only, but the seam is designed for the M2/M3
backends (wlroots protocols, uinput) behind the same interface.
"""

from abc import ABC, abstractmethod


class BackendError(Exception):
    """Base class for backend failures."""


class SessionNotStartedError(BackendError):
    """Input requested while no session is started."""


class PermissionPendingError(BackendError):
    """Session flow is underway; the user has not granted the dialog yet."""


class PermissionDeniedError(BackendError):
    """The user denied the interaction dialog, or the portal refused."""


class CancelledError(BackendError):
    """The portal cancelled the request."""


class PortalUnavailableError(BackendError):
    """No usable portal on the session bus."""


class PortalFailedError(BackendError):
    """The portal returned an error code."""


class UnsupportedCharacterError(BackendError):
    """A character cannot be typed by this backend."""


class CoordinateTranslator(ABC):
    """window-local -> global coordinate translation.

    M1 ships the identity translator (monitor/global coordinates only);
    window-aware translators (mutter RecordWindow, wlroots toplevel geometry)
    are the M3 coordinate core.
    """

    @abstractmethod
    def translate(self, local_x: float, local_y: float,
                  window_ref: object | None = None) -> tuple[float, float]: ...


class MonitorOnlyTranslator(CoordinateTranslator):
    """Identity: coordinates are already global (M1 behavior)."""

    def translate(self, local_x: float, local_y: float,
                  window_ref: object | None = None) -> tuple[float, float]:
        return float(local_x), float(local_y)


# evdev button codes (BTN_*), as used by the portal's NotifyPointerButton.
BUTTONS: dict[str, int] = {
    "left": 0x110,
    "right": 0x111,
    "middle": 0x112,
    "back": 0x113,
    "forward": 0x114,
}

PRESS, RELEASE = 1, 0
AXIS_VERTICAL, AXIS_HORIZONTAL = 0, 1


class Backend(ABC):
    """Input-injection backend contract."""

    name = "base"

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def status(self) -> dict: ...

    @abstractmethod
    def move_abs(self, x: float, y: float,
                 translator: CoordinateTranslator | None = None) -> None: ...

    @abstractmethod
    def move_rel(self, dx: float, dy: float) -> None: ...

    @abstractmethod
    def button(self, button: str | int, state: str) -> None: ...

    def click(self, button: str | int = "left") -> None:
        self.button(button, "press")
        self.button(button, "release")

    @abstractmethod
    def scroll(self, dx: int = 0, dy: int = -1, discrete: bool = True) -> None: ...

    @abstractmethod
    def key(self, keysym: str | int, state: str) -> None: ...

    def tap(self, keysym: str | int) -> None:
        self.key(keysym, "press")
        self.key(keysym, "release")

    @abstractmethod
    def type_text(self, text: str) -> None: ...
