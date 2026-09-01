# 路线图

> M1 已完成。M2/M3 为规划,内容基于调研结论([research.md](research.md))。

## M1 — portal 主线 ✅(2026-08 交付)

- portal 后端(notify + EIS 双传输)、daemon、客户端库、CLI
- restore token 缓存、GNOME 50 真机验证
- 已知边界:全局/显示器坐标、Latin-1 打字、GNOME-only

## M2 — 多合成器覆盖 + EIS 深化

| 项 | 内容 | 备注 |
|---|---|---|
| wlroots 后端 | `zwlr_virtual_pointer_v1`(motion/motion_absolute/button/axis/frame)、`zwlr_virtual_keyboard_v1`(key/keymap) | 同族 16 字节头编解码,可复用 eis_messages 的骨架;协议 XML 从 wayland-protocols 取 |
| 合成器探测 | XDG_SESSION_DESKTOP / WAYLAND_DISPLAY → 后端选择;portal → wlroots 降级梯 | |
| wlroots 窗口几何 | `wlr-foreign-toplevel-management` 拿窗口列表/几何 | 坐标翻译的前置 |
| InputCapture 评估 | 被动捕获(portals.conf 白名单可免弹窗)——CI 无弹窗路径候选 | 注意 mutter input loopback 风险 |
| KDE 真机验证 | Plasma 6 的 portal 行为差异(devices 标量这类 GNOME 怪癖未必有) | |
| EIS 补全 | scroll_stop、sync/pingpong 销毁、ei_device v2 | 见 eis-protocol.md §7 |

## M3 — uinput 兜底 + 坐标翻译核心 + Unicode

| 项 | 内容 | 备注 |
|---|---|---|
| uinput 后端 | 纯 struct/ioctl 写 /dev/uinput(UI_DEV_CREATE 等);文档化 udev 规则 | 需 root/组权限;CI+sudo 场景;对 X11 也通吃 |
| 坐标翻译核心 | 每后端配"窗口局部→全局"解析器:GNOME 走 RecordWindow 思路(Shell.Introspect),wlroots 走 toplevel 几何,兜底用 EIS region 事件 | 这是项目长期技术核心(design.md 引言) |
| 完整 Unicode 打字 | 完整 X11 keysym 表、dead keys、AltGr(ISO_Level3_Shift)、CapsLock 状态处理 | 当前 Latin-1 + Shift 双级 |
| libei 1.6 ei_text 跟踪 | keysym/UTF-8 文本事件;等 mutter 合入支持后可选启用 | 见 research.md §4 |

## 持续风险清单(详见 known-issues.md)

- R1 CI 弹窗自动化(每机预授权 + token 作 secret 注入)
- R2 EIS 绝对移动区域语义(多显示器时 region 切分待真机验证)
- R5 GNOME 静默忽略过期 token(无 API 探测,靠观察)
- R7 mutter input loopback(注入事件被当真实输入,可能触发 shell 快捷键)
- R8 安全模型:同 uid 即信任边界(README 明示)

## 未排期 / 观察项

- dogtail 的 `release_key` 疑似 bug(上游 MR 候选)
- 客户端库对多语言的绑定(目前 Python 客户端;协议是纯 JSON,任何语言可接)
- systemd user unit 管理 daemon 生命周期(当前 CLI 托管)
