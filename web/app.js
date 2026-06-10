const state = {
  socket: null,
  mode: "login",
  username: localStorage.getItem("aiwechat.username") || "",
  section: "user",
  railExpanded: localStorage.getItem("aiwechat.railExpanded") === "1",
  theme: document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light",
  connected: false,
  loginConfirmed: false,
  current: null,
  conversations: new Map(),
  groups: new Set(),
  groupNames: new Map(),
  friends: new Set(),
  online: new Map(),
  pendingLogin: new Map(),
  pendingHistory: new Map(),
  heartbeatTimer: null,
  reconnectTimer: null,
  mediaRecorder: null,
  recordedChunks: [],
  pendingAttachment: null,
  pendingUploads: new Map(),
};
const MAX_ATTACHMENT_BYTES = 4 * 1024 * 1024;
const MAX_FILE_BYTES = 50 * 1024 * 1024;
const FILE_CHUNK_BYTES = 64 * 1024;

const $ = (id) => document.getElementById(id);

const els = {
  appShell: document.querySelector(".app-shell"),
  railToggle: $("railToggle"),
  themeToggle: $("themeToggle"),
  themeLabel: $("themeLabel"),
  sectionTitle: $("sectionTitle"),
  statusDot: $("statusDot"),
  onlineUsers: $("onlineUsers"),
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
  composer: $("composer"),
  sendBtn: $("sendBtn"),
  aiHintBtn: $("aiHintBtn"),
  imageBtn: $("imageBtn"),
  audioFileBtn: $("audioFileBtn"),
  fileBtn: $("fileBtn"),
  recordBtn: $("recordBtn"),
  imageInput: $("imageInput"),
  audioInput: $("audioInput"),
  fileInput: $("fileInput"),
  backBtn: $("backBtn"),
  toast: $("toast"),
};

if (!els.fileBtn && els.recordBtn?.parentElement) {
  els.fileBtn = document.createElement("button");
  els.fileBtn.id = "fileBtn";
  els.fileBtn.className = "icon-button";
  els.fileBtn.title = "选择文件";
  els.fileBtn.setAttribute("aria-label", "选择文件");
  els.fileBtn.textContent = "↥";
  els.recordBtn.parentElement.insertBefore(els.fileBtn, els.recordBtn);
}

function requestId() {
  return crypto.randomUUID ? crypto.randomUUID().replaceAll("-", "") : `${Date.now()}${Math.random()}`.replaceAll(".", "");
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
    resetSessionState({ keepUsername: true });
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
  if (els.appShell) els.appShell.classList.toggle("connected", connected);
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
      resetSessionState();
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
    case "file_start":
    case "file_chunk":
    case "file_end":
      handleFileTransferStatus(msg);
      break;
    case "message_recall":
      handleRecall(msg);
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
  state.current = null;
  state.conversations.clear();
  state.groups.clear();
  state.groupNames.clear();
  state.friends.clear();
  state.online.clear();
  state.username = username;
  state.loginConfirmed = true;
  localStorage.setItem("aiwechat.username", username);
  loadUserMemory();
  state.online.set(username, "online");
  for (const item of msg.payload.online_users || []) {
    state.online.set(String(item), "online");
  }
  for (const group of msg.payload.groups || []) {
    addKnownGroup(group.group_id, group.name);
  }
  state.section = "chat";
  showToast(`已登录：${username}`);
}

function handleError(msg) {
  const pending = state.pendingLogin.get(msg.request_id);
  if (pending) {
    state.pendingLogin.delete(msg.request_id);
    state.loginConfirmed = false;
  }
  if (state.pendingHistory.has(msg.request_id)) {
    state.pendingHistory.delete(msg.request_id);
  }
  showToast(`${msg.payload.error_code || "error"}：${msg.payload.message || ""}`);
}

function resetSessionState({ keepUsername = false } = {}) {
  state.loginConfirmed = false;
  if (!keepUsername) state.username = "";
  state.current = null;
  state.conversations.clear();
  state.groups.clear();
  state.groupNames.clear();
  state.friends.clear();
  state.online.clear();
  state.pendingLogin.clear();
  state.pendingHistory.clear();
  clearPendingAttachment();
  if (!keepUsername) localStorage.removeItem("aiwechat.username");
  state.section = "user";
  document.body.classList.remove("chat-open");
}

function handleStatus(msg) {
  const { username, status, statuses } = msg.payload;
  if (username && status) {
    state.online.set(String(username), String(status));
  }
  if (Array.isArray(statuses)) {
    for (const row of statuses) {
      if (row.username && row.status) state.online.set(String(row.username), String(row.status));
    }
  }
}

