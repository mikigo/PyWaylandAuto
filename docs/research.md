# 调研报告:Wayland 输入注入生态

> 调研时间:2026-08。方法:本机 D-Bus introspect + portal 二进制字符串
> 分析 + libei 1.5.0 源码阅读 + 网络调研。配套 [Spike 0 实测](spike0.md)。

## 1. 问题缘起

X11 时代合成输入很简单:XTest 允许任何客户端制造全局键鼠事件,
xdotool 一行命令点哪打哪。Wayland 出于安全考虑把这个口子焊死了:
协议设计上不允许普通客户端向整个会话注入输入。所有 GUI 自动化工具
到 Wayland 都要重新回答一个问题:**事件从哪进?**

候选路线有四条:

| 路线 | 机制 | 覆盖 | 代价 |
|---|---|---|---|
| **XDG Portal RemoteDesktop**(底层 libei) | 用户显式授权的注入会话 | GNOME 46+、KDE 跟进 | 授权弹窗(CI 里的障碍) |
| wlroots 协议(zwp_virtual_keyboard 等) | compositor 私有协议 | Sway、Hyprland | 只覆盖 wlroots 系 |
| uinput(/dev/uinput) | 内核级虚拟设备 | 所有 compositor(含 X11) | 需要 root |
| xdotool via XWayland | XTest 请求被转发给 RemoteDesktop portal | GNOME | 每次操作弹一次授权窗 |

## 2. 先驱项目

### 2.1 dogtail + gnome-ponytail-daemon(GNOME 专有路线)

- dogtail 在 Wayland 下不自己合成输入,通过 D-Bus 驱动
  `gnome-ponytail-daemon` 守护进程,站在三个 GNOME API 上:
  Screen Cast(录屏)、Remote Desktop(注入)、org.gnome.Shell.Introspect
  (窗口枚举)
- `connectWindow(id)`:窗口局部坐标,mutter 的 RecordWindow 翻译成全局
- `connectMonitor()`:全局坐标直接用
- 键盘永远连 monitor,不连窗口 —— 注释原文:"Always use monitor,
  window will often get closed before final release"(Alt-F4 场景)
- 代价:要开 GNOME Shell **unsafe mode**(为了安全特性先把安全特性
  关掉);Introspect/RecordWindow 是 mutter 专有 API,换 compositor 全废
- 彩蛋:dogtail 的 `release_key` 在 Wayland 分支调用的方法与 `hold_key`
  一模一样(疑似 bug)

### 2.2 wdotool(Rust,多后端)

- 五个后端自动检测:portal/libei 优先,wlroots 协议、KWin 脚本、uinput
  依次兜底
- **在 GNOME 上用 EIS,D-Bus Notify* 作回退**(与本项目实测结论一致)
- 缓存 libei restore token,把授权弹窗次数压到最低;token 存在
  `$XDG_STATE_HOME/wdotool/portal.token`(原子写,dir 0700/file 0600)
  —— 本项目沿用了这个位置与权限约定
- 证明多后端抽象这条路走得通

### 2.3 其他

- **enigo-rs**:库形态,portal 优先
- **JDK-8357584**(2025):给 JavaFX Robot 加 portal 支持
- **KDE Connect**:Wayland 端用 EIS(libei),同样的选择
- **xdotool 2025 跨合成器实验**:结论就是"碎片化"三个字

## 3. Portal 关键机制(源码级确认)

- **persist_mode**(SelectDevices 选项,u32):0=不持久,1=存活期持久,
  2=直到撤销;>2 报 "Invalid persist mode"
- **restore_token**:SelectDevices 带上缓存 token → **Start 响应结果里
  返回新 token(每次会话轮换)** → 必须覆盖保存。GNOME 对过期 token
  的行为是悄悄忽略(重新弹窗),无 API 可探测
