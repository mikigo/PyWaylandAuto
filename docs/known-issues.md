# 已知问题与风险

> 记录,不隐瞒。编号沿用 [路线图](roadmap.md) 的 R1–R9。

## 运行时限制(M1 已知边界)

### K1 notify 传输不支持绝对移动

`PYWAYLANDAUTO_TRANSPORT=notify` 时 `move_abs` 报 `backend_error`。
根因:mutter 对 `NotifyPointerMotionAbsolute` 按 screen cast stream 校验
坐标,而 RemoteDesktop portal 不支持屏幕流类型(见 spike0.md §5)。
默认 EIS 传输不受影响。

### K2 打字仅 Latin-1

`type` 超出 U+00FF 报 `unsupported_character`。EIS 下 Shift 双级,
AltGr/死键/CapsLock 未处理(见 R6)。

### K3 仅 GNOME 验证过

KDE Plasma 6 的 portal 实现未实测;wlroots 系需 M2 后端。

### K4 单显示器坐标域验证

EIS region 在单逻辑显示器(4096×2160)上验证;多显示器、混合缩放
的 region 切分语义未实测(见 R2)。

## 风险(R1–R9)

### R1 CI 弹窗自动化(未解决)

GNOME 授权提示没有编程点击之法(Shell Eval 需要 unsafe mode)。
既定方案:每台机器预授权一次,token 作 CI secret 注入
`PYWAYLANDAUTO_TOKEN_FILE`;需要真实弹窗的集成测试在 CI 跳过。
M2 评估 InputCapture 被动捕获(portals.conf 白名单免弹窗)。

### R2 EIS 绝对移动的区域语义(部分验证)

mutter 当前给虚拟设备一个覆盖全逻辑布局的 region;多显示器时可能
一个绝对设备一个 region,客户端必须"选对设备投对区域"。现实现:
单指针绝对设备 + region 包含性检查,越界即报错(宁可失败不静默)。

### R3(已解决)notify 探针≠绝对移动可用

Spike 0 只测了相对零位移,导致绝对移动问题晚到冒烟才暴露。
教训入 spike0.md §7;现在任何传输能力变更都要按操作类型验证。

### R4 dbus-python on Python 3.14(观察中)

当前正常。若出问题:D-Bus 层已隔离(PortalClient),平移 gi.Gio.DBus。

### R5 GNOME 静默忽略过期 token(无探测手段)

token 过期/被撤销时 GNOME 不报错,只重新弹窗(日志 "Cannot parse
restore data, ignoring")。daemon 无 API 探测,唯一信号是用户报告
"又弹窗了"。缓解:操作者点一次,新 token 自动覆盖。

### R6 mutter #3375:EIS 客户端收不到修饰键事件

CapsLock 亮着时 EIS 下打 "A" 会得到 "a"(我们按 keymap 认为无需
Shift)。Shift 已自管理(D10);CapsLock/NumLock 状态感知留到 M3。
notify 传输无此问题(mutter 内部管理修饰键)。

### R7 mutter input loopback(未解决,仅记录)

注入的事件会被 shell 当作真实输入:可能触发全局快捷键、计入空闲
检测。RemoteDesktop 注入与 InputCapture 同时使用有死循环风险
(mutter #4883)。M1 不用 InputCapture;文档提醒用户别开着两个 portal
互相打架。

### R8 安全模型(设计使然,非缺陷)

socket 与 token 均为 0600:**任何同 uid 进程都能注入输入**。
这与 X11 的 XTest 同等级。不要 chmod 放权,不要跨 uid 共享。
README 明示。

### R9 keymap 二进制格式(已规避)

mutter 实测发文本 xkb_keymap;若未来变二进制,xkb.py 会解析失败并
退回 US 表(功能降级不崩)。日志会有 "keymap unusable" 警告。

## 环境/依赖杂项

- PyGObject 弃用警告:`GLib.unix_signal_add_full` → 已用 GLibUnix
  (带回退);`io_add_watch` 必须显式传 priority(新签名)
- dbus-python 构造 DBusException 时错误名**必须**用 `name=` 关键字,
  否则 `get_dbus_name()` 返回 None(tests/fakes.py 有注释)
- `AvailableDeviceTypes` 目前是 7(键盘|指针|触摸)——SelectDevices 传
  8(屏幕流)会被拒 `Unsupported device type`