function handleHistory(msg) {
  const rows = Array.isArray(msg.payload.messages) ? msg.payload.messages : [];
  const key = state.pendingHistory.get(msg.request_id);
  state.pendingHistory.delete(msg.request_id);
  const conv = key ? state.conversations.get(key) : conversationForHistory(msg) || state.current;
  if (conv) {
    conv.messages = rows.map(historyRowToMessage);
    state.current = conv;
  }
  showToast(rows.length ? `已加载 ${rows.length} 条历史` : "暂无历史消息");
}

function conversationForHistory(msg) {
  const payload = msg.payload || {};
  if (payload.chat_type === "group" || payload.group_id || msg.group_id) {
    const groupId = payload.group_id || msg.group_id;
    return groupId ? ensureConversation(groupKey(groupId), "group", String(groupId)) : null;
  }
  const peer = payload.peer || payload.username || payload.receiver || payload.sender;
  return peer ? ensureConversation(privateKey(String(peer)), "private", String(peer)) : null;
}

function historyRowToMessage(row) {
  return {
    id: row.message_id,
    sender: row.sender,
    content: row.content || row.payload?.content || "",
    attachment: row.payload?.attachment || null,
    file: row.payload?.file || null,
    recalled: Boolean(row.recalled_at || row.payload?.recalled),
    recalledAt: row.recalled_at || row.payload?.recalled_at,
    timestamp: row.created_at,
    system: false,
    ai: row.message_type === "ai_response",
  };
}

function handleFileTransferStatus(msg) {
  const fileId = msg.payload.file_id;
  if (!fileId) return;
  const upload = state.pendingUploads.get(fileId);
  if (msg.type === "file_chunk" && upload) {
    upload.offset = msg.payload.offset || upload.offset;
    showToast(`上传中 ${upload.name}: ${formatBytes(upload.offset)} / ${formatBytes(upload.size)}`);
  } else if (msg.type === "file_end") {
    state.pendingUploads.delete(fileId);
    showToast("文件已发送");
  }
}

function handleRecall(msg) {
  const messageId = msg.payload.message_id;
  if (!messageId) return;
  for (const conv of state.conversations.values()) {
    const item = conv.messages.find((candidate) => candidate.id === messageId);
    if (!item) continue;
    item.content = "";
    item.attachment = null;
    item.file = null;
    item.recalled = true;
    item.recalledAt = msg.payload.recalled_at;
    return;
  }
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
  const conv = type === "private" ? addFriend(target) : ensureConversation(key, type, target);
  if (!conv) return;
  conv.messages.push({
    id: msg.payload.message_id || null,
    sender: msg.sender,
    content: msg.payload.content || "",
    attachment: msg.payload.attachment || null,
    file: msg.payload.file || null,
    recalled: Boolean(msg.payload.recalled),
    timestamp: msg.payload.created_at || msg.timestamp,
    system: false,
    ai: Boolean(msg.payload.ai || msg.meta?.ai_response),
  });
  if (!state.current) state.current = conv;
}

function addSystemMessage(key, text) {
  const conv = key ? state.conversations.get(key) : null;
  if (!conv) {
    showToast(text);
    return;
  }
  conv.messages.push({ sender: "system", content: text, timestamp: new Date().toISOString(), system: true });
}

function userKey(suffix) {
  return `aiwechat.u.${state.username}.${suffix}`;
}

function loadUserMemory() {
  if (!state.username) return;
  try {
    const names = JSON.parse(localStorage.getItem(userKey("groupNames")) || "{}");
    for (const [id, name] of Object.entries(names)) {
      if (name) state.groupNames.set(String(id), String(name));
    }
  } catch {
    // Ignore a corrupt group-name cache.
  }
  try {
    const friends = JSON.parse(localStorage.getItem(userKey("friends")) || "[]");
    for (const peer of friends) {
      const name = String(peer);
      if (!name || name === state.username) continue;
      state.friends.add(name);
      ensureConversation(privateKey(name), "private", name);
    }
  } catch {
    // Ignore a corrupt friends cache.
  }
}

function saveGroupNames() {
  if (!state.username) return;
  localStorage.setItem(userKey("groupNames"), JSON.stringify(Object.fromEntries(state.groupNames)));
}

function saveFriends() {
  if (!state.username) return;
  localStorage.setItem(userKey("friends"), JSON.stringify([...state.friends]));
}

