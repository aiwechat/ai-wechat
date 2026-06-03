const state = {
  socket: null,
  mode: "login",
  username: localStorage.getItem("aiwechat.username") || "",
  connected: false,
  loginConfirmed: false,
  current: null,
  conversations: new Map(),
  groups: new Set(JSON.parse(localStorage.getItem("aiwechat.groups") || "[]")),
  groupNames: new Map(Object.entries(JSON.parse(localStorage.getItem("aiwechat.groupNames") || "{}"))),
  online: new Map(),
  pendingLogin: new Map(),
  heartbeatTimer: null,
  reconnectTimer: null,
  mediaRecorder: null,
  recordedChunks: [],
  pendingAttachment: null,
};
const MAX_ATTACHMENT_BYTES = 4 * 1024 * 1024;

const $ = (id) => document.getElementById(id);

const els = {
  connectionStatus: $("connectionStatus"),
  reconnectBtn: $("reconnectBtn"),
  authPanel: $("authPanel"),
  profilePanel: $("profilePanel"),
  currentUser: $("currentUser"),
  avatarText: $("avatarText"),
  onlineSummary: $("onlineSummary"),
  logoutBtn: $("logoutBtn"),
  authSubmit: $("authSubmit"),
  usernameInput: $("usernameInput"),
  passwordInput: $("passwordInput"),
  conversationList: $("conversationList"),
  groupList: $("groupList"),
  privatePeerInput: $("privatePeerInput"),
  groupNameInput: $("groupNameInput"),
  joinGroupInput: $("joinGroupInput"),
  joinGroupBtn: $("joinGroupBtn"),
  createGroupBtn: $("createGroupBtn"),
  newPrivateBtn: $("newPrivateBtn"),
  chatTitle: $("chatTitle"),
  chatSubtitle: $("chatSubtitle"),
  historyBtn: $("historyBtn"),
  messageList: $("messageList"),
  messageInput: $("messageInput"),
  pendingAttachment: $("pendingAttachment"),
  sendBtn: $("sendBtn"),
  aiHintBtn: $("aiHintBtn"),
  imageBtn: $("imageBtn"),
  audioFileBtn: $("audioFileBtn"),
  recordBtn: $("recordBtn"),
  imageInput: $("imageInput"),
  audioInput: $("audioInput"),
  backBtn: $("backBtn"),
  toast: $("toast"),
};

function requestId() {
  return crypto.randomUUID ? crypto.randomUUID().replaceAll("-", "") : `${Date.now()}${Math.random()}`;
}

function message(type, payload = {}, extra = {}) {
  return {
    version: "1.0",
    type,
    request_id: requestId(),
    timestamp: new Date().toISOString(),
    sender: state.username || null,
    receiver: extra.receiver || null,
    group_id: extra.group_id || null,
    payload,
    meta: extra.meta || {},
  };
}

