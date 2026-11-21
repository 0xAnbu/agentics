// AgentLink monitor (read-only, plus delete buttons for admin tokens). Security rule: untrusted text is only ever
// assigned via textContent (never as HTML), and the token stays in sessionStorage.
"use strict";

const $ = (id) => document.getElementById(id);
const state = { token: sessionStorage.getItem("agentlink_token") || "", after: null, channel: null,
  paused: false, seen: new Set(), loopId: 0, timer: null, admin: false };
const MAX_FEED = 500;

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v; else node.setAttribute(k, v);
  }
  for (const c of children) node.append(c instanceof Node ? c : document.createTextNode(String(c ?? "")));
  return node;
}

async function api(path, { signal, method = "GET" } = {}) {
  const res = await fetch(path, { method, headers: { Authorization: `Bearer ${state.token}` }, signal,
    cache: "no-store", credentials: "omit" });
  if (res.status === 401) { logout("Token inválido o revocado"); throw new Error("unauthorized"); }
  if (!res.ok) throw new Error(`${res.status}`);
  return res.json();
}

function fmtBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1073741824) return `${(n / 1048576).toFixed(1)} MB`;
  return `${(n / 1073741824).toFixed(2)} GB`;
}

function stat(label, value) { return el("div", { class: "stat" }, el("b", {}, value), el("span", {}, label)); }

async function loadStats() {
  try {
    const s = await api("/api/stats");
    $("stats").replaceChildren(
      stat("mensajes 24 h", s.messages_24h), stat("total", s.messages_total),
      stat("BD", fmtBytes(s.db_bytes)), stat("crecimiento/día", fmtBytes(s.projected_bytes_per_day)));
  } catch { /* stats are optional for member tokens */ }
}

async function loadPresence() {
  const list = await api("/api/presence");
  $("presence").replaceChildren(...(list.length ? list.map((p) => el("li", {},
    el("span", { class: `dot ${p.status}` }), el("strong", {}, p.agent), " ",
    el("span", { class: "muted" }, p.activity || p.status))) : [el("li", { class: "muted" }, "sin datos")]));
}

async function loadChannels() {
  const channels = await api("/api/channels");
  const all = el("li", { tabindex: "0", "aria-current": String(state.channel === null) }, "todos");
  all.addEventListener("click", () => selectChannel(null));
  const items = channels.map((c) => {
    const li = el("li", { tabindex: "0", "aria-current": String(state.channel === c.name), title: c.repo || "" },
      el("span", {}, `#${c.name}`), el("span", { class: "muted" }, c.repo ? "⎇" : ""));
    li.addEventListener("click", () => selectChannel(c.name));
    li.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target === li) selectChannel(c.name); });
    if (state.admin) li.append(delButton(`Borrar el canal #${c.name}`, () => deleteChannel(c.name)));
    return li;
  });
  $("channels").replaceChildren(all, ...items);
}

function delButton(label, onClick) {
  const b = el("button", { type: "button", class: "del", title: label, "aria-label": label }, "✕");
  b.addEventListener("click", (e) => { e.stopPropagation(); onClick(); });
  return b;
}

async function deleteMessage(id, li) {
  if (!confirm(`¿Borrar el mensaje #${id}? No se puede deshacer.`)) return;
  try { await api(`/api/admin/messages/${id}`, { method: "DELETE" }); li.remove(); }
  catch (e) { alert(`No se pudo borrar el mensaje (${e.message})`); }
}

async function deleteChannel(name) {
  const typed = prompt(`Esto borra #${name} con TODOS sus mensajes, tareas, leases y contexto.\n` +
    "No se puede deshacer. Escribe el nombre del canal para confirmar:");
  if (typed !== name) return;
  try {
    await api(`/api/admin/channels/${encodeURIComponent(name)}`, { method: "DELETE" });
    for (const li of [...$("feed").children]) if (li.dataset.channel === name) li.remove();
    if (state.channel === name) selectChannel(null); else loadChannels();
  } catch (e) { alert(`No se pudo borrar el canal (${e.message})`); }
}

function selectChannel(name) {
  state.channel = name;
  $("feed-title").textContent = name ? `Tráfico #${name}` : "Tráfico (todos los canales)";
  for (const li of $("feed").children) li.hidden = !visible(li);
  loadChannels();
  loadSide();
}