function addFriend(peer) {
  const name = String(peer || "").trim();
  if (!name || name === state.username) return null;
  if (!state.friends.has(name)) {
    state.friends.add(name);
    saveFriends();
  }
  return ensureConversation(privateKey(name), "private", name);
}

function addKnownGroup(groupId, name) {
  if (!groupId) return;
  const normalizedId = String(groupId);
  state.groups.add(normalizedId);
  if (name) {
    state.groupNames.set(normalizedId, String(name));
    saveGroupNames();
  }
  ensureConversation(groupKey(normalizedId), "group", normalizedId);
}

function removeKnownGroup(groupId) {
  state.groups.delete(String(groupId));
  state.groupNames.delete(String(groupId));
  saveGroupNames();
}

const SECTION_TITLES = { user: "用户", group: "群组", chat: "私聊" };

function renderShell() {
  if (!els.appShell) return;
  els.appShell.dataset.rail = state.railExpanded ? "expanded" : "collapsed";
  els.appShell.dataset.section = state.section;
  els.sectionTitle.textContent = SECTION_TITLES[state.section] || "";
}

function setSection(section) {
  if (!SECTION_TITLES[section]) return;
  state.section = section;
  renderShell();
}

function toggleRail() {
  state.railExpanded = !state.railExpanded;
  localStorage.setItem("aiwechat.railExpanded", state.railExpanded ? "1" : "0");
  renderShell();
}

function applyTheme(theme) {
  state.theme = theme === "dark" ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", state.theme);
  localStorage.setItem("aiwechat.theme", state.theme);
  if (els.themeLabel) els.themeLabel.textContent = state.theme === "dark" ? "夜间" : "日间";
  // Keep the PWA title-bar / status-bar color in sync with the theme.
  const themeColorMeta = document.querySelector('meta[name="theme-color"]');
  if (themeColorMeta) themeColorMeta.setAttribute("content", state.theme === "dark" ? "#161618" : "#ffffff");
}

function toggleTheme() {
  applyTheme(state.theme === "dark" ? "light" : "dark");
}

function appendEmpty(container, text) {
  const empty = document.createElement("div");
  empty.className = "mini-item";
  empty.textContent = text;
  container.appendChild(empty);
}

function renderOnlineUsers() {
  els.onlineUsers.innerHTML = "";
  if (!state.loginConfirmed) {
    appendEmpty(els.onlineUsers, "登录后查看在线用户");
    return;
  }
  const others = [...state.online.entries()]
    .filter(([name, status]) => status === "online" && name !== state.username)
    .map(([name]) => name)
    .sort((a, b) => a.localeCompare(b));
  if (!others.length) {
    appendEmpty(els.onlineUsers, "暂无其他在线用户");
    return;
  }
  for (const name of others) {
    const btn = document.createElement("button");
    btn.className = "conversation-item";
    btn.innerHTML = `<span><strong>${escapeHtml(name)}</strong><small>在线</small></span><span>聊</span>`;
    btn.addEventListener("click", () => {
      state.section = "chat";
      openConversation(addFriend(name));
    });
    els.onlineUsers.appendChild(btn);
  }
}

function render() {
  renderShell();
  els.authPanel.classList.toggle("hidden", state.loginConfirmed);
  els.profilePanel.classList.toggle("hidden", !state.loginConfirmed);
  els.currentUser.textContent = state.username || "未登录";
  els.avatarText.textContent = state.username ? state.username.slice(0, 1).toUpperCase() : "-";
  els.onlineSummary.textContent = `${[...state.online.values()].filter((item) => item === "online").length} 人在线`;
  els.authSubmit.textContent = state.mode === "login" ? "登录" : "注册";

  renderComposerTools();
  renderConversations();
  renderGroups();
  renderOnlineUsers();
  renderMessages();
}

function renderComposerTools() {
  const aiAvailable = state.current?.type === "group";
  els.aiHintBtn.classList.toggle("hidden", !aiAvailable);
  els.composer.classList.remove("hidden");
  els.historyBtn.classList.toggle("hidden", !state.current);
}

