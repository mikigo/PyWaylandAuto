"""Command-line interface for pywaylandauto."""

import argparse
import logging
import os
import subprocess
import sys
import time

from . import __version__, protocol
from .client import Client
from .daemon import AlreadyRunningError, Daemon, default_pid_path, default_socket_path
from .token_cache import TokenCache

DIALOG_WAIT_SECONDS = 60
DIALOG_POLL_INTERVAL = 1.0


def default_log_path() -> str:
    state_home = os.environ.get("XDG_STATE_HOME") or os.path.expanduser(
        "~/.local/state"
    )
    return os.path.join(state_home, "pywaylandauto", "daemon.log")


def _die(message: str, code: int = 1) -> None:
    print(f"pywaylandauto: {message}", file=sys.stderr)
    sys.exit(code)


def _setup_logging(log_file: str | None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_file)] if log_file
        else [logging.StreamHandler(sys.stderr)],
    )


# -- daemon commands -----------------------------------------------------

def cmd_daemon_start(args) -> int:
    if not args.foreground:
        cmd = [sys.executable, "-m", "pywaylandauto", "daemon", "start", "--foreground"]
        if args.log_file:
            cmd += ["--log-file", args.log_file]
        if args.no_session_autostart:
            cmd.append("--no-session-autostart")
        if args.transport:
            cmd += ["--transport", args.transport]
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # Wait for the socket to appear so callers can proceed immediately.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if os.path.exists(args.socket_path):
                print(f"daemon started (pid {_read_pid(args.pid_path) or '?'})")
                return 0
            time.sleep(0.1)
        _die(f"daemon did not start within 3s — see {args.log_file or default_log_path()}")
    _setup_logging(args.log_file)
    daemon = Daemon(
        socket_path=args.socket_path,
        pid_path=args.pid_path,
        auto_start_session=not args.no_session_autostart,
    )
    try:
        return daemon.run()
    except AlreadyRunningError as e:
        _die(str(e))


def _read_pid(pid_path: str) -> str | None:
    try:
        with open(pid_path) as f:
            return f.read().strip()
    except OSError:
        return None


def cmd_daemon_stop(args) -> int:
    client = Client(socket_path=args.socket_path, auto_spawn=False)
    try:
        client.daemon_stop()
    except (ConnectionError, FileNotFoundError) as e:
        _die(f"no running daemon ({e})")
    except protocol.RemoteError as e:
        _die(f"daemon reported: {e}")
    print("daemon stopped")
    return 0


def cmd_daemon_restart(args) -> int:
    cmd_daemon_stop(args)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and os.path.exists(args.socket_path):
        time.sleep(0.1)
    return cmd_daemon_start(args)


def cmd_daemon_status(args) -> int:
    if os.path.exists(args.pid_path):
        print(f"pid: {_read_pid(args.pid_path)}")
        return 0
    _die("no running daemon (no pid file)")


# -- input commands ------------------------------------------------------

def _client_for(args) -> Client:
    return Client(socket_path=args.socket_path, auto_spawn=args.auto_spawn)


def _call_with_dialog_wait(client: Client, method: str, params: dict | None = None) -> dict:
    """One retry with status polling: first call may kick off the session
    flow, which pops the GNOME dialog; poll until granted, then retry once."""
    try:
        return client.request(method, params)
    except protocol.RemoteError as e:
        if e.code == protocol.ERR_PERMISSION_PENDING:
            print("等待授权弹窗…(请在桌面点击“允许”)", file=sys.stderr)
            deadline = time.monotonic() + DIALOG_WAIT_SECONDS
            while time.monotonic() < deadline:
                time.sleep(DIALOG_POLL_INTERVAL)
                try:
                    state = client.status()["session"]["state"]
                except Exception:
                    state = None
                if state == "started":
                    break
            else:
                _die(f"授权超时({DIALOG_WAIT_SECONDS}s);用 `pywaylandauto session-start` 重试")
            return client.request(method, params)
        if e.code == protocol.ERR_PERMISSION_DENIED:
            _die("授权被拒绝;用 `pywaylandauto session-start` 重新授权")
        raise


def cmd_status(args) -> int:
    client = _client_for(args)
    try:
        result = client.status()
    except (ConnectionError, protocol.RemoteError) as e:
        _die(str(e))
    session = result["session"]
    layout = result.get("layout")
    print(f"daemon: pid {result['daemon']['pid']} socket {result['daemon']['socket']}")
    print(f"session: state={session['state']} transport={session['transport']} "
          f"devices={session['devices']} has_token={session['has_token']} "
          f"persist_mode={session['persist_mode']}")
    if session.get("error"):
        print(f"last error: {session['error']}")
    if layout:
        b = layout["bbox"]
        print(f"layout: {b['width']}x{b['height']}+{b['x']}+{b['y']} "
              f"({len(layout['monitors'])} monitor(s))")
    return 0


def cmd_session_start(args) -> int:
    client = _client_for(args)
    result = _call_with_dialog_wait(client, "session.start") \
        if args.wait else client.session_start()
    print(f"session state: {result['state']}")
    return 0


def cmd_move(args) -> int:
    return _run_input(args, "input.move_abs", {"x": args.x, "y": args.y})


def cmd_move_rel(args) -> int:
    return _run_input(args, "input.move_rel", {"dx": args.dx, "dy": args.dy})


def cmd_click(args) -> int:
    return _run_input(args, "input.click", {"button": args.button})


def cmd_button(args) -> int:
    return _run_input(args, "input.button", {"button": args.button, "state": args.state})


