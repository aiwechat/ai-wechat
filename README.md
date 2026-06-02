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
- `server/web_server.py`：浏览器 GUI 入口，提供静态页面和 WebSocket 网关，复用服务端消息路由。
- `client/client.py`：CLI 客户端核心，管理 TCP 连接、发送协议消息、处理服务端响应和本地状态。
- `client/receiver.py`：客户端接收线程，持续读取 socket，复用 `decode_frames()` 处理粘包/拆包并分发消息。
- `client/ui.py`：命令行交互层，解析注册、登录、私聊、群聊、群组、历史、状态、心跳等命令。
- `client/local_history.py`：客户端本地会话缓存，只保存运行期间最近消息，服务端数据库仍是历史消息的持久来源。
- `tests/`：单元测试，覆盖协议编解码、数据库操作、服务端集成流程、客户端命令解析、客户端与服务端联调及 60 客户端并发压力测试。
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
- 命令行客户端支持连接服务端、注册登录、私聊、群聊、群组管理、历史拉取、在线状态查看和手动心跳。
- 浏览器 GUI 支持电脑和手机访问，可登录注册、私聊群聊、建群加群、拉取历史和触发 `@AI`。
- 客户端本地历史仅用于当前会话显示，重启后以服务端 `history_request` / `history_response` 为准。

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

## 启动客户端

先启动服务端后，再启动客户端：

```bash
python3 -m client.client --host 127.0.0.1 --port 9000
```

可选参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--host` | `127.0.0.1` | 要连接的服务端地址 |
| `--port` | `9000` | 要连接的服务端端口 |
| `--no-connect` | off | 只进入 CLI，不立即连接服务端 |

客户端常用命令：

| 命令 | 说明 |
| --- | --- |
| `/connect [host] [port]` | 连接服务端 |
| `/reconnect` | 断开后重新连接当前服务端 |
| `/register <username> <password>` | 注册用户 |
| `/login <username> <password>` | 登录用户 |
| `/logout` | 退出当前登录用户 |
| `/msg <username> <content>` | 发送私聊消息 |
| `/gmsg <group_id> <content>` | 发送群聊消息 |
| `/chat private <username>` | 设置默认私聊对象，之后可直接输入正文发送 |
| `/chat group <group_id>` | 设置默认群聊对象，之后可直接输入正文发送 |
| `/create-group <name>` | 创建群组 |
| `/join <group_id>` | 加入群组 |
| `/leave <group_id>` | 退出群组 |
| `/history private <username> [limit]` | 拉取私聊历史 |
| `/history group <group_id> [limit]` | 拉取群聊历史 |
| `/groups` | 查看当前客户端已知加入的群组 |
| `/online` | 查看当前客户端已知在线用户 |
| `/status` | 查看连接、登录、当前聊天对象、群组和在线状态 |
| `/heartbeat` | 手动发送心跳并等待服务端回显 |
| `/quit` | 退出客户端 |

## 启动浏览器 GUI

```bash
python3 -m server.web_server --host 0.0.0.0 --port 8080
```

在电脑上访问：

```text
http://127.0.0.1:8080
```

手机和电脑在同一个局域网时，服务端使用 `--host 0.0.0.0` 启动，然后手机访问电脑的局域网 IP，例如：

```text
http://192.168.1.10:8080
```

## 运行测试

```bash
python3 -m unittest discover -s tests
```
