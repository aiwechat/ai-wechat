# Protocol Guide

本文档说明客户端和服务端如何对接通信协议，以及协议的核心设计思路。

## 核心设计

项目底层使用 TCP socket。TCP 只提供可靠、有序的字节流，不保留“哪一段字节是一条完整消息”的边界，所以应用层需要自己定义消息格式。

本项目的应用层消息格式是：

```text
4-byte big-endian body length + UTF-8 JSON body
```

- 前 4 字节表示后面 JSON body 的字节长度。
- JSON body 保存统一的消息对象。
- 这个 4 字节长度前缀不是 TCP header，而是项目自定义的应用层 frame header。
- 这样可以处理 TCP 粘包、拆包、半包和一次接收多条消息的问题。

业务数据统一放在 JSON Envelope 中：

```json
{
  "version": "1.0",
  "type": "private_msg",
  "request_id": "unique-id",
  "timestamp": "2026-05-19T12:00:00Z",
  "sender": "alice",
  "receiver": "bob",
  "group_id": null,
  "payload": {
    "content": "hello"
  },
  "meta": {}
}
```

其中：

- `type` 用来区分业务类型，类似 HTTP 中的路由。
- `payload` 保存具体业务数据，后续功能优先扩展这里。
- `request_id` 用来关联请求、响应和日志。
- `sender`、`receiver`、`group_id` 用来表示发送方、接收方和群聊对象。
- `meta` 只放调试、追踪、客户端版本等附加信息。

## 对接方式

客户端和服务端都应使用 `common/protocol.py` 中的工具函数，不要各自手写 JSON 拼接和解析逻辑。

发送消息：

```python
from common.protocol import MessageType, ProtocolMessage, encode_frame

message = ProtocolMessage(
    type=MessageType.PRIVATE_MSG,
    sender="alice",
    receiver="bob",
    payload={"content": "hello"},
)

sock.sendall(encode_frame(message))
```

接收消息：

```python
from common.protocol import decode_frames

buffer = b""

while True:
    chunk = sock.recv(4096)
    if not chunk:
        break

    buffer += chunk
    messages, buffer = decode_frames(buffer)

    for message in messages:
        handle_message(message)
```

服务端收到消息后，根据 `message.type` 分发给不同处理函数：

```python
from common.protocol import MessageType

def handle_message(message):
    if message.type == MessageType.LOGIN:
        handle_login(message)
    elif message.type == MessageType.PRIVATE_MSG:
        handle_private_message(message)
    elif message.type == MessageType.GROUP_MSG:
        handle_group_message(message)
    elif message.type == MessageType.HEARTBEAT:
        handle_heartbeat(message)
```

## 常用消息类型

| type | 用途 | payload 示例 |
| --- | --- | --- |
| `register` | 注册 | `{"username":"alice","password":"***"}` |
| `login` | 登录 | `{"username":"alice","password":"***"}` |
| `logout` | 退出登录 | `{}` |
| `private_msg` | 私聊 | `{"content":"hello"}` |
| `group_msg` | 群聊 | `{"content":"hello group"}` |
| `create_group` | 创建群组 | `{"name":"network-class"}` |
| `join_group` | 加入群组 | `{"group_id":"g1"}` |
| `leave_group` | 退出群组 | `{"group_id":"g1"}` |
| `heartbeat` | 心跳 | `{"seq":1}` |
| `history_request` | 请求历史消息 | `{"chat_type":"private","peer":"bob","limit":50}` |
| `history_response` | 返回历史消息 | `{"messages":[]}` |
| `file_start` | 文件传输开始 | `{"filename":"test.pdf","filesize":1024,"file_id":"file-1"}` |
| `file_chunk` | 文件分片 | `{"file_id":"file-1","offset":0,"data":"base64..."}` |
| `file_end` | 文件传输结束 | `{"file_id":"file-1","status":"finished"}` |
| `ai_request` | AI 请求 | `{"prompt":"explain TCP"}` |
| `ai_response` | AI 回复 | `{"content":"..."}` |
| `moderation_warning` | 内容审核警告 | `{"reason":"..."}` |
| `error` | 错误响应 | `{"error_code":"auth_failed","message":"登录失败","detail":{}}` |

## 错误格式

错误统一使用 `type = "error"`：

```json
{
  "type": "error",
  "sender": "server",
  "payload": {
    "error_code": "auth_failed",
    "message": "用户名或密码错误",
    "detail": {}
  }
}
```

常用错误码：

- `invalid_json`：JSON 解析失败。
- `invalid_frame`：frame 长度非法。
- `invalid_message_type`：未知消息类型。
- `missing_field`：缺少必要字段。
- `invalid_field`：字段类型或取值非法。
- `auth_failed`：认证失败。
- `not_found`：用户、群组或消息不存在。
- `conflict`：重复注册、重复入群等冲突。
- `server_error`：服务端内部错误。

## 扩展约定

- 新功能优先新增 `MessageType` 或扩展 `payload`。
- 不要随意改名、删除 Envelope 顶层字段。
- 新增消息类型后，需要同步更新 `common/protocol.py` 和本文档。
- 文件传输使用 `file_start -> file_chunk -> file_end`，大文件不要直接放进单个 JSON。
- AI、审核、文件传输等功能应复用 `request_id` 追踪请求链路。
