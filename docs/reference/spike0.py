#!/usr/bin/env python3
"""Spike 0 reference script — minimal RemoteDesktop portal session.

Standalone, dependency: dbus-python + PyGObject.  This is the
ground-truth-verified flow used to probe GNOME 50 before implementation
(see ../spike0.md).  Run it in a Wayland session: a GNOME "Allow remote
interaction?" dialog appears — click Allow, then watch the output.

Verified facts baked in:
  * handle_token must be a D-Bus object-path element ([A-Za-z0-9_] only)
  * CreateSession ALSO goes through the request/response flow: it returns
    a REQUEST object path; the session handle arrives in Response results
    as results["session_handle"].
  * Request object paths include the caller's unique name:
    /org/freedesktop/portal/desktop/request/{SENDER}/{TOKEN}
    with SENDER = unique name, ':' stripped, '.' -> '_'.
  * notify transport works as long as ConnectToEIS is never called;
    after ConnectToEIS every Notify* call fails with Error.Failed.
  * On denial the portal destroys the session object.
"""
import os
import sys
import time

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

BUS = dbus.SessionBus()
PORTAL = BUS.get_object("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop")
RD = dbus.Interface(PORTAL, "org.freedesktop.portal.RemoteDesktop")

loop = GLib.MainLoop()
started_at = time.time()
session_path = None


def log(msg):
    print(f"[spike] {msg}", flush=True)


def request_path(token):
    sender = BUS.get_unique_name().lstrip(":").replace(".", "_")
    return f"/org/freedesktop/portal/desktop/request/{sender}/{token}"


def register_response(token, on_response):
    """Register Response receivers BEFORE making the call (no race)."""
    path = request_path(token)
    log(f"listening for Response on {path}")
    BUS.add_signal_receiver(
        lambda response, results: on_response(response, results),
        signal_name="Response",
        dbus_interface="org.freedesktop.portal.Request",
        path=path,
    )
    BUS.add_signal_receiver(
        lambda token_, response, results: on_response(response, results),
        signal_name="Response",
        dbus_interface="org.freedesktop.portal.PersistentRequest",
        path=path,
    )
    return path


def on_properties_changed(interface, changed, invalidated):
    log(f"PropertiesChanged interface={interface!r} changed={dict(changed)} invalidated={invalidated}")


def probe_notify(label):
    try:
        RD.NotifyPointerMotion(session_path, {}, 0.0, 0.0)
        log(f"PROBE {label}: notify_pointer_motion -> OK (notify transport viable)")
        return True
    except dbus.exceptions.DBusException as e:
        log(f"PROBE {label}: notify_pointer_motion -> DBusException name={e.get_dbus_name()!r} msg={e}")
        return False


def close_session():
    if session_path is None:
        return
    try:
        session_obj = BUS.get_object("org.freedesktop.portal.Desktop", session_path)
        dbus.Interface(session_obj, "org.freedesktop.portal.Session").Close()
        log("Session.Close() sent")
    except dbus.exceptions.DBusException as e:
        log(f"Session.Close() failed: {e}")


def on_start_response(response, results):
    log(f"Start Response: code={response} keys={sorted(results.keys())}")
    if "restore_token" in results:
        token = str(results["restore_token"])
        log(f"restore_token present: {token!r} (len={len(token)})")
        with open("/tmp/spike0-token", "w") as f:
            f.write(token)
    else:
        log("restore_token MISSING from Start results")
    if response != 0:
        log(f"Start FAILED with code {response}; exiting")
        close_session()
        loop.quit()
        return

    probe_notify("before ConnectToEIS")

    try:
        fd = RD.ConnectToEIS(session_path, {})
        fd = fd.take()
        log(f"ConnectToEIS -> fd {fd}")
        try:
            os.set_blocking(fd, False)
        except OSError:
            pass
        time.sleep(0.5)
        try:
            data = os.read(fd, 128)
        except (BlockingIOError, OSError):
            data = b""
        if data:
            log(f"EIS fd first {len(data)} bytes: {data.hex()}")
            import struct
            obj_id, length, opcode = struct.unpack("=QII", data[:16])
            log(f"EIS header: object_id={obj_id} length={length} opcode={opcode}")
        else:
            log("EIS fd: no bytes readable yet")
        os.close(fd)
    except dbus.exceptions.DBusException as e:
        log(f"ConnectToEIS -> DBusException name={e.get_dbus_name()!r} msg={e}")

    probe_notify("after ConnectToEIS")
    close_session()
    log("Spike complete")
    loop.quit()


def on_select_devices_response(response, results):
    log(f"SelectDevices Response: code={response} keys={sorted(results.keys())}")
    if response != 0:
        log(f"SelectDevices FAILED code={response}")
        loop.quit()
        return
    register_response("spike0_start", on_start_response)
    RD.Start(session_path, "", {"handle_token": dbus.String("spike0_start")})
    log("Start() called, waiting for Response...")


def on_create_session_response(response, results):
    global session_path
    log(f"CreateSession Response: code={response} keys={sorted(results.keys())}")
    if response != 0:
        log(f"CreateSession FAILED code={response}")
        loop.quit()
        return
    session_path = str(results["session_handle"])
    log(f"session_handle -> {session_path}")

    BUS.add_signal_receiver(
        on_properties_changed,
        signal_name="PropertiesChanged",
        dbus_interface="org.freedesktop.DBus.Properties",
        path=session_path,
    )
    BUS.add_signal_receiver(
        lambda details: log(f"Session.Closed details={dict(details)}"),
        signal_name="Closed",
        dbus_interface="org.freedesktop.portal.Session",
        path=session_path,
    )

    register_response("spike0_select", on_select_devices_response)
    RD.SelectDevices(session_path, {
        "types": dbus.UInt32(3),
        "persist_mode": dbus.UInt32(2),
        "handle_token": dbus.String("spike0_select"),
    })
    log("SelectDevices() called")
    log(">>> A GNOME 'Allow remote interaction?' dialog should appear — please click Allow <<<")


def main():
    timeout_s = 180
    register_response("spike0_create", on_create_session_response)
    create_result = str(RD.CreateSession({
        "handle_token": dbus.String("spike0_create"),
        "session_handle_token": dbus.String("pywaylandauto_spike0"),
    }))
    log(f"CreateSession() -> returned request handle {create_result}")

    def watchdog():
        if time.time() - started_at > timeout_s:
            log(f"TIMEOUT after {timeout_s}s (dialog not granted?) — exiting")
            close_session()
            loop.quit()
            return False
        return True

    GLib.timeout_add_seconds(5, watchdog)
    loop.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