function renderConversations() {
  els.conversationList.innerHTML = "";
  const conversations = state.loginConfirmed
    ? [...state.conversations.values()].filter((conv) => conv.type === "private").sort((a, b) => a.target.localeCompare(b.target))
    : [];
  if (!conversations.length) {
    const empty = document.createElement("div");
    empty.className = "mini-item";
    empty.textContent = "暂无私聊";
    els.conversationList.appendChild(empty);
    return;
  }
  for (const conv of conversations) {
    const btn = document.createElement("button");
    btn.className = `conversation-item ${state.current?.key === conv.key ? "active" : ""}`;
    btn.innerHTML = `<span><strong>${escapeHtml(titleFor(conv))}</strong><small>${escapeHtml(subtitleFor(conv))}</small></span><span>私</span>`;
    btn.addEventListener("click", () => openConversation(conv));
    els.conversationList.appendChild(btn);
  }
}

function renderGroups() {
  els.groupList.innerHTML = "";
  if (!state.loginConfirmed) {
    const empty = document.createElement("div");
    empty.className = "mini-item";
    empty.textContent = "暂无群组";
    els.groupList.appendChild(empty);
    return;
  }
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
  if (!state.groups.size) {
    const empty = document.createElement("div");
    empty.className = "mini-item";
    empty.textContent = "暂无群组";
    els.groupList.appendChild(empty);
  }
}

function renderMessages() {
  const conv = state.current;
  els.chatTitle.textContent = conv ? titleFor(conv) : "选择一个会话";
  els.chatSubtitle.textContent = conv ? subtitleFor(conv) : "私聊、群聊和 @AI 都在这里";
  els.messageList.innerHTML = "";
  if (!conv) return;
  for (const item of conv.messages) {
    renderMessageItem(item);
  }
  els.messageList.scrollTop = els.messageList.scrollHeight;
}

function renderMessageItem(item) {
  const row = document.createElement("div");
  const mine = item.sender === state.username;
  row.className = `message-row ${item.system ? "system" : mine ? "self" : ""}`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  const name = item.ai ? "AI助手" : mine ? "我" : item.sender || "未知";
  bubble.innerHTML = item.system
    ? escapeHtml(item.content)
    : `<div class="bubble-meta">${escapeHtml(name)} · ${formatTime(item.timestamp)}</div>${renderMessageBody(item)}`;
  if (!item.system && mine && item.id && !item.recalled) {
    const recallBtn = document.createElement("button");
    recallBtn.className = "recall-button";
    recallBtn.textContent = "撤回";
    recallBtn.addEventListener("click", () => recallMessage(item.id));
    bubble.appendChild(recallBtn);
  }
  if (!item.system && !mine) {
    row.appendChild(buildMessageAvatar(item, name));
  }
  row.appendChild(bubble);
  els.messageList.appendChild(row);
}

function buildMessageAvatar(item, name) {
  const avatar = document.createElement("span");
  avatar.setAttribute("aria-hidden", "true");
  if (item.ai) {
    avatar.className = "msg-avatar ai";
    avatar.textContent = "AI";
  } else {
    avatar.className = "msg-avatar";
    avatar.textContent = name.slice(0, 1).toUpperCase();
    avatar.style.background = avatarColor(name);
  }
  return avatar;
}

