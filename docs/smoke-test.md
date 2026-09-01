# 冒烟测试流程

在 GNOME Wayland 会话中验证 PyWaylandAuto 的完整链路。

## 前置

```bash
cd ~/pywaylandauto
python3 -m venv --system-site-packages .venv   # 复用系统 dbus-python + PyGObject
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -v                  # 期望全部通过
```

## 流程

```bash
# 1. 首次启动 — 桌面弹出 "Allow remote interaction?",点「允许」
.venv/bin/pywaylandauto daemon start --foreground --log-file /tmp/pwa.log &

# 2. 授权后 status 显示 started(transport 应为 eis)
.venv/bin/pywaylandauto status
#   session: state=started transport=eis devices=['3'] has_token=True persist_mode=2

# 3. 移动(分步观察)
.venv/bin/pywaylandauto move 2048 1080     # 光标跳到屏幕中央
.venv/bin/pywaylandauto move 100 100       # 光标跳到左上角
.venv/bin/pywaylandauto move-rel 200 -150
.venv/bin/pywaylandauto scroll --dy 3

# 4. 键盘 — 打开 gnome-text-editor 使其获得焦点后
.venv/bin/pywaylandauto type "hello pywaylandauto"
.venv/bin/pywaylandauto key Return

# 5. 重启验证 token 生效(不应再弹窗)
.venv/bin/pywaylandauto daemon stop
.venv/bin/pywaylandauto daemon start --foreground --log-file /tmp/pwa2.log &
.venv/bin/pywaylandauto click                     # 期望:无弹窗
stat -c '%a %n' ~/.local/state/pywaylandauto/portal.token   # 期望 600
```

## 故障排查

- `dbus-monitor --session "interface='org.freedesktop.portal.RemoteDesktop'"`
  观察 portal 侧调用
- `PYWAYLANDAUTO_TRANSPORT=notify` 强制 notify 传输对比
  (注意:notify 传输不支持绝对移动,这是 mutter 的限制)
- 弹窗被拒绝后 portal 会销毁会话对象;重新授权:
  `.venv/bin/pywaylandauto session-start --wait`
- daemon 日志默认在 `$XDG_STATE_HOME/pywaylandauto/daemon.log`
  (前台模式输出到 stderr)
