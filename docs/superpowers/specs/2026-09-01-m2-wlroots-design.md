# M2 设计:wlroots 后端 + 合成器探测 + EIS 补全 + 目录结构调整

> 日期:2026-09-01。范围:路线图 M2 中可在本机(Windows 开发机)完成的可开发项;
> 真机验证类条目(KDE 验证等)由用户在自己的 Linux 环境执行。
> 配套文档:[设计总案](design.md) · [路线图](roadmap.md) · [代码架构](architecture.md) ·
> [EIS 协议笔记](eis-protocol.md) · [已知问题](known-issues.md)

## 1. 背景与目标

M1 已交付 portal 后端(GNOME 真机验证)。M2 的目标是**多合成器覆盖**:
把输入注入能力带到 wlroots 系合成器(sway / river / labwc / wayfire),
并顺带补齐 EIS 协议的几个空缺项。同时执行一次仓库结构调整:去掉 `src/`
这一层目录(flat layout)。

### 1.1 范围(本期)

| 项 | 内容 |
|---|---|
| 目录结构 | `src/pywaylandauto/` → 仓库根 `pywaylandauto/`,删除 `src/` 层 |
| wlroots 后端 | `zwlr_virtual_pointer_v1` / `zwlr_virtual_keyboard_v1` 的纯 stdlib 客户端 |
| 后端探测 | auto 探测降级梯 + `PYWAYLANDAUTO_BACKEND` / `--backend` 显式覆盖 |
| Daemon 泛化 | `Daemon(backend=...)`、fd 泵送约定、status 增加 backend 字段 |
| EIS 补全 | scroll_stop、sync/callback、pingpong 协议核对(见 §6) |

### 1.2 非目标(明确不做)

- Hyprland(对 zwlr 虚拟输入协议支持不完整;M3 uinput 兜底覆盖)
- KDE Plasma 真机验证、InputCapture 评估
- wlr-foreign-toplevel 窗口几何(M3 坐标翻译的前置)
- M3 全部:uinput 后端、窗口局部→全局坐标翻译、完整 Unicode 打字

## 2. 目录结构调整

- `src/pywaylandauto/` 整体移到仓库根:`pywaylandauto/`
- 删除:`src/`、`src/pywaylandauto.egg-info/`、各级 `__pycache__/`
- `pyproject.toml`:`[tool.setuptools.packages.find]` 删除 `where = ["src"]`,
  改为 `include = ["pywaylandauto*"]`
- `.gitignore` 的 egg-info 路径从 `src/` 下改为根级
- `docs/architecture.md` §1 文件树同步改路径
- 测试不受影响:flat layout 下 pytest 从仓库根解析 `import pywaylandauto`;
  **现有 181 个用例必须保持全绿**

## 3. 新增模块

```
pywaylandauto/backends/
├── wayland.py    # Wayland 线协议编解码 + 连接(纯 stdlib,同 eis_messages 风格)
├── wlroots.py    # WlrootsSession(Backend):registry 探测、虚拟指针/键盘、seat keymap
└── detect.py     # 后端探测/选择(降级梯 + 显式覆盖)
```

### 3.1 wayland.py — 线协议层

- 线格式:8 字节头,主机字节序 —— `u32 object_id`、`u32 length`
  (length 低 16 位 = 消息长度含头,高 16 位 = opcode);参数按 4 字节
  单位无额外对齐,`s` = u32 长度(含 NUL)+ 字节 + 补齐 4,`h` = fd 经
  SCM_RIGHTS 传输(负载零字节),`o/n` = 4 字节对象 id
- 接口表(照 wayland-protocols 的 XML 核实 opcode 顺序,与 eis_messages
  的"对照源文件核实"纪律一致):
  `wl_display`(get_registry/sync/error/delete_id)、`wl_registry`
  (bind/global/global_remove)、`wl_callback`(done)、`wl_seat`
  (get_keyboard/capabilities/name)、`wl_keyboard`(keymap/enter/leave/key/
  modifiers/repeat_info)、`wl_output`(release)
- zwlr 协议:
  `zwlr_virtual_pointer_manager_v1`(create_virtual_pointer /
  create_virtual_pointer_with_output)、`zwlr_virtual_pointer_v1`
  (motion/motion_absolute/button/axis/frame/axis_source/axis_stop/
  axis_discrete/release)、`zwlr_virtual_keyboard_manager_v1`
  (create_virtual_keyboard)、`zwlr_virtual_keyboard_v1`(keymap/key/
  modifiers/release)
