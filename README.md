# ai-wechat

融合 AI 智能助手的分布式即时聊天系统。目前已完成通信协议、数据库持久层和 TCP 服务端核心，支持多客户端并发连接、注册登录、私聊群聊、在线状态和心跳检测。

## 代码结构

- `common/protocol.py`：定义消息类型、统一消息对象 `ProtocolMessage`、错误消息，以及 TCP 长度前缀帧的编码和解码。
- `server/database.py`：封装 SQLite 数据库初始化和基础 CRUD，包括用户、群组、消息历史和文件传输记录。
- `server/user_manager.py`：在线用户注册中心，管理 `username ↔ TCP 连接` 映射与登录/登出/踢人逻辑。
- `server/group_manager.py`：群组业务封装，创建/加入/退出/列出成员，统一抛出 `ProtocolError`。
- `server/message_router.py`：根据 `MessageType` 分发消息，注册、登录、私聊、群聊、群组操作、历史记录拉取。
- `server/heartbeat.py`：后台心跳扫描线程，定期检测超时连接并踢出。
- `server/server.py`：TCP 服务器入口，accept 循环、每连接一线程处理帧收发，可命令行启动。
- `tests/`：单元测试，覆盖协议编解码、数据库操作、服务端集成流程及 60 客户端并发压力测试。
- `docs/protocol.md`：协议字段和消息类型说明。

## 主要功能

- 使用 `4 字节长度前缀 + UTF-8 JSON` 作为 TCP 应用层消息格式。
- 使用 `dataclass` 和 `Enum` 统一消息结构和消息类型。
- 使用 SQLite 保存用户、群组、聊天记录和文件传输状态。
- 密码使用 PBKDF2 加盐哈希保存。
- 注册/登录、单点登录踢旧连接。
- 一对一私聊（在线转发 + 离线持久化 + 回执）。
- 群组聊天（创建、加入、退出、广播）。
- 在线状态广播（USER_STATUS）。
- 服务端主动心跳检测，超时连接自动清理。
- 支持至少 50 个客户端并发连接。

## 启动服务端

```bash
python3 -m server.server --host 0.0.0.0 --port 9000
```

可选参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--host` | `0.0.0.0` | 监听地址 |
| `--port` | `9000` | 监听端口 |
| `--db` | `data/chat.db` | SQLite 数据库路径 |
| `--heartbeat-timeout` | `60.0` | 心跳超时秒数 |
| `--heartbeat-interval` | `15.0` | 心跳扫描间隔秒数 |
| `-v` / `--verbose` | off | 开启 DEBUG 日志 |

## 运行测试

```bash
python3 -m unittest discover -s tests
```
