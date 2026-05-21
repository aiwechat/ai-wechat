# ai-wechat

目前完成：通信协议封装和 SQLite 数据库访问层，方便后续客户端、服务端和 AI 功能继续对接。

## 代码结构

- `common/protocol.py`：定义消息类型、统一消息对象 `ProtocolMessage`、错误消息，以及 TCP 长度前缀帧的编码和解码。
- `server/database.py`：封装 SQLite 数据库初始化和基础 CRUD，包括用户、群组、消息历史和文件传输记录。
- `tests/`：单元测试，覆盖协议编解码、粘包/拆包、用户登录、群组、消息历史和文件传输状态。
- `docs/protocol.md`：协议字段和消息类型说明。

## 主要功能

- 使用 `4 字节长度前缀 + UTF-8 JSON` 作为 TCP 应用层消息格式。
- 使用 `dataclass` 和 `Enum` 统一消息结构和消息类型。
- 使用 SQLite 保存用户、群组、聊天记录和文件传输状态。
- 密码使用 PBKDF2 加盐哈希保存。

## 运行测试

```bash
python -m unittest discover -s tests
```