- proxy 模式:对象 id → 接口名映射(同 EisClient 的 `_objects`);
  未知事件跳过并记 debug 日志
- fd 发送:`socket.sendmsg` + `SCM_RIGHTS`(keymap 用);fd 接收走
  `recvmsg` 的 ancdata

### 3.2 wlroots.py — WlrootsSession(Backend)

状态机(无 token、无弹窗、无 D-Bus,比 portal 简单):

```
INIT ──start()──▶ CONNECTING ──registry globals 齐──▶ READY
   ▲                    │
   └────── STOPPED ◀────┴─ 连接失败 / 设备创建失败 / 外部断开
```

流程:

1. `socket.connect($XDG_RUNTIME_DIR/$WAYLAND_DISPLAY)`(WAYLAND_DISPLAY
   缺失 → `BackendError`,明确报错)
2. `get_registry` → 收集 globals:必须同时有
   `zwlr_virtual_pointer_manager_v1` 与 `zwlr_virtual_keyboard_manager_v1`,
   否则 `BackendError`("compositor does not provide zwlr virtual input
   protocols");`wl_display.sync` 往返标记 globals 收集完成
3. 绑 seat → 等 capabilities(sync 往返确认有 keyboard)→ `get_keyboard`
   → 读 `wl_keyboard.keymap`(format=1 xkb_v1 的 fd + size;读出的文本
   交给 xkb.py);seat 无 keyboard 能力或 keymap 事件缺失 → 键盘方法
   报 `BackendError`,指针功能不受影响(宁可失败不静默)
4. `create_virtual_pointer_with_output`(用第一个 `wl_output`;无 output →
   绝对移动不可用,`move_abs` 报 `BackendError`,相对移动/按键/滚轮可用)
5. `create_virtual_keyboard` → 把 seat keymap 原样转交(新 memfd 写文本,
   fd 经 SCM_RIGHTS 发给合成器)→ READY

输入方法(实现 `Backend` ABC):

| 方法 | 行为 |
|---|---|
| move_rel | `zwlr_virtual_pointer.motion(dx, dy)` + `frame()` |
| move_abs | `motion_absolute(x, y)`(全局逻辑坐标,仅在有 output 时)+ `frame()` |
| button | `button(evdev_code, 0/1)` + `frame()` |
| scroll discrete | `axis_discrete(axis, steps)` ×2 + `frame()` |
| scroll smooth | `axis(axis, value)` ×2 + `axis_source(wheel)` + `frame()`(无
axis_source 的应用会按手指滚动处理,参照 wtype) |
| key | keymap 解析 → `zwlr_virtual_keyboard.key(keycode, 0/1)`(无 frame) |
| type_text | 逐字符 press/release,复用 keysym 链 |

键盘链与 EIS 一致:seat keymap 文本 → `xkb.build_resolver` →
keysym→(keycode, level);level 0 直接发 key,level 1 自管理 Shift_L
环绕(虚拟键盘在合成器里有自己的 xkb 状态,修饰键可正常工作,但
CapsLock 状态不可知 —— 与 D10 同理由同对策),level ≥2 →
`UnsupportedCharacterError`。

### 3.3 detect.py — 后端选择

```
PYWAYLANDAUTO_BACKEND(默认 auto)
├─ wlroots → 强制 wlroots(探测失败报 backend_error)
├─ portal  → 强制 portal(portal 缺席自然报 portal_unavailable)
└─ auto:
     WAYLAND_DISPLAY 存在 → 探 registry:
       ├─ zwlr 两个 manager global 齐 → WlrootsSession
       └─ 无/连不上 → PortalSession(portal.start 失败自然报错)
     WAYLAND_DISPLAY 不存在 → PortalSession
```

- 探测连接带超时,失败视为"无 wlroots",不报错
- 顺序与路线图原文(portal 优先)相反,理由:wlroots 协议在 sway 上
  **无弹窗、无 token**,对用户更优;portal 优先只在 GNOME/KDE 有意义,
  而它们根本没有 zwlr 协议 —— 梯子顺序在实际环境中不冲突
- CLI:`daemon start --backend auto|portal|wlroots`(与现有
  `--transport` 模式一致);daemon 未指定时用环境变量/auto

## 4. Daemon 泛化

- `Daemon(portal=...)` 改为 `Daemon(backend=...)`(构造时若未传,
  用 detect.py 选择);测试同步更新
- 分发层与 `_ensure_started` 已几乎后端无关,保持原逻辑
- fd 泵送:`Backend` ABC 增加 `io_fds() -> dict[int, str]` 约定
  (fd → 说明文字);daemon 对每个 fd `GLib.io_add_watch(IO_IN)` 并回调
  `backend.pump(fd)`:
  - portal → EIS fd(现 `_maybe_watch_eis` 逻辑泛化)
  - wlroots → wayland socket fd(keymap 等事件可能随时到达)
- `status` 结果增加 `"backend": <name>`;protocol.md 同步

## 5. 错误与协议变更

- 不新增错误码:wlroots 失败统一 `backend_error`;WAYLAND_DISPLAY 缺失、
  zwlr 协议缺失、无 output 时的 move_abs 均走 `BackendError`
- 现有错误码与 `error_code_for` 映射不变
- 协议扩展仅 status 的 `backend` 字段,向后兼容

## 6. EIS 补全(eis-protocol.md §7,按需做小)

| 项 | 内容 | 备注 |
|---|---|---|
| scroll_stop | 平滑滚动帧后补发 `scroll_stop`(轴标志;签名按 protocol.xml @1.5.0 核实) | 真机冒烟确认 kinetic 滚动终止 |
| sync/callback | 实现 `ei_connection.sync` + `ei_callback.done` 往返;close() 前 flush | |
| pingpong 销毁 | 核对 protocol.xml:v1 若只有 `done` 无销毁 opcode,则清理 `_objects` 条目后记录"无需处理" | |
| ei_device v2 | 我们只报 version 1,不会收到 v2 事件 → 维持现状,文档记录 | |

## 7. 测试策略

沿用"每层一个 fake"的既有策略(architecture.md §8):

| 层 | 手段 | 文件 |
|---|---|---|
| Wayland 编解码 | round-trip 单测 | test_wayland |
| WlrootsSession | FakeWaylandServer(socketpair 对端,真实线协议,脚本化 globals/事件,记录客户端请求) | test_wlroots |
| 后端选择 | 注入 env / fake socket / fake bus | test_detect |
| Daemon 泛化 | 既有 test_daemon_client 更新 backend 参数 | — |
| CLI | `--backend` 标志解析(build_parser 单测) | test_cli(新增) |

FakeWaylandServer 能力:注册指定 globals、应答 sync、发 keymap fd、
记录客户端发出的全部消息(对象 id/opcode/参数)供断言。

真机验证由用户在 Linux 环境执行:smoke-test.md 增加 sway 冒烟清单
(move/move-rel/click/scroll/key/type/绝对移动/keymap 生效)。

## 8. 文档更新

- `docs/architecture.md`:文件树、数据流、状态机、后端选择章节
- `docs/smoke-test.md`:sway 冒烟流程
- `docs/roadmap.md`:M2 完成态标记(真机验证项标注"待真机验证")
- `docs/known-issues.md`:新边界(wlroots keymap 格式、Hyprland 不覆盖、
  无 output 时绝对移动不可用)
- `docs/protocol.md`:status 的 backend 字段

## 9. 验收标准

1. 目录结构调整完成,`pip install -e .` 与 `pytest` 全绿(181 + 新增用例)
2. `pywaylandauto` 包在仓库根可导入,无 `src/` 残留引用
3. WlrootsSession 全部输入方法有 FakeWaylandServer 测试覆盖
4. detect.py 的梯子与覆盖路径有测试
5. EIS 补全项落地 + 测试(scroll_stop 帧顺序、sync 往返)
6. 文档按 §8 更新
7. sway 真机冒烟清单交付(由用户执行,结果回填 smoke-test.md)

## 10. 风险

| 编号 | 风险 | 对策 |
|---|---|---|
| W1 | wlroots 合成器对 zwlr 协议的实现差异(river/labwc 未经真机验证) | 协议按 spec 实现;真机清单覆盖 sway;差异回填 known-issues |
| W2 | 虚拟键盘 keymap 格式(合成器要求 xkb_v1 文本) | 转交 seat 的真实 keymap;非文本格式时退回 US 表并警告(同 R9) |
| W3 | scroll_stop 语义(签名/时机) | 实现时对照 protocol.xml;真机冒烟验证滚动终止 |
| W4 | 无 output 时绝对移动语义差异 | 明确报错(宁可失败不静默,同 R2 原则) |