function avatarColor(name) {
  let hash = 0;
  for (const char of String(name)) {
    hash = (hash * 31 + char.codePointAt(0)) % 997;
  }
  // Monochrome palette: vary only the gray lightness per user.
  const light = 28 + (hash % 27);
  return `linear-gradient(135deg, hsl(0, 0%, ${light + 8}%), hsl(0, 0%, ${light}%))`;
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

async function sendSelectedFile(file) {
  const conv = state.current;
  if (!file) return;
  if (!conv) {
    showToast("请先选择会话");
    return;
  }
  if (file.size > MAX_FILE_BYTES) {
    showToast("文件不能超过 50MB");
    return;
  }
  const fileId = requestId();
  const bytes = new Uint8Array(await file.arrayBuffer());
  const sha256 = await sha256Hex(bytes);
  const basePayload = {
    file_id: fileId,
    filename: file.name || "file",
    filesize: file.size,
    mime: file.type || "application/octet-stream",
  };
  if (sha256) basePayload.sha256 = sha256;
  state.pendingUploads.set(fileId, { name: basePayload.filename, size: file.size, offset: 0 });
  if (conv.type === "group") {
    send(message("file_start", { ...basePayload, group_id: conv.target }, { group_id: conv.target }));
  } else {
    send(message("file_start", { ...basePayload, receiver: conv.target }, { receiver: conv.target }));
  }
  for (let offset = 0; offset < bytes.length; offset += FILE_CHUNK_BYTES) {
    const chunk = bytes.slice(offset, offset + FILE_CHUNK_BYTES);
    send(message("file_chunk", {
      file_id: fileId,
      offset,
      data: bytesToBase64(chunk),
    }));
  }
  const endPayload = { file_id: fileId };
  if (sha256) endPayload.sha256 = sha256;
  send(message("file_end", endPayload));
  showToast(`文件上传中：${file.name}`);
}

function recallMessage(messageId) {
  if (!messageId) return;
  send(message("message_recall", { message_id: messageId }));
}

async function sha256Hex(bytes) {
  if (!crypto.subtle) return "";
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function bytesToBase64(bytes) {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode(...bytes.slice(i, i + 0x8000));
  }
  return btoa(binary);
}

function requestHistory() {
  const conv = state.current;
  if (!conv) return;
  clearPendingAttachment();
  if (conv.type === "group") {
    const msg = message("history_request", { chat_type: "group", group_id: conv.target, limit: 50 }, { group_id: conv.target });
    state.pendingHistory.set(msg.request_id, conv.key);
    send(msg);
  } else {
    const msg = message("history_request", { chat_type: "private", peer: conv.target, limit: 50 });
    state.pendingHistory.set(msg.request_id, conv.key);
    send(msg);
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
  if (item.recalled) {
    return `<div class="message-text recalled">消息已撤回</div>`;
  }
  if (item.content) {
    parts.push(item.ai ? renderMarkdown(item.content) : `<div class="message-text">${escapeHtml(item.content)}</div>`);
  }
  if (item.attachment) {
    parts.push(renderAttachment(item.attachment));
  }
  if (item.file) {
    parts.push(renderFileMessage(item.file));
  }
  return parts.join("");
}

function renderMarkdown(content) {
  if (!window.marked || !window.DOMPurify) {
    return `<div class="message-text">${escapeHtml(content)}</div>`;
  }
  const html = window.marked.parse(String(content), { breaks: true, gfm: true });
  return `<div class="message-markdown">${window.DOMPurify.sanitize(html)}</div>`;
}

function renderFileMessage(file) {
  const name = escapeHtml(file.filename || file.name || file.file_id || "file");
  const size = Number.isFinite(file.filesize) ? formatBytes(file.filesize) : "";
  const href = escapeHtml(file.download_url || "#");
  return `<a class="file-message" href="${href}" download>
    <span class="file-icon">↧</span>
    <span><strong>${name}</strong><small>${escapeHtml(size)}</small></span>
  </a>`;
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
  if (!state.loginConfirmed) {
    showToast("请先登录");
    return;
  }
  const peer = els.privatePeerInput.value.trim();
  if (!peer) {
    showToast("请输入对方用户名");
    return;
  }
  if (peer === state.username) {
    showToast("不能和自己私聊");
    return;
  }
  openConversation(addFriend(peer));
  els.privatePeerInput.value = "";
}

function createGroup() {
  if (!state.loginConfirmed) {
    showToast("请先登录");
    return;
  }
  const name = els.groupNameInput.value.trim();
  if (!name) {
    showToast("请输入群名称");
    return;
  }
  send(message("create_group", { name }));
  els.groupNameInput.value = "";
}

function joinGroup() {
  if (!state.loginConfirmed) {
    showToast("请先登录");
    return;
  }
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
  const time = date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (isToday(date)) return time;
  return `${formatDate(date)} ${time}`;
}

function isToday(date) {
  const today = new Date();
  return (
    date.getFullYear() === today.getFullYear()
    && date.getMonth() === today.getMonth()
    && date.getDate() === today.getDate()
  );
}

function formatDate(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}.${month}.${day}`;
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
els.fileBtn?.addEventListener("click", () => els.fileInput.click());
els.recordBtn.addEventListener("click", toggleRecording);
els.imageInput.addEventListener("change", () => {
  stageFileAttachment(els.imageInput.files?.[0], "image");
  els.imageInput.value = "";
});
els.audioInput.addEventListener("change", () => {
  stageFileAttachment(els.audioInput.files?.[0], "audio");
  els.audioInput.value = "";
});
els.fileInput.addEventListener("change", () => {
  sendSelectedFile(els.fileInput.files?.[0]);
  els.fileInput.value = "";
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

document.querySelectorAll(".rail-item").forEach((item) => {
  item.addEventListener("click", () => setSection(item.dataset.section));
});
els.railToggle.addEventListener("click", toggleRail);
els.themeToggle.addEventListener("click", toggleTheme);

applyTheme(state.theme);
els.usernameInput.value = state.username;
connect();
render();

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch((error) => {
      console.debug("service worker registration failed", error);
    });
  });
}
