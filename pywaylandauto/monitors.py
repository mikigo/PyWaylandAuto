"""Monitor layout in global logical coordinates, from mutter's DisplayConfig.

mutter's GetCurrentState signature (introspected on GNOME 50):

    GetCurrentState(out u serial,
                    out a((ssss)a(siiddada{sv})a{sv}) monitors,
                    out a(iiduba(ssss)a{sv}) logical_monitors,
                    out a{sv} properties);

Logical monitors carry x/y/scale but NOT width/height, so logical size is
derived from the physical monitor's current mode (modes[0]) divided by the
logical scale.  The portal's absolute-pointer coordinates live in this
logical coordinate space.
"""

import dbus
import dbus.exceptions


class MonitorLayoutUnavailableError(Exception):
    """The compositor's monitor layout could not be determined."""


def parse_display_config(serial, monitors, logical_monitors, properties) -> dict:
    """Pure parser over GetCurrentState's return values (unit-testable)."""
    modes_by_spec: dict[tuple, tuple[int, int]] = {}
    for specs, modes, _props in monitors:
        specs = tuple(str(s) for s in specs)
        width = height = 0
        if modes:  # the current mode is the first entry
            width, height = int(modes[0][1]), int(modes[0][2])
        modes_by_spec[specs] = (width, height)

    layout_monitors = []
    bbox = [0, 0, 0, 0]  # x, y, x_max, y_max
    first = True
    for x, y, scale, transform, primary, phys, _props in logical_monitors:
        x, y, scale = int(x), int(y), float(scale)
        if scale <= 0:
            scale = 1.0
        # A logical monitor may span several physical ones (tiled, typically
        # side by side): sum widths, take max height.  Exact for the common
        # single-physical case.
        widths, heights = [], []
        for p in phys:
            pw, ph = modes_by_spec.get(tuple(str(s) for s in p), (0, 0))
            if pw and ph:
                widths.append(pw)
                heights.append(ph)
        width = int(sum(widths) / scale) if widths else 0
        height = int(max(heights) / scale) if heights else 0
        layout_monitors.append({
            "x": x, "y": y, "width": width, "height": height,
            "scale": scale, "primary": bool(primary),
        })
        if first:
            bbox = [x, y, x + width, y + height]
            first = False
        else:
            bbox[0] = min(bbox[0], x)
            bbox[1] = min(bbox[1], y)
            bbox[2] = max(bbox[2], x + width)
            bbox[3] = max(bbox[3], y + height)

    if not layout_monitors:
        raise MonitorLayoutUnavailableError("no logical monitors reported")
    return {
        "bbox": {
            "x": bbox[0], "y": bbox[1],
            "width": bbox[2] - bbox[0], "height": bbox[3] - bbox[1],
        },
        "monitors": layout_monitors,
        "logical": True,
    }


def get_monitor_layout(bus=None) -> dict:
    """Query mutter's DisplayConfig on the session bus."""
    if bus is None:
        bus = dbus.SessionBus()
    try:
        obj = bus.get_object(
            "org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig"
        )
        iface = dbus.Interface(obj, "org.gnome.Mutter.DisplayConfig")
        serial, monitors, logical_monitors, properties = iface.GetCurrentState()
    except dbus.exceptions.DBusException as e:
        raise MonitorLayoutUnavailableError(
            f"mutter DisplayConfig unavailable: {e.get_dbus_name()}: {e}"
        ) from e
    return parse_display_config(serial, monitors, logical_monitors, properties)
