# PyWaylandAuto

Wayland 键鼠输入注入工具:daemon + 客户端库 + CLI。

Wayland 协议不允许普通客户端注入全局键鼠事件。PyWaylandAuto 通过
XDG Desktop Portal RemoteDesktop 获得合法的输入注入能力,并以 daemon
长连接缓存授权 restore token,让 GNOME 的 "Allow remote interaction?"
弹窗只出现一次。

> M1 已完成并真机验证(GNOME 50);wlroots/uinput 后端、坐标翻译在规划中。

## 快速开始

```bash
pip install .
pywaylandauto daemon start          # 首次会在桌面弹出授权窗,点一次"允许"
pywaylandauto move 960 540
pywaylandauto click
pywaylandauto type "hello"
pywaylandauto daemon stop
```

详见 docs/smoke-test.md。

## 功能(M1,GNOME 实测)

| 能力 | 支持 |
|---|---|
| 绝对移动 `move X Y` | ✅ EIS 传输(mutter 的 notify 路径不支持绝对移动) |
| 相对移动 / 点击 / 滚轮 | ✅ 双传输 |
| 按键(含组合键)/ 打字 | ✅ EIS 用 mutter 的真实 XKB keymap 做 keysym→keycode |
| 授权弹窗 | 仅首次一次(persist_mode=2 + restore token 缓存) |

## 架构

```
CLI / 你的测试代码
   └── Client (UNIX socket + 换行分隔 JSON)
         └── Daemon (GLib 主循环, 持有 portal 会话)
               └── XDG RemoteDesktop portal
                     ├── 默认: ConnectToEIS → libei 线协议(纯 stdlib 实现)
                     └── 回退/强制: Notify* D-Bus(PYWAYLANDAUTO_TRANSPORT=notify)
```

- 授权 token 缓存在 `$XDG_STATE_HOME/pywaylandauto/portal.token`(0600/0700)
- socket 位于 `$XDG_RUNTIME_DIR/pywaylandauto.sock`(0600)

## 文档

| 文档 | 内容 |
|---|---|
| [docs/design.md](docs/design.md) | 设计方案:架构、关键决策与理由、验收标准 |
| [docs/research.md](docs/research.md) | 调研报告:Wayland 输入注入生态、先驱项目、portal 机制 |
| [docs/spike0.md](docs/spike0.md) | Spike 0 实测报告:portal 会话探测、被推翻的假设、教训 |
| [docs/eis-protocol.md](docs/eis-protocol.md) | EIS 线协议笔记(libei 1.5.0):线格式、opcode 表、帧语义 |
| [docs/architecture.md](docs/architecture.md) | 代码架构:模块、状态机、错误映射、测试策略、不变量 |
| [docs/protocol.md](docs/protocol.md) | socket 协议规格(JSON 帧、方法、错误码) |
| [docs/smoke-test.md](docs/smoke-test.md) | 真机冒烟测试流程 |
| [docs/roadmap.md](docs/roadmap.md) | 路线图:M2/M3 规划与风险清单 |
| [docs/known-issues.md](docs/known-issues.md) | 已知问题与风险(R1–R9 + K1–K4) |

## 安全模型

socket 与 token 文件均为 0600:任何同 uid 进程都能注入输入,这是设计使然
(与 X11 的 XTest 同等级)。不要赋予其他用户访问权。

## 开发

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -v
```