def cmd_scroll(args) -> int:
    return _run_input(
        args, "input.scroll",
        {"dx": args.dx, "dy": args.dy, "discrete": not args.smooth},
    )


def _split_chord(spec: str) -> tuple[list[str], str]:
    parts = [p.strip() for p in spec.split("+")]
    if not parts or any(not p for p in parts):
        _die(f"invalid key spec {spec!r}")
    return parts[:-1], parts[-1]


def cmd_key(args) -> int:
    client = _client_for(args)
    mods, key = _split_chord(args.keyspec)
    for mod in mods:
        _call_with_dialog_wait(client, "input.key", {"keysym": mod, "state": "press"})
    _call_with_dialog_wait(client, "input.key", {"keysym": key, "state": "tap"})
    for mod in reversed(mods):
        _call_with_dialog_wait(client, "input.key", {"keysym": mod, "state": "release"})
    return 0


def cmd_type(args) -> int:
    return _run_input(args, "input.type_text", {"text": args.text})


def _run_input(args, method: str, params: dict) -> int:
    client = _client_for(args)
    try:
        _call_with_dialog_wait(client, method, params)
    except protocol.RemoteError as e:
        _die(f"{e.code}: {e.message}")
    except (ConnectionError, TimeoutError) as e:
        _die(str(e))
    return 0


# -- parser --------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pywaylandauto",
                                     description="Wayland 键鼠输入注入工具")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p):
        p.add_argument("--socket", dest="socket_path", default=None,
                       help=f"daemon socket (default {default_socket_path()})")
        p.add_argument("--no-spawn", dest="auto_spawn", action="store_false",
                       default=True, help="don't auto-spawn the daemon")

    p = sub.add_parser("daemon", help="daemon 生命周期管理")
    dsub = p.add_subparsers(dest="daemon_command", required=True)
    ps = dsub.add_parser("start", help="启动 daemon(默认后台运行)")
    ps.add_argument("--foreground", action="store_true")
    ps.add_argument("--log-file", default=None)
    ps.add_argument("--no-session-autostart", action="store_true")
    ps.add_argument("--transport", choices=["notify", "eis"], default=None)
    ps.add_argument("--socket", dest="socket_path", default=None)
    ps.add_argument("--pid-file", dest="pid_path", default=None)
    ps.set_defaults(func=cmd_daemon_start)
    pst = dsub.add_parser("stop", help="停止 daemon")
    pst.add_argument("--socket", dest="socket_path", default=None)
    pst.set_defaults(func=cmd_daemon_stop)
    pr = dsub.add_parser("restart", help="重启 daemon")
    pr.add_argument("--socket", dest="socket_path", default=None)
    pr.add_argument("--pid-file", dest="pid_path", default=None)
    pr.add_argument("--foreground", action="store_true")
    pr.add_argument("--log-file", default=None)
    pr.add_argument("--no-session-autostart", action="store_true")
    pr.add_argument("--transport", choices=["notify", "eis"], default=None)
    pr.set_defaults(func=cmd_daemon_restart)
    pd = dsub.add_parser("status", help="daemon 进程状态")
    pd.add_argument("--pid-file", dest="pid_path", default=None)
    pd.set_defaults(func=cmd_daemon_status)

    p = sub.add_parser("status", help="会话与显示器布局状态")
    add_common(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("session-start", help="手动发起 portal 会话(重新弹授权窗)")
    add_common(p)
    p.add_argument("--wait", action="store_true",
                   help="等待授权弹窗被允许(最长 60s)")
    p.set_defaults(func=cmd_session_start)

    p = sub.add_parser("move", help="绝对移动指针 move X Y(全局逻辑坐标)")
    add_common(p)
    p.add_argument("x", type=float)
    p.add_argument("y", type=float)
    p.set_defaults(func=cmd_move)

    p = sub.add_parser("move-rel", help="相对移动指针 move-rel DX DY")
    add_common(p)
    p.add_argument("dx", type=float)
    p.add_argument("dy", type=float)
    p.set_defaults(func=cmd_move_rel)

    p = sub.add_parser("click", help="点击(默认左键)")
    add_common(p)
    p.add_argument("--button", default="left", choices=["left", "right", "middle"])
    p.set_defaults(func=cmd_click)

    p = sub.add_parser("button", help="按键动作 button NAME press|release")
    add_common(p)
    p.add_argument("button")
    p.add_argument("state", choices=["press", "release"])
    p.set_defaults(func=cmd_button)

    p = sub.add_parser("scroll", help="滚轮 scroll [--dx N] [--dy N]")
    add_common(p)
    p.add_argument("--dx", type=int, default=0)
    p.add_argument("--dy", type=int, default=-1)
    p.add_argument("--smooth", action="store_true", help="平滑滚动(非离散)")
    p.set_defaults(func=cmd_scroll)

    p = sub.add_parser("key", help="按键 key KEYSPEC(支持组合键,如 ctrl+shift+t)")
    add_common(p)
    p.add_argument("keyspec")
    p.set_defaults(func=cmd_key)

    p = sub.add_parser("type", help="输入文本 type TEXT(Latin-1)")
    add_common(p)
    p.add_argument("text")
    p.set_defaults(func=cmd_type)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Resolve socket/pid paths to the same defaults everywhere.
    for attr in ("socket_path", "pid_path"):
        if hasattr(args, attr) and getattr(args, attr) is None:
            if attr == "socket_path":
                setattr(args, attr, default_socket_path())
            else:
                setattr(args, attr, default_pid_path())
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
