"""Input-injection backends: portal (M1), wlroots (M2), uinput (M3)."""

from .base import (  # noqa: F401
    Backend,
    BackendError,
    CoordinateTranslator,
    MonitorOnlyTranslator,
)
from .portal import PortalSession  # noqa: F401
