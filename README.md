# ai-wechat

融合 AI 智能助手的分布式即时聊天系统。目前已完成通信协议、数据库持久层和 TCP 服务端核心，支持多客户端并发连接、注册登录、私聊群聊、在线状态和心跳检测。

## 代码结构

- `common/protocol.py`：定义消息类型、统一消息对象 `ProtocolMessage`、错误消息，以及 TCP 长度前缀帧的编码和解码。
- `server/database.py`：封装 SQLite 数据库初始化和基础 CRUD，包括用户、群组、消息历史和文件传输记录。
- `server/user_manager.py`：在线用户注册中心，管理 `username ↔ TCP 连接` 映射与登录/登出/踢人逻辑。
- `server/group_manager.py`：群组业务封装，创建/加入/退出/列出成员，统一抛出 `ProtocolError`。
- `server/message_router.py`：根据 `MessageType` 分发消息，注册、登录、私聊、群聊、群组操作、历史记录拉取。
- `server/heartbeat.py`：后台心跳扫描线程，定期检测超时连接并踢出。
- `server/relay.py`：共享聊天中继核心，统一持有数据库、在线用户表、群组管理、消息路由和心跳检测。
- `server/relay_service.py`：CLI TCP 网关和浏览器 WebSocket 网关的统一启动入口，让两种客户端共享同一个在线会话表并互相实时聊天。
- `server/server.py`：TCP 服务器入口，accept 循环、每连接一线程处理帧收发，可命令行启动。
- `server/web_server.py`：浏览器 GUI 入口，提供静态页面和 WebSocket 网关，复用服务端消息路由。
- `client/client.py`：CLI 客户端核心，管理 TCP 连接、发送协议消息、处理服务端响应和本地状态。
- `client/receiver.py`：客户端接收线程，持续读取 socket，复用 `decode_frames()` 处理粘包/拆包并分发消息。
- `client/ui.py`：命令行交互层，解析注册、登录、私聊、群聊、群组、历史、状态、心跳等命令。
- `client/local_history.py`：客户端本地会话缓存，只保存运行期间最近消息，服务端数据库仍是历史消息的持久来源。
- `tests/`：单元测试，覆盖协议编解码、数据库操作、服务端集成流程、客户端命令解析、客户端与服务端联调及 60 客户端并发压力测试。
- `docs/protocol.md`：协议字段和消息类型说明。
- `docs/file_transfer_principles.md`：文件传输功能的实现原理，以及和计算机网络知识点的对应关系。

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
- 浏览器 GUI 支持图片和语音消息；群聊中可用 `@AI + 图片` 触发多模态图片分析。
- 统一中继服务支持 CLI 客户端和浏览器 GUI 客户端互相实时私聊、群聊、同步在线状态，并共享同一套历史记录。
- 支持正式文件传输：使用 `file_start -> file_chunk -> file_end` 分片上传，服务端落盘保存并通过受控下载链接访问。
- 支持消息撤回：发送者可撤回自己发送的私聊/群聊消息，历史记录中隐藏原内容。
- 支持发言安全审查：用户消息先正常发送，再由服务端异步调用 OpenAI-compatible API 审查；违规时向发送者警告并强制撤回。
- 客户端本地历史仅用于当前会话显示，重启后以服务端 `history_request` / `history_response` 为准。

## AI API 配置

项目根目录支持 `.env` 配置，服务端启动时会自动读取。`.env` 已在 `.gitignore` 中，不应提交真实 API Key。

MiMo 配置示例：

```text
MIMO_API_KEY=your-api-key
MIMO_BASE_URL=https://api.xiaomimimo.com/v1
MIMO_MODEL=mimo-v2.5
AI_ASSISTANT_NAME=AI助手
AI_TIMEOUT_SECONDS=20
AI_MAX_COMPLETION_TOKENS=1024
AI_TEMPERATURE=1.0
AI_TOP_P=0.95
```

也可以使用 OpenAI-compatible 配置：

```text
OPENAI_API_KEY=your-api-key
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o-mini
```

如果没有配置 API Key，`@AI` 会返回本地降级回复，方便离线开发和测试。

## 安全审查配置

服务端会对每条文字私聊/群聊发言做异步安全审查。消息会先保存并转发；如果 API 判定不合理或不安全，服务端会发送 `moderation_warning` 给发送者，并广播 `message_recall` 强制撤回该消息。

