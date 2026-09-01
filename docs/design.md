# 设计方案

> 状态:设计已按本文档实现并真机验证(GNOME 50 Wayland,2026-08)。
> 配套文档:[调研报告](research.md) · [Spike 0 实测](spike0.md) ·
> [EIS 协议笔记](eis-protocol.md) · [代码架构](architecture.md) ·
> [路线图](roadmap.md) · [已知问题](known-issues.md)

## 1. 背景与目标

Wayland 协议出于安全考虑禁止普通客户端注入全局键鼠事件(X11 的
XTest 在 Wayland 下不存在),所有 GUI 自动化工具必须重新回答
"事件从哪进"。PyWaylandAuto 的答案是:**通过 XDG Desktop Portal
RemoteDesktop 获得合法的输入注入能力**,并以 daemon 长连接缓存授权
restore token,让 GNOME 的 "Allow remote interaction?" 弹窗只出现一次。

**M1 目标(已达成)**:portal 后端(全局坐标:移动/点击/滚轮/按键/打字)
+ UNIX socket daemon(token 缓存)+ 客户端库 + CLI,GNOME 50 真机可用。

## 2. 架构

```
CLI / 你的测试代码
   └── Client (UNIX socket + 换行分隔 JSON, auto-spawn daemon)
         └── Daemon (GLib 主循环, 持有唯一 portal 会话)
               └── XDG RemoteDesktop portal
                     ├── 默认: ConnectToEIS → libei 线协议(EisClient, 纯 stdlib)
                     └── 回退: Notify* D-Bus 调用(PYWAYLANDAUTO_TRANSPORT=notify)
```

### 2.1 为什么是 daemon + 客户端库(而非纯 CLI)

- portal/libei 会话是有状态的:libei 不是为短命工具设计的,每次新建
  会话都要走一遍授权流程。daemon 常驻持有会话,弹窗只在首次出现。
- restore token 按会话轮换,daemon 是唯一合理的缓存归属地。
- 单 daemon、单共享会话:token 流程按会话计,单会话 = 弹窗一次;
  且单线程串行处理请求,天然无并发竞争(click = press+release 两帧
  完整处理后才会读下一个请求)。

### 2.2 通信:UNIX socket + JSON

- socket:`$XDG_RUNTIME_DIR/pywaylandauto.sock`(回退 `/tmp/...`),0600
- 帧:每行一个 UTF-8 JSON 对象,请求带 `id`,响应回显 `id`
- 完整规格见 [protocol.md](protocol.md);零依赖、易调试、后期可平移 D-Bus

### 2.3 主循环:GLib 单线程

dbus-python 的异步 `reply_handler` 需要主循环;同一循环同时 watch
客户端 socket 与 EIS fd。无线程、无 asyncio。所有 D-Bus 调用都挤在
`backends/portal.py` 的 `PortalClient` 薄封装里 —— 若 dbus-python 在
未来 Python 版本出问题,可整体平移 gi.Gio.DBus 而不动状态机。

### 2.4 token 缓存位置:XDG_STATE_HOME

`$XDG_STATE_HOME/pywaylandauto/portal.token`(默认 `~/.local/state/...`),
目录 0700、文件 0600、临时文件 + `os.replace` 原子写。理由:restore
token 是持久授权凭证(persist_mode=2 下有效到被撤销),必须跨重启
存活 —— XDG_RUNTIME_DIR 会在注销时被清空,~/.cache 语义是可再生的
数据。可用 `PYWAYLANDAUTO_TOKEN_FILE` 覆盖(CI 注入)。

## 3. 传输选择:EIS 优先

默认走 **EIS**(`ConnectToEIS` 拿到 fd,纯 Python 实现 libei 线协议),
原因是一个实测出来的硬限制:

> mutter 的 `NotifyPointerMotionAbsolute` 校验坐标必须在某个 screen
> cast stream 内;而 RemoteDesktop portal 的 `AvailableDeviceTypes=7`
> (键盘|指针|触摸)根本不支持屏幕流类型 —— 所以 **notify 路径在
> GNOME 上无法做绝对移动**("Invalid position",见 spike0.md §5)。

EIS 的 `motion_absolute` 走虚拟表面,不需要 stream —— 这也是 wdotool、
KDE Connect 在 GNOME 上用 EIS 的原因。选择逻辑:

1. `PYWAYLANDAUTO_TRANSPORT=notify` → 强制 notify(零位移探针验证)
2. 默认 → ConnectToEIS + 握手;**D-Bus 调用失败** → 回退 notify
   (此时会话尚未进入 EIS-mode,notify 仍可用)
3. **握手失败** → 会话报废(portal 已把会话标记为 EIS-mode,
   notify 被永久拒绝 "Session is not allowed to call Notify* methods")
   → 会话状态 stopped,报 portal_failed

EIS 下键盘事件用 mutter 发来的真实 XKB keymap 做 keysym→keycode
翻译(keymap 文本格式,36345 字节实测);解析出的 keymap 优先、
内置 US evdev 表兜底(部分 keymap 常见,见 architecture.md §6)。

## 4. 关键设计决策与理由

| # | 决策 | 理由 |
|---|---|---|
| D1 | 单共享会话 | token 按会话计;单线程串行天然原子 |
| D2 | EIS 默认传输 | notify 无法绝对移动(GNOME 硬限制) |
| D3 | 状态机信任 Response 码 | PropertiesChanged 在 GNOME 50 实测不触发 |
| D4 | handle_token 仅 `[A-Za-z0-9_]` | D-Bus 对象路径约束,`-` 会被拒(旧版直接崩) |
| D5 | 先注册信号接收器再调用 | 防竞态(Response 可能先于调用返回到达) |
| D6 | 回调内捕获一切异常 | dbus-python 信号回调抛异常只打警告,状态机会卡死 |
| D7 | token 被拒自动撤销重试一次 | 弹窗重现是正确行为(授权确实失效了) |
| D8 | 每次 start() 重读缓存 | CI 中途注入 token 文件、撤销后重开都要生效 |
| D9 | keymap 链式解析 | 部分 keymap 很常见,缺失键退回 US 表 |
| D10 | Shift 自管理(EIS) | mutter #3375:EIS 客户端收不到修饰键事件 |
| D11 | 拒绝后不自动重启会话 | 防弹窗风暴;`session.start` 手动重授权 |

## 5. M1 验收标准(全部达成)

1. ✅ pytest 全绿(协议帧、token、状态机、EIS 编解码/客户端、集成)
2. ✅ 本机:daemon start → GNOME 弹窗出现一次 → 授权后 started
3. ✅ move/move-rel/click/scroll/key/type 在编辑器真机验证
4. ✅ daemon 重启 → **无弹窗**(token 生效);文件 600/目录 700
5. ✅ 错误路径:auto-spawn、already_running、method_not_found 等