- **EIS-mode 门控(关键)**:portal 核心在会话调用过 `ConnectToEIS` 后
  拒绝一切 `Notify*` 调用("Session is not allowed to call Notify*
  methods")。libportal 文档要求 `connect_to_eis()` 必须在 `start()`
  之前调用 —— 但**先 start 后 ConnectToEIS 在 GNOME 50 实测可行**
  (spike0.md §4)
- **授权拒绝**:Start 响应 code 1;portal 随后**销毁会话对象**
  (Session.Close 报 "Object does not exist")

## 4. 已知坑(均已在本项目设计中规避或记录)

| 坑 | 描述 | 本项目对策 |
|---|---|---|
| **input loopback** | mutter 上 RemoteDesktop 注入的事件会被 InputCapture 再捕获一次,两个 portal 同时用可能死循环 | M1 不用 InputCapture;已知问题记录 |
| **libei 不是为短命工具设计** | 设备协商有状态,客户端必须活着;跑一次就退的 CLI 反复触发弹窗 | daemon 架构(D1) |
| **mutter #3375** | Ei 客户端收不到键盘修饰键事件,CapsLock 等状态不可知 | Shift 自管理(D10) |
| **notify 绝对移动** | mutter 校验 stream 坐标,RemoteDesktop portal 不支持屏幕流 | EIS 默认传输(D2) |
| **handle_token 字符集** | 含 `-`/`&`/`/` 的 token 会让 portal 崩溃或拒绝(xdg-desktop-portal #1747/#1549,f585a91 修复) | D4 |
| **libei 1.5 无 keysym 事件** | `ei_text`(keysym/UTF-8)在 libei 1.6 才进协议,mutter 支持仍待合并 | 客户端 keymap 翻译 |
| **GNOME 50 devices 标量** | Start 结果的 devices 是单个 u32 不是数组(违反 spec 字面) | 归一化处理 + 回归测试 |

## 5. 本机环境事实(2026-08)

| 项 | 值 |
|---|---|
| GNOME Shell | 50.1(Ubuntu,Wayland 会话) |
| xdg-desktop-portal / -gnome | 1.21.1 / 50.0 |
| libei / libeis | 1.5.0 |
| RemoteDesktop portal | v2 API(AvailableDeviceTypes=7,NotifyKeyboardKeysym 可用,ConnectToEIS 可用) |
| Python | 3.14.4;dbus-python 1.4.0、PyGObject 3.56.2;无 cffi |
| 显示器 | 单逻辑显示器 4096x2160@1.0(Virtual-1,虚拟机) |

## 6. 参考资料

- [gnome-ponytail-daemon](https://gitlab.gnome.org/ofourdan/gnome-ponytail-daemon)
- [wdotool](https://github.com/cushycush/wdotool) ·
  [wdotool portal_token.rs](https://docs.rs/wdotool-core/0.5.0/src/wdotool_core/portal_token.rs.html) ·
  [wdotool libei.rs](https://docs.rs/wdotool-core/0.5.3/src/wdotool_core/backend/libei.rs.html)
- [xdg-desktop-portal persist_mode commit 33bac33](https://github.com/flatpak/xdg-desktop-portal/commit/33bac335dc8fc2545e4cb11269142f1e4163ed9f)
- [xdg-desktop-portal issue #850 (persist_mode)](https://github.com/flatpak/xdg-desktop-portal/issues/850)
- [xdg-desktop-portal EIS 门控 commit b9693c3](https://github.com/flatpak/xdg-desktop-portal/commit/b9693c38894d97c0104189d1f204e2214ccf3c00)
- [xdg-desktop-portal handle_token 校验 f585a91](https://github.com/whot/xdg-desktop-portal/commit/f585a918ae066a3b1ec9a35a47459bf33583991e) ·
  [issue #1747](https://github.com/flatpak/xdg-desktop-portal/issues/1747) ·
  [issue #1549](https://github.com/flatpak/xdg-desktop-portal/issues/1549)
- [libportal connect_to_eis](https://libportal.org/method.Session.connect_to_eis.html)
- [KDE Connect EIS commit](https://invent.kde.org/network/kdeconnect-kde/-/commit/8b7d5a12056e04a1392ada512b1bc4be303581b7)
- [enigo PR #532 (token)](https://github.com/enigo-rs/enigo/pull/532)
- [SO: restore_token not working](https://stackoverflow.com/questions/77076388/wayland-xdg-remote-desktop-portal-restore-token-not-working-in-python3)
- [OpenJDK wakefield 讨论](https://mail.openjdk.org/pipermail/wakefield-dev/2025-March/000199.html)
- [mutter #3375(修饰键)](https://gitlab.gnome.org/GNOME/mutter/-/work_items/3375) ·
  [mutter input loopback #4883](https://gitlab.gnome.org/GNOME/mutter/-/work_items/4883)
- [libei keysym/text 现状(who-t)](http://who-t.blogspot.com/2026/07/libei-and-keysymtext-events.html)
- 起源文章:《在 Wayland 下点鼠标到底有多难》mikigo.site 2026-08-20
