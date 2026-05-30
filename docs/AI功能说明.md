# AI 功能说明

本文档说明群聊 `@AI` 智能回复和基础内容审核功能。

## 功能范围

- 群聊消息以 `@AI` 开头时触发 AI 助手。
- 服务端提取 `@AI` 后面的文本作为问题。
- AI 调用在后台线程池中执行，不阻塞普通聊天消息收发。
- AI 回复以 `AI助手` 身份转发到同一个群聊。
- 私聊和群聊消息都会先经过关键词审核；违规消息会被拦截，不写入数据库，也不广播给其他用户。

## 环境变量

API Key 不能写死在代码中。需要调用真实 AI API 时，可以写入项目根目录的 `.env` 文件，服务端启动时会自动读取。

MiMo 示例：

```text
MIMO_API_KEY=your-api-key
MIMO_BASE_URL=https://api.xiaomimimo.com/v1
MIMO_MODEL=mimo-v2.5-pro
```

OpenAI-compatible 示例：

```text
OPENAI_API_KEY=your-api-key
OPENAI_MODEL=gpt-4o-mini
```

可选配置：

```text
OPENAI_BASE_URL=https://api.openai.com/v1/chat/completions
AI_TIMEOUT_SECONDS=20
AI_ASSISTANT_NAME=AI助手
AI_MAX_COMPLETION_TOKENS=1024
AI_TEMPERATURE=1.0
AI_TOP_P=0.95
AI_WECHAT_BAD_WORDS=违规词1,违规词2,攻击性词汇
```

`OPENAI_BASE_URL` / `MIMO_BASE_URL` 可以填写 `/v1` 根地址，服务端会自动补全 `/chat/completions`。

如果未配置 `OPENAI_API_KEY`、`AI_API_KEY` 或 `MIMO_API_KEY`，服务端会返回本地降级回复，方便开发和测试。

## 群聊触发示例

用户发送：

```text
@AI 请解释一下 TCP 和 UDP 的区别
```

服务端流程：

```text
审核消息
-> 广播用户原始群聊消息
-> 检测 @AI
-> 提交后台 AI 任务
-> 调用 AI API 或本地降级回复
-> 以 AI助手 身份广播回复
```

## 内容审核

当前采用关键词过滤方案，默认关键词在 `server/moderation.py` 中维护，也可以通过 `AI_WECHAT_BAD_WORDS` 扩展。

命中违规词时，服务端向发送者返回：

```json
{
  "type": "moderation_warning",
  "sender": "server",
  "payload": {
    "action": "block",
    "reason": "message contains blocked keywords",
    "message": "消息包含违规内容，已被拦截。",
    "matched_words": ["违规词1"]
  }
}
```

被拦截的消息不会保存到 `messages` 表，也不会转发给私聊或群聊对象。

## 限流和异常处理

- 每个用户在每个群内的 `@AI` 请求有冷却时间，默认 3 秒。
- AI API 超时、网络异常或响应格式异常时，服务端会发送“AI 服务暂时不可用，请稍后再试。”到群聊。
- AI 调用由线程池执行，普通群聊转发不等待 API 返回。

## 测试说明

运行全部测试：

```bash
python -m unittest discover -s tests
```

重点测试：

- `GroupChatTest.test_bad_group_message_is_blocked_with_warning`：验证违规群聊消息被拦截并返回警告。
- `AIFeatureTest.test_group_ai_mention_sends_async_assistant_reply`：验证 `@AI` 群聊触发后，服务端以 `AI助手` 身份异步回复群组。

手工测试：

1. 启动服务端：`python -m server.server --host 127.0.0.1 --port 9000`
2. 启动两个客户端并注册登录。
3. 创建群组并让第二个客户端加入。
4. 在群聊中发送 `@AI 请解释一下 TCP 和 UDP 的区别`。
5. 观察两个客户端都收到用户消息和 `AI助手` 回复。
6. 发送包含 `违规词1` 的消息，观察发送者收到审核警告，其他成员不收到该消息。
