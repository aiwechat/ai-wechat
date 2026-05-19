# ai-wechat

融合 AI 智能助手的分布式即时聊天系统。当前仓库已完成成员 C 负责的第一阶段基础：协议设计、SQLite 数据库访问层、协议说明文档和协作注意事项。

## 小组协作入口

本仓库建议按模块分工开发。所有成员在开始写代码前，先阅读：

- [Agent.md](Agent.md)：项目协作注意事项，尤其是分支、密钥、协议变更规则。
- [docs/协议说明.md](docs/协议说明.md)：客户端和服务端通信必须遵守的协议格式。
- `common/protocol.py`：统一消息对象、JSON 编解码、TCP frame 编解码。
- `server/database.py`：数据库初始化和 CRUD 接口。

### 推荐分支

| 成员 | 分支名 | 负责内容 |
| --- | --- | --- |
| A | `feature/server-core` | 服务端核心、用户管理、消息路由、心跳 |
| B | `feature/client-ui` | 客户端 CLI/UI、消息收发、重连 |
| C | `feature/protocol-db` | 协议、数据库、协议文档 |
| D | `feature/ai-moderation` | AI 助手、内容审核 |
| E | `feature/file-test-docs` | 文件传输、测试、最终文档 |

### 开发流程

```bash
git checkout main
git pull origin main
git checkout -b feature/your-branch
```

日常开发时建议：

```bash
git status
git add .
git commit -m "说明本次完成的具体功能"
git push origin feature/your-branch
```

合并前先同步主分支：

```bash
git checkout main
git pull origin main
git checkout feature/your-branch
git merge main
```

不要直接向 `main` 提交代码。合并时建议至少让一名同学检查代码，尤其是协议字段、数据库表结构、服务端路由和客户端解析逻辑。

## 模块对接约定

### 服务端成员 A

服务端收到 socket 数据后，应通过 `decode_frames()` 解析消息，再根据 `message.type` 分发：

```python
from common.protocol import MessageType, decode_frames, encode_frame

messages, buffer = decode_frames(buffer)
for message in messages:
    if message.type == MessageType.PRIVATE_MSG:
        handle_private_message(message)
```

服务端发送给客户端时，也应使用 `encode_frame(message)`，不要手动拼 JSON。

数据库访问优先使用 `server/database.py` 中的 `ChatDatabase` 方法，避免在 `server.py` 或 `message_router.py` 中散落 SQL。

### 客户端成员 B

客户端发送注册、登录、私聊、群聊、心跳时，都构造 `ProtocolMessage`：

```python
from common.protocol import MessageType, ProtocolMessage, encode_frame

message = ProtocolMessage(
    type=MessageType.LOGIN,
    sender="alice",
    payload={"username": "alice", "password": "123456"},
)
sock.sendall(encode_frame(message))
```

客户端接收线程或协程需要维护一个 `buffer`，每次 `recv()` 后调用 `decode_frames()`，因为 TCP 可能一次收到半条消息，也可能一次收到多条消息。

### AI 与审核成员 D

AI 和内容审核扩展不要改变 Envelope 顶层结构。优先使用已有消息类型：

- `ai_request`
- `ai_response`
- `moderation_warning`
- `error`

AI API Key 只能从环境变量或 `.env` 读取，不要写入代码，也不要提交 `.env`。

### 文件与测试成员 E

文件传输使用三段式协议：

```text
file_start -> file_chunk -> file_end
```

文件内容放在 `payload.data`，建议使用 base64 编码。文件传输状态保存到 `file_transfers` 表。压力测试和联调测试应覆盖粘包、拆包、心跳掉线、50 个客户端并发连接等场景。

## 提交前检查清单

- 已运行测试：`python -m unittest discover -s tests`
- 没有提交 `.env`、数据库文件、日志、缓存、上传文件。
- 如果新增或修改消息类型，已同步更新 `common/protocol.py` 和 `docs/协议说明.md`。
- 如果修改数据库表结构，已更新 `server/database.py` 和 README/文档中的说明。
- 客户端和服务端都通过 `common/protocol.py` 编解码消息，没有各自手写不一致的 JSON 格式。
- 错误响应统一使用 `type = "error"`，payload 中包含 `error_code`、`message`、`detail`。

## 成员 C 技术总结

### 为什么使用 TCP + 长度前缀 + JSON

TCP 负责可靠传输，但 TCP 是字节流协议，不会保留“这一条消息从哪里开始、到哪里结束”。因此本项目在应用层增加 4 字节 big-endian 长度前缀：

```text
4-byte body length + UTF-8 JSON body
```

这样客户端和服务端就能处理粘包、拆包、连续多条消息等网络编程中的常见问题。

JSON 适合作为课程项目的通信格式，因为它可读性好、调试方便，并且后续新增 AI、内容审核、文件传输字段时不需要重写底层协议。

### 为什么使用 dataclass 和 Enum

`common/protocol.py` 使用 `ProtocolMessage` dataclass 管理统一消息结构，并用 `MessageType` Enum 管理消息类型。

好处：

- Python 内部写代码时有统一对象，不需要到处手写 dict。
- 网络传输时统一转换为 JSON，方便和其他模块对接。
- 未知消息类型、非法 JSON、半包等问题可以集中处理。
- 后续扩展协议时只需要新增 `MessageType` 和 payload 约定。

### 协议分层

本项目协议参考经典计算机网络分层思想：

- 传输层：TCP 提供可靠、有序传输。
- 应用层分帧：长度前缀定义消息边界。
- 应用层语义：JSON Envelope 定义 `type`、`sender`、`receiver`、`payload` 等业务字段。
- 持久化层：SQLite 保存用户、群组、消息历史、文件传输记录。

核心 Envelope 字段：

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

详细字段和示例见 [docs/协议说明.md](docs/协议说明.md)。

### 数据库设计

`server/database.py` 封装 SQLite 操作，服务端后续应调用这里的函数，而不是在路由层直接拼 SQL。

当前表：

- `users`：用户信息、密码盐和哈希、在线状态。
- `groups`：群组基本信息。
- `group_members`：群成员关系。
- `messages`：私聊和群聊历史。
- `file_transfers`：文件传输状态。

密码使用标准库 `hashlib.pbkdf2_hmac` 加盐哈希，不明文存储。

### 联调方式

客户端或服务端发送消息：

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

接收并解析消息：

```python
from common.protocol import decode_frames

buffer += sock.recv(4096)
messages, buffer = decode_frames(buffer)
for message in messages:
    print(message.type, message.payload)
```

初始化数据库：

```python
from server.database import init_db

db = init_db("data/chat.db")
db.create_user("alice", "password")
```

## 项目文件

- `common/protocol.py`：统一协议、帧编解码、错误消息。
- `server/database.py`：SQLite 初始化和 CRUD 接口。
- `docs/协议说明.md`：协议字段、消息类型、错误码和扩展规范。
- `Agent.md`：给 Codex 和协作者参考的项目注意事项。
- `tests/`：协议和数据库基础测试。

## 运行测试

```bash
python -m unittest discover -s tests
```
