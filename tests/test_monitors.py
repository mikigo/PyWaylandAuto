import dbus.exceptions
import pytest

from pywaylandauto.monitors import (
    MonitorLayoutUnavailableError,
    get_monitor_layout,
    parse_display_config,
)


def _spec(vendor, product, serial, name):
    return tuple(map(dbus.String, [vendor, product, serial, name]))


def _mode(id_, w, h):
    return (
        dbus.String(id_), dbus.Int32(w), dbus.Int32(h),
        dbus.Double(60.0), dbus.Double(1.0), dbus.Array([], signature="d"),
        dbus.Dictionary({}, signature="sv"),
    )


def test_single_monitor_layout():
    # Shape mirrors the real GetCurrentState output on GNOME 50.
    monitors = [(_spec("Virtual-1", "unknown", "unknown", "unknown"),
                 [_mode("4096x2160@60", 4096, 2160)], {})]
    logical = [(0, 0, 1.0, 0, True, [(_spec("Virtual-1", "unknown", "unknown", "unknown"))], {})]
    layout = parse_display_config(1, monitors, logical, {})
    assert layout["bbox"] == {"x": 0, "y": 0, "width": 4096, "height": 2160}
    assert layout["monitors"][0]["primary"] is True


def test_scale_divides_physical_size():
    monitors = [(_spec("DP-1", "u", "u", "DP-1"), [_mode("3840x2160@60", 3840, 2160)], {})]
    logical = [(0, 0, 2.0, 0, True, [(_spec("DP-1", "u", "u", "DP-1"))], {})]
    layout = parse_display_config(1, monitors, logical, {})
    assert layout["bbox"]["width"] == 1920
    assert layout["bbox"]["height"] == 1080


def test_tiled_side_by_side_logical_monitor():
    spec_a, spec_b = _spec("A", "u", "u", "A"), _spec("B", "u", "u", "B")
    monitors = [
        (spec_a, [_mode("1920x1080", 1920, 1080)], {}),
        (spec_b, [_mode("1920x1080", 1920, 1080)], {}),
    ]
    logical = [(0, 0, 1.0, 0, True, [spec_a, spec_b], {})]
    layout = parse_display_config(1, monitors, logical, {})
    # side-by-side tiling: widths sum, height is the max
    assert layout["bbox"] == {"x": 0, "y": 0, "width": 3840, "height": 1080}


def test_two_logical_monitors_bbox_union():
    spec_a, spec_b = _spec("A", "u", "u", "A"), _spec("B", "u", "u", "B")
    monitors = [
        (spec_a, [_mode("1920x1080", 1920, 1080)], {}),
        (spec_b, [_mode("1920x1080", 1920, 1080)], {}),
    ]
    logical = [
        (0, 0, 1.0, 0, True, [spec_a], {}),
        (1920, 0, 1.0, 0, False, [spec_b], {}),
    ]
    layout = parse_display_config(1, monitors, logical, {})
    assert layout["bbox"] == {"x": 0, "y": 0, "width": 3840, "height": 1080}
    assert [m["primary"] for m in layout["monitors"]] == [True, False]


def test_no_logical_monitors_raises():
    with pytest.raises(MonitorLayoutUnavailableError):
        parse_display_config(1, [], [], {})


class _FakeBus:
    def get_object(self, dest, path):
        class _Obj:
            def __init__(self):
                self.dest = dest
        return _Obj()


def test_get_monitor_layout_wraps_dbus_errors():
    def boom(*a, **kw):
        raise dbus.exceptions.DBusException("ServiceUnknown")
    bus = _FakeBus()
    orig = dbus.Interface
    dbus.Interface = lambda obj, iface: type("Iface", (), {"GetCurrentState": boom})()
    try:
        with pytest.raises(MonitorLayoutUnavailableError) as exc:
            get_monitor_layout(bus)
        assert "DisplayConfig" in str(exc.value)
    finally:
        dbus.Interface = orig
