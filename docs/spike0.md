# Spike 0 实测报告:portal 会话与传输探测

> 时间:2026-08-26,本机 GNOME 50.1。方法:最小脚本化 portal 会话 +
> 真机授权弹窗。结论已全部吸收进实现;被推翻的假设已修正设计。

## 1. 目标(5 问)

1. 不调 ConnectToEIS 时,`Notify*` 在 GNOME 上可用吗?(notify 传输可行性)
2. Start 响应里有哪些结果键?restore_token 在不在?
3. PropertiesChanged 的信号接口名与触发情况?
4. ConnectToEIS 的 fd 是什么线协议?(头字节)
5. EIS-mode 门控假设(调过 ConnectToEIS 后 Notify 被拒)成立吗?

## 2. 结果

| # | 问题 | 结果 |
|---|---|---|
| 1 | notify 可用? | ✅ **可用**:零位移 `NotifyPointerMotion({}, 0.0, 0.0)` 成功 |
| 2 | Start 结果 | code 0,keys = `['devices', 'restore_token']`;token 是 36 位 UUID |
| 3 | PropertiesChanged | ❌ **全程未触发**(GNOME 50)→ 状态机以 Response 码 + 流程位置为准 |
| 4 | EIS fd | ✅ 服务器先发 `handshake_version(1)`;16 字节头 `=QII`(obj_id/length/opcode),native 序 |
| 5 | EIS-mode 门控 | ✅ 证实:ConnectToEIS 之后 Notify 报 `org.freedesktop.DBus.Error.Failed: Session is not allowed to call NotifyPointer methods` |

## 3. 被推翻的假设(设计修正)

| 原假设 | 实测 | 修正 |
|---|---|---|
| CreateSession 直接返回会话句柄 | **也走 request/response 流程**:返回的是 request 对象路径;真实会话路径在 Response 的 `results["session_handle"]` 里 | PortalClient 的 create_session 回调式接线 |
| request 对象路径 = `/request/{token}` | 路径含 sender 段:`/org/freedesktop/portal/desktop/request/{SENDER}/{token}`,SENDER = 唯一名去 `:`、`.`→`_`(如 `:1.177` → `1_177`) | `PortalClient.request_path()` 拼接 sender |
| handle_token 可随意取 | 必须是 D-Bus 对象路径元素,仅 `[A-Za-z0-9_]`;`spike0-create` 被拒 `InvalidArgument: Invalid token`(旧版本 portal 会直接崩溃) | `new_handle_token()` = `pywaylandauto_` + hex |
| 会话拒绝后仍可 Close | 拒绝后 portal **销毁会话对象**,Close 报 "Object does not exist" | close 容忍 UnknownMethod |
| Start 结果 devices 是数组 | **单个 dbus.UInt32**(值 3 = 键盘|指针) | 标量归一化 + 回归测试 |

## 4. 授权流实测细节

- 弹窗出现在 SelectDevices→Start 阶段;用户点「允许」→ Start Response
  code 0;点「拒绝」→ code 1 + 会话销毁
- SelectDevices 响应 code 0、结果为空 dict(无 token 时)
- 首次授权的 token 落盘后,新会话带上它即可静默恢复(冒烟 §5 验证)

## 5. 后续发现:notify 绝对移动不可用(EIS 拉回 M1 的导火索)

冒烟阶段 `move 2048 1080` 全部报 `org.freedesktop.DBus.Error.Failed:
Invalid position`(任何坐标)。排查:

1. 尝试 SelectDevices 加屏幕流类型(8)→ portal 直接拒绝
   `InvalidArgument: Unsupported device type: 8`(AvailableDeviceTypes=7
   早已暗示:只有键盘|指针|触摸)
2. mutter 源码核实:绝对移动坐标按 **screen cast stream** 校验,无
   活动 stream 即 "Invalid position"/"No screen cast active"
3. 结论:GNOME 上绝对移动唯一正解是 **EIS**(wdotool/KDE Connect 同款
   选择)→ EIS 传输从 M2 提前进 M1,成为默认传输

## 6. EIS 真机握手(首次)

- 重启 daemon(token 静默恢复,无弹窗)→ `ConnectToEIS` → 握手完成
- mutter 发来 **36345 字节 XKB keymap(文本格式)**,解析成功
- `move 2048 1080` 光标正确跳到屏幕中央 —— EIS 链路全通

## 7. 对实现的教训(已成代码不变量)

1. **先注册信号接收器,再发 D-Bus 调用**(防竞态)
2. **信号回调内捕获一切异常**(dbus-python 对回调异常只打警告,
   状态机会静默卡死)
3. **以 Response 码为真相源**,PropertiesChanged 只能当增强
4. **线协议要对着源码验证**(brei-shared.c 的 4 字节对齐、
   float=4B、fd 走 SCM_RIGHTS 零载荷 —— 与直觉相反处不少)
5. **spike 要覆盖所有操作类型**:零位移相对探测通过 ≠ 绝对移动可用