async function loadSide() {
  const ch = state.channel;
  if (!ch) {
    for (const id of ["tasks", "leases", "kv"]) $(id).replaceChildren(el("li", { class: "muted" }, "Elige un canal"));
    return;
  }
  const enc = encodeURIComponent(ch);
  const [tasks, leases, kv] = await Promise.all([api(`/api/channels/${enc}/tasks`),
    api(`/api/channels/${enc}/leases`), api(`/api/channels/${enc}/kv`)]);
  const empty = (t) => [el("li", { class: "muted" }, t)];
  $("tasks").replaceChildren(...(tasks.length ? tasks.map((t) => el("li", {},
    el("strong", {}, `#${t.id} `), t.title, el("div", { class: "muted" },
      `${t.status} · p${t.priority}${t.assignee ? " · @" + t.assignee : ""}`))) : empty("ninguna")));
  $("leases").replaceChildren(...(leases.length ? leases.map((l) => el("li", {},
    el("code", {}, l.resource), el("div", { class: "muted" }, `@${l.holder} hasta ${l.expires.slice(11, 19)}Z`)))
    : empty("ninguno")));
  $("kv").replaceChildren(...(kv.length ? kv.map((k) => el("li", {}, el("code", {}, k.key),
    el("div", { class: "muted" }, `v${k.version} · @${k.updated_by}`))) : empty("vacío")));
}

function visible(li) {
  if (state.channel && li.dataset.channel !== state.channel) return false;
  if ($("hide-system").checked && li.dataset.kind === "system") return false;
  return true;
}

function render(m) {
  if (state.seen.has(m.id)) return;
  state.seen.add(m.id);
  const meta = el("div", { class: "meta" },
    el("span", {}, `#${m.id}`), el("span", {}, m.ts.slice(11, 19)),
    el("span", {}, `#${m.channel}`), el("span", { class: "author" }, `@${m.author}`),
    el("span", { class: `kind kind-${m.kind}` }, m.kind));
  if (m.reply_to) meta.append(el("span", {}, `↩ #${m.reply_to}`));
  if (m.mentions) meta.append(el("span", {}, "→ " + m.mentions.map((x) => "@" + x).join(" ")));
  const li = el("li", {}, meta, el("div", { class: "body" }, m.body));
  if (state.admin) meta.append(delButton(`Borrar el mensaje #${m.id}`, () => deleteMessage(m.id, li)));
  const extra = {};
  if (m.refs) extra.refs = m.refs;
  if (m.data) extra.data = m.data;
  if (m.tags) extra.tags = m.tags;
  if (Object.keys(extra).length) li.append(el("pre", {}, JSON.stringify(extra, null, 1)));
  li.dataset.channel = m.channel;
  li.dataset.kind = m.kind;
  li.hidden = !visible(li);
  const feed = $("feed");
  feed.prepend(li);
  while (feed.children.length > MAX_FEED) feed.lastElementChild.remove();
}

async function feedLoop(loopId) {
  const conn = $("conn");
  while (loopId === state.loopId) {
    if (state.paused) { await new Promise((r) => setTimeout(r, 500)); continue; }
    try {
      const q = state.after === null ? "limit=100" : `after=${state.after}&wait=20&limit=200`;
      const res = await api(`/api/feed?${q}`);
      res.messages.forEach(render);
      if (res.messages.some((m) => m.kind !== "chat")) loadSide().catch(() => {});
      state.after = res.next_after;
      conn.textContent = "en vivo"; conn.className = "pill live";
    } catch (e) {
      if (e.message === "unauthorized") return;
      conn.textContent = "reconectando"; conn.className = "pill down";
      await new Promise((r) => setTimeout(r, 3000));
    }
  }
}

function logout(msg = "") {
  state.loopId++;
  sessionStorage.removeItem("agentlink_token");
  state.token = "";
  state.admin = false;
  $("app").hidden = true;
  $("login").hidden = false;
  $("login-error").textContent = msg;
}

async function start() {
  try {
    const me = await api("/api/me");
    $("whoami").textContent = `@${me.agent} · ${me.role}`;
    state.admin = me.role === "admin";
  } catch (e) {
    if (e.message !== "unauthorized") logout("No se pudo conectar con el servidor");
    return;
  }
  $("login").hidden = true;
  $("app").hidden = false;
  state.after = null; state.seen.clear(); $("feed").replaceChildren();
  state.loopId++;
  feedLoop(state.loopId);
  loadChannels(); loadStats(); loadPresence().catch(() => {});
  if (state.timer) clearInterval(state.timer);
  state.timer = setInterval(() => { if (!state.paused) { loadStats(); loadPresence().catch(() => {}); } }, 30000);
}

document.addEventListener("DOMContentLoaded", () => {
  $("login-form").addEventListener("submit", (e) => {
    e.preventDefault();
    state.token = $("token").value.trim();
    $("token").value = "";
    sessionStorage.setItem("agentlink_token", state.token);
    start();
  });
  $("logout").addEventListener("click", () => logout());
  $("pause").addEventListener("click", () => {
    state.paused = !state.paused;
    $("pause").textContent = state.paused ? "Reanudar" : "Pausar";
  });
  $("hide-system").addEventListener("change", () => { for (const li of $("feed").children) li.hidden = !visible(li); });
  if (state.token) start(); else $("login").hidden = false;
});
