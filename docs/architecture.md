# 代码架构

> M1 实现说明;与 [设计方案](design.md) 互为表里。行内引用为
> `模块名:符号`。

## 1. 文件树

```
src/pywaylandauto/
├── __init__.py            # __version__
├── __main__.py            # python -m pywaylandauto → cli.main()
├── cli.py                 # 子命令解析;权限弹窗轮询;chord 分解;daemon 托管
├── client.py              # Client: socket 客户端 + JSON 协议 + auto-spawn
├── protocol.py            # 帧编码/校验、Request/Response、错误码(纯 stdlib)
├── daemon.py              # Daemon(GLib 循环、socket 服务、分发)+ SessionManager 职责
├── token_cache.py         # TokenCache: load/save/revoke(原子写 0600/0700)
├── monitors.py            # mutter DisplayConfig → 逻辑布局 bbox
├── keysyms.py             # X11 keysym 名称表(Latin-1/Unicode/hex)
└── backends/
    ├── base.py            # Backend ABC、CoordinateTranslator、类型化错误、按钮码
    ├── portal.py          # PortalClient(D-Bus 胶水) + PortalSession(状态机/双传输)
    ├── eis.py             # EisClient: EIS 线协议客户端(socket/struct 纯 stdlib)
    ├── eis_messages.py    # 线协议编解码 + opcode 表(照 protocol.xml 1.5.0)
    └── xkb.py             # xkb_keymap 文本解析 + US evdev 回退表
```

## 2. 数据流(一次 `pywaylandauto click`)

```
cli._call_with_dialog_wait
  → client.request("input.click")            # socket 一行 JSON
    → daemon._dispatch → _ensure_started     # 未启动则发起会话 → permission_pending
      → portal.click → button(press) + button(release)
        → eis: start_emulating → ei_button.button → frame → stop_emulating ×2
  ← {"id": N, "ok": true, "result": {}}
```

CLI 收到 `permission_pending` → 提示用户点弹窗 → 每秒轮询 `status`
直到 `started`(最长 60s)→ 重试原请求一次。

## 3. PortalSession 状态机

```
INIT ──start()──▶ STARTING ──Start 响应 code 0──▶ STARTED
  ▲                  │  │                            │  │
  │                  │  └─ CreateSession/SelectDevices 失败
  │                  │     或 Start code 1(用户拒绝)
  │                  ▼
  └────────────── STOPPED ◀── 外部关闭(Closed 信号)/ 探测失败 / _fail()
```

- 状态字符串:`init / starting / started / stopped`(直接进 JSON)
- `start()` 幂等(进行中/已启动直接返回);每次 start 重读 token 缓存
- 拒绝后 `last_error = PermissionDeniedError`:输入方法直接报
  `permission_denied`(不自动重启,防弹窗风暴);`session.start` 手动重授
- `init/stopped(外部关闭)` 时输入方法自动发起会话流程并报
  `permission_pending`
- 状态转换全部由 **Response 信号码**驱动;PropertiesChanged 仅 Closed
  信号参与(见 spike0.md §2-3)

## 4. 传输选择(portal.py:_choose_transport)

```
Start 成功
  ├─ PYWAYLANDAUTO_TRANSPORT=notify → 零位移探针 → notify | 失败
  └─ 默认 → ConnectToEIS
       ├─ D-Bus 失败 → 回退 notify(会话还未进 EIS-mode)
       └─ 成功 → EisClient.handshake()
            ├─ 完成 → transport=eis(等 keymap 到齐)
            └─ 失败 → 会话报废(portal 已标记 EIS-mode)→ stopped
```

## 5. D-Bus 层隔离(PortalClient)

- `PortalClient` 是唯一接触 dbus-python 的类:所有方法、信号注册、
  request 路径拼接(sender 段!)、错误都在这层
- `PortalSession` 只依赖它的窄接口 → 状态机可对 FakePortalClient
  完全离线测试;将来要平移 gi.Gio.DBus 也只动这一个文件
- 接线铁律:**先 add_response_listener 再调用**;Response 与
  PersistentRequest 两个接口都注册(带 persist_mode 时 request 对象
  实现后者,签名多一个 token 参数)

## 6. 键盘链(keysym → 事件)

```
用户输入(名称/字符/组合键)
  → keysyms.lookup         # XK 名称表、单字符 Latin-1/Unicode、0x 形式
      ├─ eis 传输 → xkb.build_resolver(keymap_text)
      │     ├─ 解析出的 keymap 命中 → (keycode, level)
      │     └─ 未命中 → US evdev 表兜底(部分 keymap 常见)
      │   level 0 → 直接 key
      │   level 1 → 自管理 Shift_L 环绕(mutter #3375)
      │   level ≥2 → UnsupportedCharacterError
      └─ notify 传输 → NotifyKeyboardKeysym(keysym 直接给 portal)
```

- 打字 Latin-1 范围(M1 边界,超界报 `unsupported_character`)
- EIS 下每次 key 事件独立成帧;press/release 严格分帧

## 7. 错误映射

| 异常(backends.base) | 协议错误码 |
|---|---|
| ProtocolError | 其自带 code |
| PermissionPendingError | `permission_pending` |
| PermissionDeniedError | `permission_denied` |
| CancelledError | `cancelled` |
| PortalUnavailableError | `portal_unavailable` |
| PortalFailedError | `portal_failed` |
| SessionNotStartedError | `session_not_started` |
| UnsupportedCharacterError | `unsupported_character` |
| MonitorLayoutUnavailableError | `monitor_layout_unavailable` |
| 其余 BackendError | `backend_error` |
| ValueError | `invalid_params` |
| 其他 | `internal_error`(记日志) |

D-Bus 名 → 异常:NotAllowed→Denied、Cancelled→Cancelled、
ServiceUnknown/UnknownMethod/UnknownInterface→Unavailable、
其余→Failed(portal.py:map_dbus_error)。

## 8. 测试策略(181 个用例)

| 层 | 手段 | 文件 |
|---|---|---|
| 纯逻辑 | 直接单测 | test_protocol / test_token_cache / test_keysyms / test_monitors / test_xkb |
| 状态机 | FakePortalClient(脚本化替身,一次性错误,同步 fire) | test_state_machine / test_portal |
| EIS 编解码 | round-trip 单测 | test_eis_messages |
| EIS 客户端 | FakeEisServer(socketpair 对端,真实线协议,记录断言) | test_eis_client / test_eis_portal |
| 端到端 | 真 daemon 线程 + 真 socket + 假 portal | test_daemon_client |
| 协议客户端 | 脚本化 socket 服务 | test_client |

原则:每发现一个真机行为(devices 标量、token 字符集、EIS-mode 门控
……)都补一条回归测试 —— 真机冒烟只验证"活系统",单元测试才保回归。

## 9. 需要保持的不变量(改代码前必读)

1. handle_token / session_handle_token 仅 `[A-Za-z0-9_]`
2. request 路径必须带 sender 段
3. 信号接收器先注册、后调用;回调内不抛异常
4. 每帧一对 press/release 分帧;start_emulating/frame/stop_emulating
   严格包裹
5. token 每次 Start 响应覆盖保存;保存用原子写
6. devices 结果按"标量或数组"归一化
7. 单线程假设:所有 GLib 回调与 dispatch 在同一线程
