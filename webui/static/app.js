"use strict";

const $ = (id) => document.getElementById(id);

const CORE = [
  { id: "feedback", title: "执行反馈驱动的自我反省", desc: "以真实 exit_code / 输出为依据纠错，而非模型自述「已完成」" },
  { id: "memory", title: "双层记忆与跨任务经验复用", desc: "对话历史 + 结构化工作记忆 + 持久化经验（JSONL 检索）" },
  { id: "audit", title: "可干预、可恢复、可审计的执行层", desc: "参数校验、路径沙箱、快照回滚、脱敏轨迹，全程可追责" },
];

const DESIGN = [
  { id: "loop", title: "手写主循环 + 显式资源上限", desc: "AgentLimits 约束轮数/工具调用/时长，防失控死循环" },
  { id: "validate", title: "工具参数校验 + 错误反馈限次修复", desc: "JSON 兜底 + Schema 校验 + max_parse_errors 限次" },
  { id: "sandbox", title: "路径沙箱（resolve + relative_to）", desc: "所有文件工具走同一道越界闸门，阻止 ../ 逃逸" },
  { id: "snapshot", title: "写入前快照 + rollback", desc: "自实现版本控制，不依赖 git，可一键恢复" },
  { id: "compress", title: "三层上下文 + 确定性压缩", desc: "超限保留 system + 首条 user + 最近 8 条，纯函数可复现" },
  { id: "workingmem", title: "显式工作记忆", desc: "程序维护 changed_files / current_error / next_step" },
  { id: "cmdsandbox", title: "命令沙箱（黑名单 + 环境变量清洗）", desc: "拒绝 format/shutdown/rm -rf；剥离含密钥的 env" },
  { id: "repeat", title: "重复调用检测", desc: "规范化参数计数，防模型原地打转" },
  { id: "redact", title: "脱敏 trace", desc: "key 名匹配 + sk-/Bearer 值模式，递归脱敏" },
  { id: "compat", title: "OpenAI 兼容 + 可注入 HTTP transport", desc: "任意兼容端点；可注入假 HTTP 便于测试" },
  { id: "finish", title: "finish 作为显式终止信号", desc: "模型必须显式调用 finish 并附测试结果才算成功" },
  { id: "session", title: "跨任务会话状态隔离", desc: "剥离上一任务 finish 交换，避免上下文污染" },
];

let currentRunId = null;
let es = null;
let sessionId = (crypto.randomUUID && crypto.randomUUID()) || ("sess-" + Math.random().toString(36).slice(2));

/* ---------------- utils ---------------- */
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function pretty(v) {
  try { return JSON.stringify(v, null, 2); } catch { return String(v); }
}
function setStatus(msg, cls) {
  const el = $("status");
  el.textContent = msg;
  el.className = "status" + (cls ? " " + cls : "");
}
function setBusy(busy) {
  $("btn-run").disabled = busy;
  $("btn-stop").disabled = !busy;
}