默认复用上面的 OpenAI-compatible / MiMo 配置。也可以单独配置审查接口：

```text
MODERATION_API_KEY=your-api-key
MODERATION_BASE_URL=https://api.openai.com/v1
MODERATION_MODEL=gpt-4o-mini
MODERATION_TIMEOUT_SECONDS=10
```

如果没有可用 API Key，服务端会退回本地关键词审查，关键词可通过 `AI_WECHAT_BAD_WORDS=词1,词2` 扩展。

## 启动服务端

如果需要 CLI 和浏览器 GUI 互相实时聊天，推荐启动统一中继服务：

```bash
python3 -m server.relay_service --tcp-host 0.0.0.0 --tcp-port 9000 --web-host 0.0.0.0 --web-port 8080
```

然后：

- CLI 连接 `127.0.0.1:9000`。
- 浏览器访问 `http://127.0.0.1:8080`。
- 手机和电脑在同一个局域网时，浏览器访问电脑的局域网 IP，例如 `http://192.168.1.10:8080`。

统一中继服务的常用参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--tcp-host` | `0.0.0.0` | CLI TCP 网关监听地址 |
| `--tcp-port` | `9000` | CLI TCP 网关监听端口 |
| `--web-host` | `0.0.0.0` | 浏览器 HTTP/WebSocket 网关监听地址 |
| `--web-port` | `8080` | 浏览器 HTTP/WebSocket 网关监听端口 |
| `--db` | `data/chat.db` | SQLite 数据库路径 |
| `--heartbeat-timeout` | `60.0` | 心跳超时秒数 |
| `--heartbeat-interval` | `15.0` | 心跳扫描间隔秒数 |
| `--recv-timeout` | `30.0` | CLI TCP 连接接收超时秒数 |
| `-v` / `--verbose` | off | 开启 DEBUG 日志 |

只需要 TCP CLI 服务端时，也可以单独启动：

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
| `/send-file private <username> <path>` | 向用户发送文件 |
| `/send-file group <group_id> <path>` | 向群组发送文件 |
| `/recall <message_id>` | 撤回自己发送的消息 |
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

如果需要和 CLI 客户端互通，请使用上面的 `server.relay_service`。只需要浏览器 GUI 时，可以单独启动：

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

GUI 中常用能力：

- 私聊：输入用户名后点击“开始”。
- 群聊：输入群名称创建群，或输入群 ID 加入群。
- 图片：点击输入框旁的 `□`，图片会先进入待发送区，点击“发送”后发出。
- 语音：点击 `♪` 选择音频文件，或点击 `●` 录音，再点击“发送”。
- 文件：点击文件按钮选择普通文件，浏览器会按分片上传，发送完成后聊天中显示下载链接。
- 撤回：自己发送的消息旁会显示“撤回”，点击后双方或群成员会看到消息已撤回。
- 多模态 AI：在群聊输入 `@AI 分析一下这张图`，再选择图片并发送。

## 安装为手机 / 桌面应用（PWA）

浏览器 GUI 同时是一个 PWA（渐进式 Web 应用），可以像原生应用一样安装到手机主屏幕或电脑桌面，带独立窗口、应用图标，静态资源离线缓存。

- **Android（Chrome / Edge）**：打开站点后，点击浏览器菜单中的“安装应用”或“添加到主屏幕”。
- **iOS（Safari）**：点击分享按钮，选择“添加到主屏幕”。
- **桌面（Chrome / Edge）**：地址栏右侧会出现安装图标，点击即可安装为独立窗口应用。

注意事项：

- 除 `localhost` / `127.0.0.1` 外，浏览器要求 **HTTPS** 才会启用 Service Worker 与安装提示。局域网手机调试时，可用反向代理（如 Caddy、nginx）加自签或内网证书，或使用 `chrome://flags` 中的 `unsafely-treat-insecure-origin-as-secure` 仅作本地调试。
- 静态资源由 `web/sw.js` 缓存；修改前端文件后，需将 `sw.js` 中的 `CACHE_VERSION` 加一，客户端才会刷新缓存。
- 应用图标位于 `web/icons/`，可运行 `python3 scripts/generate_icons.py` 重新生成（需要 Pillow 与 numpy）。

## 运行测试

```bash
python3 -m unittest discover -s tests
```
