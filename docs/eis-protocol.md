# EIS 线协议笔记(libei 1.5.0)

> 本文是 `backends/eis.py` / `eis_messages.py` 的协议依据,全部对照
> libei 1.5.0 `proto/protocol.xml`、`src/brei-shared.c`(编解码)、
> `src/libei-device.c`(帧语义)核实。协议源文件可取:
>
> ```
> wget https://gitlab.freedesktop.org/libinput/libei/-/raw/1.5.0/proto/protocol.xml
> ```

## 1. 线格式(brei)

```
[ 16 字节头, 本机字节序 ]
  u64 object_id
  u32 length        # 含头, 16 + payload
  u32 opcode        # 接口内按声明顺序从 0 编号(请求/事件各自独立)

[ 参数, 4 字节单位, 无额外对齐 ]
  u/i/f : 4 字节 (f = C float)
  x/o/n/t : 8 字节 (按 2×u32 读取, 无 8 字节对齐要求)
  s     : u32 长度(含 NUL) + 字节 + 补齐到 4 的倍数
  h     : fd —— 负载中零字节, 通过 SCM_RIGHTS 附带
```

无 fd 的消息直接 `sendall`;带 fd 的消息用 `sendmsg` + SCM_RIGHTS。

## 2. 上下文类型

`ei_handshake.context_type`:1 = receiver,2 = **sender**(本项目的角色)。

## 3. 接口与 opcode(项目只用到 version 1)

### ei_handshake(对象 0)

| 方向 | op | 名称 | 签名 | 说明 |
|---|---|---|---|---|
| ← | 0 | handshake_version | `u` | 服务器**先发**(connect 即发) |
| → | 0 | handshake_version | `u` | 应答 min(server, 1) |
| → | 2 | context_type | `u` | 2 = sender |
| → | 3 | name | `s` | 客户端名字 |
| → | 4 | interface_version | `su` | 各接口我们支持的版本(全部报 1) |
| → | 1 | finish | `` | 结束握手 |
| ← | 2 | connection | `unu` | serial, connection new_id, version |

### ei_connection

| 方向 | op | 名称 | 签名 | 说明 |
|---|---|---|---|---|
| ← | 1 | seat | `nu` | seat new_id, version |
| ← | 3 | ping | `nu` | 服务器要求客户端在 ei_pingpong 对象上应答 |
| ← | 0 | disconnected | `uus` | serial, reason, explanation |
| → | 1 | disconnect | `` | 主动断开 |

### ei_seat

| 方向 | op | 名称 | 签名 | 说明 |
|---|---|---|---|---|
| ← | 1 | name | `s` | |
| ← | 2 | capability | `ts` | mask(u64), interface 名 —— 可多条 |
| ← | 3 | done | `` | 能力宣告结束 |
| → | 1 | bind | `t` | 绑定 mask(各 capability 的 OR) |
| ← | 4 | device | `nu` | 设备 new_id, version |

### ei_device

| 方向 | op | 名称 | 签名 | 说明 |
|---|---|---|---|---|
| ← | 1 | name | `s` | |
| ← | 2 | device_type | `u` | 1=virtual, 2=physical |
| ← | 3 | dimensions | `uu` | 物理尺寸 mm(仅 physical) |
| ← | 4 | region | `uuuuf` | x, y, w, h, scale —— **绝对移动坐标域** |
| ← | 5 | interface | `nsu` | 子对象 new_id, 接口名, version(见 §5) |
| ← | 6 | done | `` | 设备宣告结束 |
| → | 1 | start_emulating | `uu` | last_serial, sequence(>0 递增) |
| → | 2 | stop_emulating | `u` | last_serial |
| → | 3 | frame | `ut` | last_serial, timestamp(CLOCK_MONOTONIC µs) |

### 子接口(1.5 的能力拆分设计)

1.5 起指针能力拆成四个子接口,由 EIS 通过 `ei_device.interface` 事件
**推送**(客户端不创建对象):`ei_pointer`(相对)、`ei_pointer_absolute`
(绝对)、`ei_scroll`(滚动)、`ei_button`(按键),加 `ei_keyboard`。

| 接口 | op | 名称 | 签名 |
|---|---|---|---|
| ei_pointer | 1 | motion_relative | `ff` x, y(逻辑像素) |
| ei_pointer_absolute | 1 | motion_absolute | `ff` x, y(**region 内**,否则被丢弃/断连) |
| ei_scroll | 1 | scroll | `ff` x, y(像素) |
| ei_scroll | 2 | scroll_discrete | `ii` x, y(格) |
| ei_scroll | 3 | scroll_stop | `ii` x, y(终止滚动;离散滚轮可省) |
| ei_button | 1 | button | `uu` button(evdev 码), state(0/1) |
| ei_keyboard | 1 | key | `uu` keycode(evdev), state(0/1) |
| ei_keyboard | ←1 | keymap | `uuh` type(1=xkb), size, fd |
| ei_keyboard | ←3 | modifiers | `uuuuu` serial, depressed, locked, latched, group |
| ei_pingpong | →0 | done | ``(应答 ping) |

## 4. 帧语义(发送侧)

参考 wdotool 与 libei 源码(`ei_device_frame` 在非 EMULATING 状态是
no-op;`ei_device_start_emulating` 要求 RESUMED):

```
start_emulating(last_serial, seq)
  事件...(任意子接口)
frame(last_serial, timestamp_us)
stop_emulating(last_serial)
```

- **每次输入操作一批一帧**;press 与 release 必须在**不同帧**
  (同帧 press+release = 逻辑 noop)
- `last_serial` = 客户端**收到的最后一条服务端事件**携带的 serial
  (发送侧事件在线上不携带 serial;frame 用它作同步点)
- `sequence`:每设备从 1 递增,必须严格大于上次
- `timestamp`:CLOCK_MONOTONIC 微秒

## 5. 设备树构建(客户端视角)

```
finish
  ← connection(serial=1, id=1)          # 首个 serial
  ← seat(id=2) → name, capability×N, done
  → bind(OR 全部 mask)                  # 全量注入器
  ← device(id=3) → name, device_type, dimensions?, region×N,
      interface(ei_pointer → 10), interface(ei_pointer_absolute → 11),
      interface(ei_scroll → 12), interface(ei_button → 13),
      interface(ei_keyboard → 14), done
  ← keymap(在对象 14 上: xkb, size, fd) # 握手完成的最后一块
```

mutter 实测:单一虚拟设备,单一 region = 整个逻辑布局(4096×2160),
keymap 为文本 xkb_keymap(36345 字节)。

## 6. 与直觉相反的点(踩过/防踩)

1. **float 是 4 字节**(brei 测试断言 `buf[4]`),不是 double
2. **fd 参数不占负载字节** —— 只在 SCM_RIGHTS 里
3. **子对象由服务器创建**(1.5 起客户端不再 `device.interface(...)`
   申请 —— 那是旧版本协议)
4. **客户端发的事件没有 serial 字段**;`frame.last_serial` 是"我最后
   收到的服务器 serial"
5. **绝对移动坐标域是 region**(可能不是整个桌面,多设备多 region;
   越界事件被静默丢弃或断连,客户端需自检)
6. **keymap 只有 evdev keycode**,keysym 翻译是客户端的事(libei 1.6
   的 ei_text 才有 keysym,且 mutter 支持未合入)

## 7. 未实现部分(按需再补)

- `ei_scroll.scroll_stop`(平滑滚动终止)
- `ei_connection.sync` / ei_callback(往返同步点)
- pingpong 对象的销毁协议(目前应答 done 即弃)
- ei_device v2 的 region_mapping_id(我们报 version 1,不会收到)
