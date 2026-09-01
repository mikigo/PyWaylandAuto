# Socket 协议规格

daemon 与客户端之间通过 UNIX stream socket 通信,换行分隔的 JSON 帧。

## 传输

- socket 路径:`$XDG_RUNTIME_DIR/pywaylandauto.sock`
  (回退 `/tmp/pywaylandauto-<uid>.sock`),文件权限 0600
- 帧:每行一个 UTF-8 JSON 对象,`\n` 结尾(容忍 `\r\n`)
- 单行上限 1 MiB,超限断连
- M1 无服务端主动推送;每个请求对应一个响应,`id` 回显

## 帧格式

请求(客户端 → daemon):

```json
{"id": 1, "method": "input.click", "params": {"button": "left"}}
```

响应(daemon → 客户端):

```json
{"id": 1, "ok": true, "result": {}}
{"id": 1, "ok": false, "error": {"code": "permission_denied", "message": "..."}}
```

## 方法

| method | params | result |
|---|---|---|
| `ping` | `{}` | `{"version": "0.1.0"}` |
| `status` | `{}` | `{"session": {"state", "devices", "transport", "has_token", "persist_mode", "error"}, "daemon": {"pid", "socket"}, "layout": {"bbox", "monitors"}}` |
| `session.start` | `{}` | `{"state": "starting"}` |
| `input.move_abs` | `{"x": number, "y": number}` | `{}` |
| `input.move_rel` | `{"dx": number, "dy": number}` | `{}` |
| `input.button` | `{"button": "left"\|"right"\|"middle" 或 evdev int, "state": "press"\|"release"}` | `{}` |
| `input.click` | `{"button": "left"}` | `{}` |
| `input.scroll` | `{"dx": int, "dy": int, "discrete": bool}` | `{}` |
| `input.key` | `{"keysym": str, "state": "press"\|"release"\|"tap"}` | `{}` |
| `input.type_text` | `{"text": str}` | `{}` |
| `daemon.stop` | `{}` | `{"stopped": true}` |

## 错误码(稳定,机器可读)

```
invalid_json / invalid_params / method_not_found / internal_error
session_not_started / permission_pending / permission_denied / cancelled
portal_unavailable / portal_failed / backend_error
monitor_layout_unavailable / unsupported_character / already_running
```

## 授权流程约定

- 会话尚未启动时,任何 `input.*` 调用会触发 daemon 自动发起 portal
  会话(桌面上弹出 GNOME 授权窗),并返回 `permission_pending`
- 客户端(CLI)收到 `permission_pending` 后应轮询 `status` 至
  `state == "started"` 再重试一次
- 用户拒绝弹窗 → 后续 `input.*` 返回 `permission_denied`;
  `session.start` 可重新发起授权
