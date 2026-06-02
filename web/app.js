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
};

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
  sendBtn: $("sendBtn"),
  aiHintBtn: $("aiHintBtn"),
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

  renderConversations();
  renderGroups();
  renderMessages();
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
      : `<div class="bubble-meta">${escapeHtml(name)} · ${formatTime(item.timestamp)}</div>${escapeHtml(item.content)}`;
    row.appendChild(bubble);
    els.messageList.appendChild(row);
  }
  els.messageList.scrollTop = els.messageList.scrollHeight;
}

function titleFor(conv) {
  return conv.type === "group" ? groupName(conv.target) : conv.target;
}

function subtitleFor(conv) {
  if (conv.type === "group") return `群聊 ID ${shortId(conv.target)} · 输入 @AI 可以触发智能助手`;
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
  if (!content || !conv) return;
  if (conv.type === "group") {
    send(message("group_msg", { content }, { group_id: conv.target }));
  } else {
    send(message("private_msg", { content }, { receiver: conv.target }));
  }
  els.messageInput.value = "";
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
  try {
    await navigator.clipboard.writeText(text);
    showToast("群 ID 已复制");
  } catch {
    showToast(`群 ID：${text}`);
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
  if (!els.messageInput.value.startsWith("@AI")) els.messageInput.value = `@AI ${els.messageInput.value}`;
  els.messageInput.focus();
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
