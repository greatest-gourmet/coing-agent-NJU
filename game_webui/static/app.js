"use strict";

const $ = (id) => document.getElementById(id);

let sid = "";            // 会话标识，保持同一页签里的游戏进度
let history = [];        // 每次猜测记录 { value, low/high }

const elResult = $("result");
const elGuess = $("guess");
const elForm = $("form");
const elAttempts = $("attempts");
const elHistory = $("history");
const elNewGame = $("newgame");

function render(state) {
  elAttempts.textContent = String(state.attempts);
  elResult.textContent = state.hint;
  elResult.classList.toggle("won", state.won);
  elGuess.disabled = !state.active || state.won;
  elNewGame.hidden = !state.won && state.active === false;

  // 历史标签
  elHistory.innerHTML = "";
  history.forEach((item) => {
    const chip = document.createElement("span");
    chip.className = "chip " + item.cls;
    chip.textContent = item.value;
    elHistory.appendChild(chip);
  });
}

async function api(path, payload) {
  const url = path + "?sid=" + encodeURIComponent(sid);
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  return res.json();
}

function recordGuess(value, cls) {
  history.push({ value, cls });
  if (history.length > 20) history.shift();
}

elForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const raw = elGuess.value.trim();
  if (!raw) return;
  const val = parseInt(raw, 10);
  const state = await api("/guess", { guess: String(val) });
  const low = state.hint.indexOf("小") > -1 && state.hint.indexOf("大") === -1;
  const high = state.hint.indexOf("大") > -1 && state.hint.indexOf("小") === -1;
  recordGuess(val, low ? "low" : high ? "high" : "");
  elGuess.value = "";
  render(state);
});

elNewGame.addEventListener("click", async () => {
  history = [];
  const state = await api("/new", {});
  render(state);
});

// 初始化：取会话
(async function init() {
  try {
    const res = await fetch("/state");
    const state = await res.json();
    if (state.sid) sid = state.sid;
    state.hint = state.hint || "我选好了 1~100 之间的一个数字，开始猜吧！";
    history = [];
    render(state);
  } catch (err) {
    elResult.textContent = "连接到游戏服务器失败，请确认已启动 server.py。";
  }
})();