function renderModelText(text) {
  const parts = String(text ?? "").split(/```/);
  let out = "";
  for (let i = 0; i < parts.length; i++) {
    if (i % 2 === 1) out += `<pre><code>${esc(parts[i])}</code></pre>`;
    else out += esc(parts[i]).replace(/\n/g, "<br>");
  }
  return out;
}

/* ---------------- innovation panel ---------------- */
function renderInnovations() {
  const panel = $("innovation-panel");
  let html = `<div class="innov-group-title">三项核心创新</div>`;
  html += CORE.map((i) => innovItem(i, true)).join("");
  html += `<div class="innov-group-title" style="margin-top:6px">12 条设计点</div>`;
  html += DESIGN.map((i) => innovItem(i, false)).join("");
  panel.innerHTML = html;
}
function innovItem(i, core) {
  return `<div class="innov${core ? " core" : ""}" id="innov-${i.id}" data-id="${i.id}">
    <div class="innov-title">${esc(i.title)}</div>
    <div class="innov-desc">${esc(i.desc)}</div>
    <div class="innov-note"></div>
  </div>`;
}

const highlighted = new Map(); // id -> note text
let flashTimer = null;
function flashInnovations(ids, note) {
  for (const id of ids) highlighted.set(id, note || "");
  refreshInnovations();
  clearTimeout(flashTimer);
  flashTimer = setTimeout(() => { highlighted.clear(); refreshInnovations(); }, 1800);
}
function refreshInnovations() {
  for (const i of [...CORE, ...DESIGN]) {
    const el = document.getElementById("innov-" + i.id);
    if (!el) continue;
    const active = highlighted.has(i.id);
    el.classList.toggle("active", active);
    const noteEl = el.querySelector(".innov-note");
    noteEl.textContent = active ? (highlighted.get(i.id) || "刚才被触发") : "";
  }
}

/* ---------------- event -> innovation mapping ---------------- */
function highlightFor(type, data) {
  switch (type) {
    case "run_started": return ["feedback", "memory", "audit"];
    case "memory_prepared": {
      const h = ["workingmem"];
      if (data.compressed) h.push("compress");
      return h;
    }
    case "model_response": {
      const h = [];
      if ((data.tool_calls || []).some((c) => c.name === "finish")) h.push("finish");
      return h;
    }
    case "no_tool_recovery": return ["finish"];
    case "tool_started": return data.validation === "passed" ? ["validate"] : ["validate"];
    case "tool_finished": {
      const md = data.metadata || {};
      const h = [];
      if (data.tool === "run_command" || md.exit_code !== undefined) h.push("feedback");
      if (md.rollback_available || md.rolled_back) h.push("snapshot");
      if (!data.ok) h.push("validate");
      return h;
    }
    case "run_finished": return data.stop_reason === "finish" ? ["finish"] : [];
    default: return [];
  }
}

/* ---------------- event rendering (tabbed: one task = one panel) ---------------- */
let turns = [];
let activeTurnId = null;
let turnSeq = 0;

function renderEvent(type, data) {
  const tl = $("timeline");
  if (tl.querySelector(".placeholder")) tl.innerHTML = "";
  const ids = highlightFor(type, data);
  const note = innovationNote(type, data);
  if (ids.length) flashInnovations(ids, note);

  if (type === "run_started") {
    // A new user input opens a new tab (page), labelled by the task name.
    const turn = createTurn(data.task);
    activateTurn(turn.id);
    turn.panel.appendChild(makeCard(type, data, note));
    scrollToLatest();
    return;
  }

  const card = makeCard(type, data, note);
  const turn = turns.find((t) => t.id === activeTurnId);
  if (turn) turn.panel.appendChild(card);
  else tl.appendChild(card);
  scrollToLatest();
}

function createTurn(task) {
  turnSeq += 1;
  const id = "turn-" + turnSeq;
  const title = String(task || "").trim() || ("任务 " + turnSeq);

  const panel = document.createElement("div");
  panel.className = "turn-panel";
  panel.dataset.turnId = id;
  $("timeline").appendChild(panel);

  const tab = document.createElement("button");
  tab.type = "button";
  tab.className = "tab";
  tab.dataset.turnId = id;
  const label = document.createElement("span");
  label.className = "tab-label";
  label.textContent = title;
  label.title = title;
  tab.appendChild(label);
  tab.addEventListener("click", () => activateTurn(id));
  $("tabs").appendChild(tab);

  const turn = { id, task, panel, tab };
  turns.push(turn);
  return turn;
}

function activateTurn(id) {
  activeTurnId = id;
  for (const t of turns) {
    const on = t.id === id;
    t.panel.style.display = on ? "" : "none";
    t.tab.classList.toggle("active", on);
  }
}

function resetTurns() {
  turns = [];
  activeTurnId = null;
  turnSeq = 0;
  $("tabs").innerHTML = "";
}

function makeCard(type, data, note) {
  const card = document.createElement("div");
  card.className = "tcard";
  card.innerHTML = renderCard(type, data, note);
  return card;
}

function scrollToLatest() {
  window.scrollTo(0, document.documentElement.scrollHeight);
}

function innovationNote(type, data) {
  switch (type) {
    case "tool_finished":
      if (data.tool === "run_command") return `run_command 返回 exit_code=${data.metadata?.exit_code ?? "?"}，作为下一轮反省依据`;
      if (data.metadata?.rollback_available) return "写入前已保存快照，可 rollback";
      if (!data.ok) return "工具失败，错误将回填给模型修正";
      return "";
    case "memory_prepared": return data.compressed ? "上下文超限，执行确定性压缩" : "程序维护结构化工作记忆";
    case "tool_started": return "执行前统一校验参数";
    case "run_started": return "任务开始，三项核心创新全程生效";
    case "run_finished": return data.stop_reason === "finish" ? "模型显式调用 finish 结束任务" : "";
    default: return "";
  }
}

function roundBadge(data) {
  return data.round ? `<span class="round">第 ${data.round} 轮</span>` : "";
}

function renderCard(type, data, note) {
  let html = "";
  switch (type) {
    case "run_started":
      html = `<div class="card-head"><span class="tag tag-start">任务开始</span></div>`;
      if (data.prior_lessons) html += `<div class="muted">🔍 召回 ${data.prior_lessons} 条相关历史经验</div>`;
      if (data.limits) html += `<div class="muted">资源上限：${esc(pretty(data.limits))}</div>`;
      if (data.innovations && data.innovations.length)
        html += `<div class="chips">${data.innovations.map((i) => `<span class="chip">${esc(i)}</span>`).join("")}</div>`;
      break;

    case "round_started":
      return `<div class="round-divider">第 ${data.round} 轮 · 调用模型</div>`;

    case "memory_prepared": {
      const mem = data.memory || {};
      html = `<div class="card-head">${roundBadge(data)}<span class="tag tag-mem">上下文 / 记忆</span></div>
        <div class="mem-grid">
          <div>原始 ${data.raw_messages ?? "?"} 条 · ${data.raw_chars ?? "?"} 字符</div>
          <div>发送 ${data.sent_messages ?? "?"} 条 · ${data.sent_chars ?? "?"} 字符</div>
          <div>状态 <b>${data.compressed ? "已压缩" : "未压缩"}</b></div>
          <div>已改文件：${(mem.changed_files || []).join(", ") || "无"}</div>
          <div>测试观察 ${(mem.tests || []).length} 条</div>
          <div>当前错误：${mem.current_error ? esc(JSON.stringify(mem.current_error)) : "无"}</div>
        </div>`;
      break;
    }

    case "model_response":
      html = `<div class="card-head">${roundBadge(data)}<span class="tag tag-model">模型回复</span></div>`;
      if (data.content) html += `<div class="model-reply">${renderModelText(data.content)}</div>`;
      if (data.tool_calls && data.tool_calls.length) {
        html += `<div class="toolcalls">`;
        for (const c of data.tool_calls) {
          const args = c.arguments ?? c.arguments_json ?? {};
          html += `<div class="toolcall"><span class="tc-name">⚙ ${esc(c.name)}</span><pre class="arg">${esc(pretty(args))}</pre></div>`;
        }
        html += `</div>`;
      }
      break;

    case "model_error":
      html = `<div class="card-head">${roundBadge(data)}<span class="tag tag-fail">模型错误</span></div>
        <div class="err-text">${esc(data.error)}</div>`;
      break;

    case "no_tool_recovery":
      html = `<div class="card-head">${roundBadge(data)}<span class="tag tag-warn">执行提醒</span></div>
        <div>模型只分析未调用工具，已追加执行提醒（第 ${data.attempt} 次）</div>`;
      break;

    case "tool_started":
      html = `<div class="card-head">${roundBadge(data)}<span class="tag tag-tool">工具调用</span>
        <span class="val ${data.validation === "passed" ? "ok" : "bad"}">${data.validation === "passed" ? "✓ 参数校验通过" : "✗ 参数校验失败"}</span></div>
        <div class="tool-intent"><b>${esc(data.tool)}</b><pre class="arg">${esc(pretty(data.arguments))}</pre></div>`;
      break;

    case "tool_finished": {
      const md = data.metadata || {};
      html = `<div class="card-head">${roundBadge(data)}
        <span class="tag ${data.ok ? "tag-ok" : "tag-fail"}">${data.ok ? "成功" : "失败"}</span>
        <span class="tag tag-tool">${esc(data.tool)}</span>
        ${md.duration_ms != null ? `<span class="dur">${md.duration_ms} ms</span>` : ""}</div>`;
      if (md.exit_code !== undefined) html += `<div class="meta">exit_code = <b>${md.exit_code}</b></div>`;
      if (md.rollback_available) html += `<div class="meta">🔒 已存快照，可 rollback（${esc(md.revision || "")}）</div>`;
      if (md.rolled_back) html += `<div class="meta">↩ 已回滚到修改前</div>`;
      const out = data.output || data.error || "";
      if (out) html += `<pre class="tool-out ${data.ok ? "" : "err-out"}">${esc(String(out))}</pre>`;
      if (data.memory) {
        const m = data.memory;
        html += `<div class="meta">已改文件：${(m.changed_files || []).join(", ") || "无"} · 测试 ${(m.tests || []).length} 条 · 错误：${m.current_error ? esc(JSON.stringify(m.current_error)) : "无"}</div>`;
      }
      break;
    }

    case "run_finished":
      html = `<div class="card-head">
        <span class="tag ${data.ok ? "tag-ok" : "tag-fail"}">${data.ok ? "✓ 完成" : "✗ 停止"}</span>
        <span class="tag tag-stop">${esc(data.stop_reason)}</span></div>
        <div class="summary">${esc(data.summary)}</div>
        <div class="meta">轮数 ${data.rounds} · 工具调用 ${data.tool_calls}</div>`;
      break;

    default:
      html = `<div class="card-head"><span class="tag tag-stop">${esc(type)}</span></div><pre class="arg">${esc(pretty(data))}</pre>`;
  }

  if (note && type !== "run_started")
    html += `<div class="inno-badge">◈ 创新点：${esc(note)}</div>`;
  return html;
}

/* ---------------- live run ---------------- */
async function startRun() {
  const task = $("task").value.trim();
  if (!task) { setStatus("请先输入任务", "err"); return; }
  const body = {
    task,
    workdir: $("workdir").value.trim() || ".",
    mode: $("mode").value,
    trace: $("trace").checked,
    session_id: sessionId,
    api_key: $("api-key").value.trim(),
    base_url: $("base-url").value.trim(),
    model: $("model").value.trim(),
  };
  setBusy(true);
  setStatus("正在启动…");
  try {
    const res = await fetch("/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const data = await res.json();
    if (!res.ok || data.error) { setStatus(data.error || "启动失败", "err"); setBusy(false); return; }
    currentRunId = data.run_id;
    openStream(data.run_id);
  } catch (e) { setStatus("请求失败：" + e, "err"); setBusy(false); }
}

function openStream(runId) {
  if (es) es.close();
  es = new EventSource("/stream?run_id=" + encodeURIComponent(runId));
  es.onmessage = (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    handleLive(msg);
  };
  es.onerror = () => { /* done/error events are handled in onmessage */ };
}

function handleLive(msg) {
  const type = msg.type;
  const data = msg.data || {};
  if (type === "connected") return;
  if (type === "__done__") { finishRun(data); if (es) es.close(); return; }
  if (type === "__error__") { setStatus(data.error || "运行出错", "err"); setBusy(false); if (es) es.close(); return; }
  renderEvent(type, data);
}

function finishRun(data) {
  setBusy(false);
  if (data.trace_path) setStatus((data.ok ? "✓ " : "✗ ") + data.stop_reason + " · 轨迹已保存：" + data.trace_path, data.ok ? "ok" : "err");
  else setStatus((data.ok ? "✓ " : "✗ ") + data.stop_reason, data.ok ? "ok" : "err");
  // Bring focus back to the input so the next command can be typed immediately.
  $("task").focus();
}

async function stopRun() {
  if (!currentRunId) return;
  setStatus("已请求停止（将在下一轮模型调用处生效）", "err");
  try {
    await fetch("/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ run_id: currentRunId }) });
  } catch (e) { /* ignore */ }
}

/* ---------------- replay ---------------- */
function normalizeTrace(raw) {
  const event = raw.event;
  const data = { ...raw };
  delete data.event; delete data.timestamp;
  switch (event) {
    case "tool_result": {
      const r = data.result || {};
      return { type: "tool_finished", data: { round: data.round, tool: data.tool, ok: r.ok, output: r.output, error: r.error, metadata: r.metadata || {} } };
    }
    case "memory_prepared":
      if (data.context) return { type: event, data: { round: data.round, ...data.context } };
      return { type: event, data };
    case "model_request": return { type: "skip", data };
    default: return { type: event, data };
  }
}

async function doReplay() {
  const path = $("replay-path").value.trim();
  if (!path) { setStatus("请填写轨迹文件路径", "err"); return; }
  closeReplayDialog();
  const tl = $("timeline");
  resetTurns();
  tl.innerHTML = "";
  setStatus("正在回放…");
  try {
    const res = await fetch("/replay?path=" + encodeURIComponent(path));
    const data = await res.json();
    if (!res.ok || data.error) { setStatus(data.error || "回放失败", "err"); return; }
    replayEvents(data.events || []);
  } catch (e) { setStatus("回放请求失败：" + e, "err"); }
}

function replayEvents(events) {
  let i = 0;
  const step = () => {
    if (i >= events.length) { setStatus("回放结束（共 " + events.length + " 条事件）"); return; }
    const raw = events[i++];
    const { type, data } = normalizeTrace(raw);
    if (type !== "skip") renderEvent(type, data);
    setTimeout(step, 45);
  };
  step();
}

/* ---------------- dialog ---------------- */
function openReplayDialog() {
  $("replay-dialog").showModal();
  if (!$("replay-path").value) $("replay-path").value = "demo_workspace/trace/";
}
function closeReplayDialog() { $("replay-dialog").close(); }

/* ---------------- init ---------------- */
async function loadConfig() {
  try {
    const res = await fetch("/config");
    const cfg = await res.json();
    $("base-url").value = cfg.base_url || "";
    $("model").value = cfg.model || "";
    if (cfg.has_api_key) setStatus("就绪（已检测到环境变量 OPENAI_API_KEY，API Key 可留空）");
  } catch (e) { /* ignore */ }
}

function init() {
  renderInnovations();
  $("btn-run").addEventListener("click", startRun);
  $("btn-stop").addEventListener("click", stopRun);
  $("btn-clear").addEventListener("click", () => { resetTurns(); $("timeline").innerHTML = `<div class="placeholder">已清空。</div>`; });
  $("btn-replay").addEventListener("click", openReplayDialog);
  $("btn-replay-go").addEventListener("click", doReplay);
  $("btn-replay-close").addEventListener("click", closeReplayDialog);
  $("task").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) startRun(); });
  loadConfig();
}

document.addEventListener("DOMContentLoaded", init);