function connect() {
  if (state.socket && state.socket.readyState === WebSocket.OPEN) return;
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${location.host}/ws`);
  state.socket = socket;
  setConnected(false, "连接中");

  socket.addEventListener("open", () => {
    setConnected(true, "已连接");
    startHeartbeat();
    if (state.username) {
      els.usernameInput.value = state.username;
    }
  });

  socket.addEventListener("message", (event) => {
    const data = JSON.parse(event.data);
    handleIncoming(data);
  });

  socket.addEventListener("close", () => {
    setConnected(false, "连接断开");
    stopHeartbeat();
    state.loginConfirmed = false;
    document.body.classList.remove("chat-open");
    render();
    scheduleReconnect();
  });

  socket.addEventListener("error", () => {
    showToast("连接异常");
  });
}

function send(msg) {
  if (!state.socket || state.socket.readyState !== WebSocket.OPEN) {
    showToast("WebSocket 未连接");
    return null;
  }
  state.socket.send(JSON.stringify(msg));
  return msg.request_id;
}

function startHeartbeat() {
  stopHeartbeat();
  state.heartbeatTimer = setInterval(() => {
    if (state.socket && state.socket.readyState === WebSocket.OPEN) {
      send(message("heartbeat", { seq: Date.now() }));
    }
  }, 20000);
}

function stopHeartbeat() {
  if (state.heartbeatTimer) {
    clearInterval(state.heartbeatTimer);
    state.heartbeatTimer = null;
  }
}

function scheduleReconnect() {
  if (state.reconnectTimer) return;
  state.reconnectTimer = setTimeout(() => {
    state.reconnectTimer = null;
    connect();
  }, 1500);
}

function setConnected(connected, text) {
  state.connected = connected;
  els.connectionStatus.textContent = text;
  render();
}

function handleIncoming(msg) {
  switch (msg.type) {
    case "register":
      showToast(`注册成功：${msg.payload.username}`);
      if (state.pendingRegister) {
        const { username, password } = state.pendingRegister;
        state.pendingRegister = null;
        loginWith(username, password);
      }
      break;
    case "login":
      handleLogin(msg);
      break;
    case "logout":
      state.loginConfirmed = false;
      state.username = "";
      localStorage.removeItem("aiwechat.username");
      showToast("已退出登录");
      break;
    case "private_msg":
      addChatMessage(privateKey(resolvePrivateTarget(msg)), msg);
      break;
    case "group_msg":
      addKnownGroup(msg.group_id, msg.payload.group_name || msg.payload.name);
      addChatMessage(groupKey(msg.group_id), msg);
      break;
    case "create_group":
    case "join_group":
      {
        const groupId = msg.group_id || msg.payload.group_id;
        addKnownGroup(groupId, msg.payload.name);
        openConversation(ensureConversation(groupKey(groupId), "group", String(groupId)));
      }
      showToast(msg.type === "create_group" ? "群组已创建" : "已加入群组");
      break;
    case "leave_group":
      removeKnownGroup(msg.group_id || msg.payload.group_id);
      showToast("已退出群组");
      break;
    case "history_response":
      handleHistory(msg);
      break;
    case "user_status":
      handleStatus(msg);
      break;
    case "moderation_warning":
      addSystemMessage(activeKey(), msg.payload.message || "消息被拦截");
      showToast(msg.payload.message || "消息被拦截");
      break;
    case "heartbeat":
      break;
    case "error":
      handleError(msg);
      break;
    default:
      showToast(`收到 ${msg.type}`);
  }
  render();
}

function handleLogin(msg) {
  const username = state.pendingLogin.get(msg.request_id) || msg.payload.username || msg.receiver;
  state.pendingLogin.delete(msg.request_id);
  state.username = username;
  state.loginConfirmed = true;
  localStorage.setItem("aiwechat.username", username);
  state.online.set(username, "online");
  for (const item of msg.payload.online_users || []) {
    state.online.set(String(item), "online");
    if (String(item) !== username) ensureConversation(privateKey(String(item)), "private", String(item));
  }
  for (const group of msg.payload.groups || []) {
    addKnownGroup(group.group_id, group.name);
  }
  showToast(`已登录：${username}`);
}

function handleError(msg) {
  const pending = state.pendingLogin.get(msg.request_id);
  if (pending) {
    state.pendingLogin.delete(msg.request_id);
    state.loginConfirmed = false;
  }
  showToast(`${msg.payload.error_code || "error"}：${msg.payload.message || ""}`);
}

function handleStatus(msg) {
  const { username, status, statuses } = msg.payload;
  if (username && status) {
    state.online.set(String(username), String(status));
    if (String(username) !== state.username) ensureConversation(privateKey(String(username)), "private", String(username));
  }
  if (Array.isArray(statuses)) {
    for (const row of statuses) {
      if (row.username && row.status) state.online.set(String(row.username), String(row.status));
    }
  }
}

function handleHistory(msg) {
  const rows = Array.isArray(msg.payload.messages) ? msg.payload.messages : [];
  for (const row of rows) {
    const chatType = row.group_id ? "group" : "private";
    const target = chatType === "group" ? row.group_id : row.sender === state.username ? row.receiver : row.sender;
    const key = chatType === "group" ? groupKey(target) : privateKey(target);
    addHistoryRow(key, row);
  }
  showToast(rows.length ? `已加载 ${rows.length} 条历史` : "暂无历史消息");
}

function resolvePrivateTarget(msg) {
  if (msg.sender === state.username) return msg.receiver;
  return msg.sender;
}

function privateKey(username) {
  return `private:${username}`;
}

function groupKey(groupId) {
  return `group:${groupId}`;
}

function activeKey() {
  return state.current ? state.current.key : null;
}

function ensureConversation(key, type, target) {
  if (!key || !target) return null;
  if (!state.conversations.has(key)) {
    state.conversations.set(key, { key, type, target, messages: [] });
  }
  return state.conversations.get(key);
}

function addChatMessage(key, msg) {
  const type = msg.type === "group_msg" ? "group" : "private";
  const target = type === "group" ? msg.group_id : resolvePrivateTarget(msg);
  const conv = ensureConversation(key, type, target);
  if (!conv) return;
  conv.messages.push({
    sender: msg.sender,
    content: msg.payload.content || "",
    attachment: msg.payload.attachment || null,
    timestamp: msg.payload.created_at || msg.timestamp,
    system: false,
    ai: Boolean(msg.payload.ai || msg.meta?.ai_response),
  });
  if (!state.current) state.current = conv;
}

function addHistoryRow(key, row) {
  const conv = ensureConversation(key, row.group_id ? "group" : "private", row.group_id || (row.sender === state.username ? row.receiver : row.sender));
  if (!conv) return;
  const id = row.message_id;
  if (id && conv.messages.some((item) => item.id === id)) return;
  conv.messages.push({
    id,
    sender: row.sender,
    content: row.content || row.payload?.content || "",
    attachment: row.payload?.attachment || null,
    timestamp: row.created_at,
    system: false,
    ai: row.message_type === "ai_response",
  });
}

function addSystemMessage(key, text) {
  const conv = key ? state.conversations.get(key) : null;
  if (!conv) {
    showToast(text);
    return;
  }
  conv.messages.push({ sender: "system", content: text, timestamp: new Date().toISOString(), system: true });
}

function addKnownGroup(groupId, name) {
  if (!groupId) return;
  const normalizedId = String(groupId);
  state.groups.add(normalizedId);
  if (name) {
    state.groupNames.set(normalizedId, String(name));
    localStorage.setItem("aiwechat.groupNames", JSON.stringify(Object.fromEntries(state.groupNames)));
  }
  localStorage.setItem("aiwechat.groups", JSON.stringify([...state.groups]));
  ensureConversation(groupKey(normalizedId), "group", normalizedId);
}

function removeKnownGroup(groupId) {
  state.groups.delete(String(groupId));
  state.groupNames.delete(String(groupId));
  localStorage.setItem("aiwechat.groups", JSON.stringify([...state.groups]));
  localStorage.setItem("aiwechat.groupNames", JSON.stringify(Object.fromEntries(state.groupNames)));
}

function render() {
  els.authPanel.classList.toggle("hidden", state.loginConfirmed);
  els.profilePanel.classList.toggle("hidden", !state.loginConfirmed);
  els.currentUser.textContent = state.username || "未登录";
  els.avatarText.textContent = state.username ? state.username.slice(0, 1).toUpperCase() : "-";
  els.onlineSummary.textContent = `${[...state.online.values()].filter((item) => item === "online").length} 人在线`;
  els.authSubmit.textContent = state.mode === "login" ? "登录" : "注册";

  renderComposerTools();
  renderConversations();
  renderGroups();
  renderMessages();
}

function renderComposerTools() {
  const aiAvailable = state.current?.type === "group";
  els.aiHintBtn.classList.toggle("hidden", !aiAvailable);
}

function renderConversations() {
  els.conversationList.innerHTML = "";
  const conversations = [...state.conversations.values()].sort((a, b) => a.type.localeCompare(b.type));
  if (!conversations.length) {
    const empty = document.createElement("div");
    empty.className = "mini-item";
    empty.textContent = "暂无会话";
    els.conversationList.appendChild(empty);
    return;
  }
  for (const conv of conversations) {
    const btn = document.createElement("button");
    btn.className = `conversation-item ${state.current?.key === conv.key ? "active" : ""}`;
    btn.innerHTML = `<span><strong>${escapeHtml(titleFor(conv))}</strong><small>${escapeHtml(subtitleFor(conv))}</small></span><span>${conv.type === "group" ? "群" : "私"}</span>`;
    btn.addEventListener("click", () => openConversation(conv));
    els.conversationList.appendChild(btn);
  }
}

function renderGroups() {
  els.groupList.innerHTML = "";
  for (const groupId of state.groups) {
    const row = document.createElement("div");
    row.className = "mini-item group-row";
    const openBtn = document.createElement("button");
    openBtn.className = "mini-open";
    openBtn.innerHTML = `<span><strong>${escapeHtml(groupName(groupId))}</strong><small>ID ${escapeHtml(shortId(groupId))}</small></span>`;
    openBtn.addEventListener("click", () => openConversation(ensureConversation(groupKey(groupId), "group", groupId)));
    const copyBtn = document.createElement("button");
    copyBtn.className = "copy-button";
    copyBtn.textContent = "复制";
    copyBtn.addEventListener("click", () => copyText(groupId));
    row.append(openBtn, copyBtn);
    els.groupList.appendChild(row);
  }
}

function renderMessages() {
  const conv = state.current;
  els.chatTitle.textContent = conv ? titleFor(conv) : "选择一个会话";
  els.chatSubtitle.textContent = conv ? subtitleFor(conv) : "私聊、群聊和 @AI 都在这里";
  els.messageList.innerHTML = "";
  if (!conv) return;
  for (const item of conv.messages) {
    const row = document.createElement("div");
    const mine = item.sender === state.username;
    row.className = `message-row ${item.system ? "system" : mine ? "self" : ""}`;
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    const name = item.ai ? "AI助手" : mine ? "我" : item.sender || "未知";
    bubble.innerHTML = item.system
      ? escapeHtml(item.content)
      : `<div class="bubble-meta">${escapeHtml(name)} · ${formatTime(item.timestamp)}</div>${renderMessageBody(item)}`;
    row.appendChild(bubble);
    els.messageList.appendChild(row);
  }
  els.messageList.scrollTop = els.messageList.scrollHeight;
}

function titleFor(conv) {
  return conv.type === "group" ? groupName(conv.target) : conv.target;
}

function subtitleFor(conv) {
  if (conv.type === "group") return `群聊 ID ${shortId(conv.target)} · @AI 可配合图片提问`;
  return state.online.get(conv.target) === "online" ? "在线" : "私聊";
}

function groupName(groupId) {
  return state.groupNames.get(String(groupId)) || `未命名群聊 ${shortId(groupId)}`;
}

function shortId(groupId) {
  const text = String(groupId || "");
  if (text.length <= 10) return text;
  return `${text.slice(0, 6)}…${text.slice(-4)}`;
}

function openConversation(conv) {
  if (!conv) return;
  state.current = conv;
  document.body.classList.add("chat-open");
  render();
}

function submitAuth() {
  const username = els.usernameInput.value.trim();
  const password = els.passwordInput.value;
  if (!username || !password) {
    showToast("请输入用户名和密码");
    return;
  }
  const type = state.mode === "login" ? "login" : "register";
  if (type === "login") {
    loginWith(username, password);
    return;
  }
  const msg = message(type, { username, password });
  state.pendingRegister = { username, password };
  send(msg);
}

function loginWith(username, password) {
  const msg = message("login", { username, password });
  state.pendingLogin.set(msg.request_id, username);
  state.username = username;
  send(msg);
}

function sendCurrentMessage() {
  const content = els.messageInput.value.trim();
  const conv = state.current;
  if (!conv) {
    showToast("请先选择会话");
    return;
  }
  if (!content && !state.pendingAttachment) {
    return;
  }
  const payload = { content };
  if (state.pendingAttachment) {
    payload.attachment = state.pendingAttachment;
  }
  sendChatPayload(payload);
  els.messageInput.value = "";
  clearPendingAttachment();
}

function sendChatPayload(payload) {
  const conv = state.current;
  if (!conv) {
    showToast("请先选择会话");
    return;
  }
  if (conv.type === "group") {
    send(message("group_msg", payload, { group_id: conv.target }));
  } else {
    send(message("private_msg", payload, { receiver: conv.target }));
  }
}

function requestHistory() {
  const conv = state.current;
  if (!conv) return;
  if (conv.type === "group") {
    send(message("history_request", { chat_type: "group", group_id: conv.target, limit: 50 }, { group_id: conv.target }));
  } else {
    send(message("history_request", { chat_type: "private", peer: conv.target, limit: 50 }));
  }
}

function showToast(text) {
  els.toast.textContent = text;
  els.toast.classList.remove("hidden");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => els.toast.classList.add("hidden"), 2600);
}

function renderMessageBody(item) {
  const parts = [];
  if (item.content) {
    parts.push(`<div class="message-text">${escapeHtml(item.content)}</div>`);
  }
  if (item.attachment) {
    parts.push(renderAttachment(item.attachment));
  }
  return parts.join("");
}

function renderAttachment(attachment) {
  const name = escapeHtml(attachment.name || (attachment.kind === "image" ? "图片" : "语音"));
  const data = escapeHtml(attachment.data || "");
  if (attachment.kind === "image") {
    return `<figure class="attachment image-attachment"><img src="${data}" alt="${name}" loading="lazy"><figcaption>${name}</figcaption></figure>`;
  }
  if (attachment.kind === "audio") {
    return `<figure class="attachment audio-attachment"><audio controls src="${data}"></audio><figcaption>${name}</figcaption></figure>`;
  }
  return `<div class="attachment-file">${name}</div>`;
}

async function stageFileAttachment(file, kind) {
  if (!file) return;
  if (!state.current) {
    showToast("请先选择会话");
    return;
  }
  if (file.size > MAX_ATTACHMENT_BYTES) {
    showToast("附件不能超过 4MB");
    return;
  }
  if (!file.type.startsWith(`${kind}/`)) {
    showToast(kind === "image" ? "请选择图片文件" : "请选择音频文件");
    return;
  }
  const data = await readFileAsDataUrl(file);
  state.pendingAttachment = {
    kind,
    mime: file.type,
    name: file.name || (kind === "image" ? "image" : "audio"),
    size: file.size,
    data,
  };
  renderPendingAttachment();
  showToast(kind === "image" ? "图片已放入发送框" : "音频已放入发送框");
}

function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => resolve(String(reader.result || "")));
    reader.addEventListener("error", () => reject(reader.error));
    reader.readAsDataURL(file);
  });
}

async function toggleRecording() {
  if (state.mediaRecorder && state.mediaRecorder.state === "recording") {
    state.mediaRecorder.stop();
    return;
  }
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    showToast("当前浏览器不支持录音");
    return;
  }
  if (!state.current) {
    showToast("请先选择会话");
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const recorder = new MediaRecorder(stream);
    state.recordedChunks = [];
    state.mediaRecorder = recorder;
    recorder.addEventListener("dataavailable", (event) => {
      if (event.data.size > 0) state.recordedChunks.push(event.data);
    });
    recorder.addEventListener("stop", async () => {
      stream.getTracks().forEach((track) => track.stop());
      els.recordBtn.classList.remove("recording");
      const blob = new Blob(state.recordedChunks, { type: recorder.mimeType || "audio/webm" });
      if (blob.size > MAX_ATTACHMENT_BYTES) {
        showToast("语音不能超过 4MB");
        return;
      }
      const file = new File([blob], `voice-${Date.now()}.webm`, { type: blob.type || "audio/webm" });
      await stageFileAttachment(file, "audio");
    });
    recorder.start();
    els.recordBtn.classList.add("recording");
    showToast("正在录音，再点一次发送");
  } catch (error) {
    showToast(`录音失败：${error.message || error}`);
  }
}

function renderPendingAttachment() {
  const attachment = state.pendingAttachment;
  if (!attachment) {
    els.pendingAttachment.classList.add("hidden");
    els.pendingAttachment.innerHTML = "";
    return;
  }
  const title = escapeHtml(attachment.name || (attachment.kind === "image" ? "图片" : "语音"));
  const size = formatBytes(attachment.size || 0);
  const preview = attachment.kind === "image"
    ? `<img src="${escapeHtml(attachment.data)}" alt="${title}">`
    : `<audio controls src="${escapeHtml(attachment.data)}"></audio>`;
  els.pendingAttachment.innerHTML = `
    <div class="pending-preview">${preview}</div>
    <div class="pending-meta">
      <strong>${title}</strong>
      <small>${attachment.kind === "image" ? "图片" : "语音"} · ${size}</small>
    </div>
    <button id="clearAttachmentBtn" class="icon-button" title="移除附件" aria-label="移除附件">×</button>
  `;
  els.pendingAttachment.classList.remove("hidden");
  $("clearAttachmentBtn").addEventListener("click", clearPendingAttachment);
}

function clearPendingAttachment() {
  state.pendingAttachment = null;
  renderPendingAttachment();
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function createPrivateConversation() {
  const peer = els.privatePeerInput.value.trim();
  if (!peer) {
    showToast("请输入对方用户名");
    return;
  }
  if (peer === state.username) {
    showToast("不能和自己私聊");
    return;
  }
  openConversation(ensureConversation(privateKey(peer), "private", peer));
  els.privatePeerInput.value = "";
}

function createGroup() {
  const name = els.groupNameInput.value.trim();
  if (!name) {
    showToast("请输入群名称");
    return;
  }
  send(message("create_group", { name }));
  els.groupNameInput.value = "";
}

function joinGroup() {
  const groupId = els.joinGroupInput.value.trim();
  if (!groupId) {
    showToast("请输入群 ID");
    return;
  }
  send(message("join_group", { group_id: groupId }, { group_id: groupId }));
  els.joinGroupInput.value = "";
}

async function copyText(text) {
  const value = String(text);
  const copied = await writeClipboard(value);
  if (copied) {
    showToast(`已复制群ID:${value}`);
    return;
  }
  showToast(`复制失败，请手动复制群ID:${value}`);
}

async function writeClipboard(text) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Fall back to the legacy path below when the Clipboard API is blocked.
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.top = "-1000px";
  textarea.style.left = "-1000px";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  textarea.setSelectionRange(0, textarea.value.length);
  try {
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    textarea.remove();
  }
}

function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  }[char]));
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    state.mode = tab.dataset.mode;
    document.querySelectorAll(".tab").forEach((item) => item.classList.toggle("active", item === tab));
    render();
  });
});

els.authSubmit.addEventListener("click", submitAuth);
els.logoutBtn.addEventListener("click", () => send(message("logout")));
els.reconnectBtn.addEventListener("click", connect);
els.sendBtn.addEventListener("click", sendCurrentMessage);
els.historyBtn.addEventListener("click", requestHistory);
els.backBtn.addEventListener("click", () => document.body.classList.remove("chat-open"));
els.aiHintBtn.addEventListener("click", () => {
  if (state.current?.type !== "group") return;
  if (!els.messageInput.value.startsWith("@AI")) els.messageInput.value = `@AI ${els.messageInput.value}`;
  els.messageInput.focus();
});
els.imageBtn.addEventListener("click", () => els.imageInput.click());
els.audioFileBtn.addEventListener("click", () => els.audioInput.click());
els.recordBtn.addEventListener("click", toggleRecording);
els.imageInput.addEventListener("change", () => {
  stageFileAttachment(els.imageInput.files?.[0], "image");
  els.imageInput.value = "";
});
els.audioInput.addEventListener("change", () => {
  stageFileAttachment(els.audioInput.files?.[0], "audio");
  els.audioInput.value = "";
});
els.messageInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    sendCurrentMessage();
  }
});
els.newPrivateBtn.addEventListener("click", createPrivateConversation);
els.privatePeerInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") createPrivateConversation();
});
els.createGroupBtn.addEventListener("click", createGroup);
els.groupNameInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") createGroup();
});
els.joinGroupBtn.addEventListener("click", joinGroup);
els.joinGroupInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") joinGroup();
});

for (const groupId of state.groups) {
  ensureConversation(groupKey(groupId), "group", groupId);
}
els.usernameInput.value = state.username;
connect();
render();
