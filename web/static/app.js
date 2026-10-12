/* Todoist Pomodoro web UI — mirrors the GTK app's functions. */
"use strict";
const $ = (id) => document.getElementById(id);
const api = {
  async get(p) { const r = await fetch(p); const j = await r.json(); if (!j.ok) throw new Error(j.error || r.statusText); return j; },
  async post(p, b = {}) { const r = await fetch(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) }); const j = await r.json(); if (!j.ok) throw new Error(j.error || r.statusText); return j; },
};

const S = {
  tasks: [], candidates: [], selectedId: null, timerTask: null,
  focusMin: 25, breakMin: 5, mode: "focus", total: 25 * 60, remaining: 25 * 60,
  tickId: null, sessionStart: null, rsTarget: null, bannerTimer: null, lastDone: null,
};

const fmt = (s) => { s = Math.max(0, s | 0); const m = (s / 60) | 0, ss = s % 60, h = (m / 60) | 0; return h ? `${h}:${String(m % 60).padStart(2, "0")}:${String(ss).padStart(2, "0")}` : `${String(m).padStart(2, "0")}:${String(ss).padStart(2, "0")}`; };
const fmtDur = (s) => { s = Math.max(0, s | 0); const h = (s / 3600) | 0, m = ((s % 3600) / 60) | 0, sec = s % 60; if (h) return m ? `${h}h ${m}m` : `${h}h`; if (m) return `${m}m`; return sec ? `${sec}s` : "0m"; };
const isOverdue = (t) => t.due_date ? t.due_date.slice(0, 10) < new Date().toISOString().slice(0, 10) : false;
const meta = (t) => { const p = []; if (t.project_name) p.push(t.project_name); if (isOverdue(t)) p.push("overdue"); else if (t.due_date) p.push(t.due_date.slice(0, 10)); if (t.priority >= 2) p.push("P" + t.priority); if (t.is_recurring) p.push("recurring"); return p.join(" · "); };

// -- views / banner ------------------------------------------------
function show(name) { for (const v of ["today", "timer", "stats"]) $("view-" + v).classList.toggle("hidden", v !== name); if (name === "stats") loadStats(); }
document.querySelectorAll("[data-go]").forEach(b => b.onclick = () => show(b.dataset.go));
$("btn-today").onclick = () => show("today"); $("btn-timer").onclick = () => show("timer");
$("btn-stats").onclick = () => show("stats"); $("btn-settings").onclick = openSettings;
$("btn-palette").onclick = openPalette;
document.querySelectorAll("[data-close]").forEach(b => b.onclick = () => $(b.dataset.close).classList.add("hidden"));

let toastTimer = null;
function toast(text, undoTask) {
  $("banner-text").textContent = text; $("banner").classList.remove("hidden");
  $("banner-undo").classList.toggle("hidden", !undoTask);
  S.lastDone = undoTask || null;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => $("banner").classList.add("hidden"), undoTask ? 5000 : 3000);
}
$("banner-undo").onclick = async () => { if (!S.lastDone) return; try { await api.post("/api/reopen", { id: S.lastDone.id }); toast("Task restored."); loadToday(); } catch (e) { toast("Could not undo: " + e.message); } };

// -- today ----------------------------------------------------------
function rowEl(t) {
  const d = document.createElement("div"); d.className = "task-row"; d.dataset.id = t.id;
  const c = document.createElement("input"); c.type = "checkbox"; c.title = "Mark complete";
  c.onclick = (e) => { e.stopPropagation(); completeTask(t); };
  const label = document.createElement("span"); label.className = "content"; label.textContent = t.content;
  const m = document.createElement("span"); m.className = "meta" + (isOverdue(t) ? " overdue" : ""); m.textContent = meta(t);
  d.append(c, label, m);
  d.onclick = () => selectTask(t);
  d.oncontextmenu = (e) => { e.preventDefault(); selectRow(t.id, d.parentElement); openCtx(e.clientX, e.clientY, t); };
  return d;
}
function renderTasks(list, el) { el.innerHTML = ""; if (!list.length) { el.innerHTML = '<div class="dim">Nothing due — enjoy the calm.</div>'; return; } for (const t of list) el.append(rowEl(t)); }
function selectRow(id, container) { S.selectedId = id; (container || $("task-list")).querySelectorAll(".task-row").forEach(r => r.style.outline = r.dataset.id === id ? "2px solid var(--acc)" : ""); }
function selectedTask() { return S.tasks.find(t => t.id === S.selectedId) || S.candidates.find(t => t.id === S.selectedId) || null; }
function applyTodayFilter() {
  const q = $("today-search").value.trim().toLowerCase(), pid = $("project-filter").value;
  renderTasks(S.tasks.filter(t => (!q || t.content.toLowerCase().includes(q)) && (!pid || t.project_id === pid)), $("task-list"));
  selectRow(S.selectedId);
}
$("today-search").oninput = applyTodayFilter;
$("project-filter").onchange = applyTodayFilter;

async function loadToday() {
  $("today-status").textContent = "Loading…";
  try {
    const [today, proj] = await Promise.all([api.get("/api/today"), api.get("/api/projects").catch(() => ({ projects: [] }))]);
    S.tasks = today.tasks;
    const sel = $("project-filter"), cur = sel.value;
    sel.innerHTML = '<option value="">All projects</option>';
    for (const p of (proj.projects || [])) { const o = document.createElement("option"); o.value = p.id; o.textContent = p.name; sel.append(o); }
    if ([...sel.options].some(o => o.value === cur)) sel.value = cur;
    applyTodayFilter();
    $("today-status").textContent = S.tasks.length ? "" : "Nothing due — enjoy the calm.";
    const pend = await api.get("/api/pending").catch(() => ({ pending: 0 }));
    $("pending-count").textContent = pend.pending ? `(${pend.pending})` : "";
  } catch (e) {
    $("today-status").textContent = "Could not load tasks: " + e.message;
    if (/401|Unauthorized|token/i.test(e.message)) $("auth-banner").classList.remove("hidden");
  }
}
$("btn-refresh").onclick = loadToday;

function selectTask(t) { S.timerTask = t; S.selectedId = t.id; selectRow(t.id); bindTimer(); show("timer"); }

async function completeTask(t) {
  try { await api.post("/api/complete", { id: t.id }); stopTimerIfBound(t.id, true); toast(`Completed: ${t.content || "Task"}`, t); S.tasks = S.tasks.filter(x => x.id !== t.id); applyTodayFilter(); }
  catch (e) { toast("Could not complete: " + e.message); }
}
$("btn-add").onclick = async () => {
  const v = $("add-input").value.trim(); if (!v) return;
  try { const j = await api.post("/api/add", { text: v }); $("add-input").value = ""; S.tasks.push(j.task); applyTodayFilter(); toast("Task added."); }
  catch (e) { toast("Add failed — kept as pending: " + e.message); loadToday(); }
};
$("add-input").onkeydown = (e) => { if (e.key === "Enter") $("btn-add").click(); };
$("btn-retry").onclick = async () => { try { await api.post("/api/retry"); toast("Retry finished — reloading…"); loadToday(); } catch (e) { toast("Retry failed: " + e.message); } };

// -- context menu (right-click): start / complete / reschedule / custom --
const PRESETS = [["Tomorrow", "tomorrow"], ["Next week", "next week"], ["Next Monday", "next monday"]];
function openCtx(x, y, t) {
  const c = $("ctx"); c.innerHTML = "";
  const mk = (label, fn) => { const b = document.createElement("button"); b.textContent = label; b.onclick = () => { c.classList.add("hidden"); fn(); }; c.append(b); };
  mk("Start timer", () => selectTask(t));
  mk("Complete", () => completeTask(t));
  const sep = document.createElement("div"); sep.className = "sep"; c.append(sep);
  for (const [label, due] of PRESETS) mk(label, () => reschedule(t, due));
  mk("Custom…", () => openResched(t));
  c.style.left = Math.min(x, innerWidth - 210) + "px"; c.style.top = Math.min(y, innerHeight - 240) + "px";
  c.classList.remove("hidden");
}
document.addEventListener("click", (e) => { if (!$("ctx").contains(e.target)) $("ctx").classList.add("hidden"); });
async function reschedule(t, due) { try { const j = await api.post("/api/reschedule", { id: t.id, due_string: due }); Object.assign(t, j.task); applyTodayFilter(); toast(`Rescheduled to ${j.task.due_date || "?"}.`); } catch (e) { toast("Could not reschedule: " + e.message); } }
function openResched(t) { S.rsTarget = t; $("rs-date").value = ""; $("rs-time").value = ""; $("rs-free").value = ""; $("resched").classList.remove("hidden"); $("rs-free").focus(); }
$("btn-rs-save").onclick = () => {
  const t = S.rsTarget; if (!t) return;
  const free = $("rs-free").value.trim();
  const due = free || `${$("rs-date").value} ${$("rs-time").value}`.trim();
  if (!due) { toast("Enter a date like 'tomorrow'."); return; }
  $("resched").classList.add("hidden"); reschedule(t, due);
};

// -- palette (Ctrl+P): search-all + saved filters --------------------
async function openPalette() {
  $("palette").classList.remove("hidden"); $("palette-search").value = ""; $("palette-status").textContent = "Loading…";
  try {
    const [c, f] = await Promise.all([api.get("/api/candidates"), api.get("/api/filters")]);
    S.candidates = c.tasks;
    const sel = $("palette-filter"); sel.innerHTML = "";
    const mk = (v, l) => { const o = document.createElement("option"); o.value = v; o.textContent = l; sel.append(o); };
    mk("", "All tasks"); for (const fl of (f.filters || [])) mk(fl.query, fl.name); mk("__custom__", "Custom…");
    applyPaletteFilter();
  } catch (e) { $("palette-status").textContent = "Could not load tasks: " + e.message; }
  setTimeout(() => $("palette-search").focus(), 0);
}
function applyPaletteFilter() {
  const q = $("palette-search").value.trim().toLowerCase();
  const hits = q ? S.candidates.filter(t => t.content.toLowerCase().includes(q) || (t.project_name || "").toLowerCase().includes(q)) : S.candidates;
  renderTasks(hits.slice(0, 100), $("palette-list"));
  $("palette-list").querySelectorAll(".task-row").forEach(r => { const t = hits.find(x => x.id === r.dataset.id); r.onclick = () => { $("palette").classList.add("hidden"); selectTask(t); }; });
  $("palette-status").textContent = hits.length ? "" : (S.candidates.length ? "No matches." : "No tasks.");
}
$("palette-search").oninput = applyPaletteFilter;
$("palette-search").onkeydown = (e) => { if (e.key === "Enter") { const first = $("palette-list").querySelector(".task-row"); if (first) first.click(); } };
$("palette-filter").onchange = async (e) => {
  const v = e.target.value;
  const q = v === "__custom__" ? $("palette-search").value.trim() : (v || null);
  try { await api.post("/api/filter/select", { query: q }); const c = await api.get("/api/candidates"); S.candidates = c.tasks; applyPaletteFilter(); }
  catch (err) { toast("Filter failed: " + err.message); }
};
$("btn-save-filter").onclick = async () => {
  const name = $("palette-filter-name").value.trim(), q = $("palette-search").value.trim();
  if (!name || !q) { toast("Name + query required to save a filter."); return; }
  try { await api.post("/api/filters", { name, query: q }); $("palette-filter-name").value = ""; toast(`Saved filter "${name}".`); openPalette(); }
  catch (e) { toast("Could not save filter: " + e.message); }
};

// -- timer (never auto-completes; Mark-done is explicit) --------------
function bindTimer() {
  stopTick(); S.mode = "focus"; S.total = S.focusMin * 60; S.remaining = S.total; S.sessionStart = null;
  $("timer-task").textContent = S.timerTask ? S.timerTask.content : "No task selected";
  $("t-done").disabled = !S.timerTask; renderTimer(); $("timer-state").textContent = "Ready";
  $("timer-meta").textContent = S.timerTask && S.timerTask.project_name ? S.timerTask.project_name : "";
}
function renderTimer() { $("timer-clock").textContent = fmt(S.remaining); document.title = fmt(S.remaining) + " — Todoist Pomodoro"; }
function startTick() {
  stopTick();
  if (!S.sessionStart) S.sessionStart = new Date().toISOString();
  $("t-start").disabled = true; $("t-pause").disabled = false;
  $("timer-state").textContent = S.mode === "focus" ? "Focusing…" : "On break…";
  S.tickId = setInterval(() => { S.remaining -= 1; if (S.remaining <= 0) { S.remaining = 0; renderTimer(); finishTimer(); } else renderTimer(); }, 1000);
}
function stopTick() { if (S.tickId) clearInterval(S.tickId); S.tickId = null; }
function playBell() { try { const ctx = new (window.AudioContext || window.webkitAudioContext)(); const o = ctx.createOscillator(), g = ctx.createGain(); o.connect(g); g.connect(ctx.destination); o.frequency.value = 880; g.gain.setValueAtTime(0.2, ctx.currentTime); o.start(); o.stop(ctx.currentTime + 0.6); } catch { /* silent */ } }
async function logSession(finished) {
  if (S.mode !== "focus") return;
  const elapsed = S.total - S.remaining;
  if (elapsed < 60) return;
  const t = S.timerTask || {};
  try { await api.post("/api/session", { task_id: t.id || "", content: t.content || "(no task)", project_id: t.project_id || "", project_name: t.project_name || "", duration_sec: elapsed, finished, started_at: S.sessionStart }); } catch { /* stats must never break the timer */ }
  S.sessionStart = null;
}
async function finishTimer() {
  stopTick(); await logSession(true); playBell();
  $("t-start").disabled = true; $("t-pause").disabled = true;
  $("timer-state").textContent = "Done — take a break or reset.";
}
function stopTimerIfBound(taskId, completed) {
  if (!S.timerTask || String(S.timerTask.id) !== String(taskId) || !S.tickId) return false;
  logSession(false); stopTick(); S.remaining = S.total; renderTimer();
  $("t-start").disabled = false; $("t-pause").disabled = true;
  $("timer-state").textContent = completed ? "Ready — task completed." : "Ready";
  return true;
}
$("t-start").onclick = () => { if (S.remaining <= 0) S.remaining = S.total; startTick(); };
$("t-pause").onclick = () => { stopTick(); $("t-start").disabled = false; $("t-start").textContent = "Resume"; $("t-pause").disabled = true; $("timer-state").textContent = "Paused"; };
$("t-reset").onclick = async () => { await logSession(false); stopTick(); S.remaining = S.total; renderTimer(); $("t-start").disabled = false; $("t-start").textContent = "Start"; $("t-pause").disabled = true; $("timer-state").textContent = "Ready"; };
$("t-break").onclick = async () => { await logSession(false); stopTick(); S.mode = "break"; S.total = S.breakMin * 60; S.remaining = S.total; renderTimer(); startTick(); };
$("t-fullscreen").onclick = () => { const el = document.querySelector(".timer-stage"); if (document.fullscreenElement) document.exitFullscreen(); else el.requestFullscreen && el.requestFullscreen(); };
$("t-done").onclick = () => { if (S.timerTask) completeTask(S.timerTask); };

// -- stats ------------------------------------------------------------
async function loadStats() {
  try {
    const j = await api.get("/api/stats"), d = j.stats;
    $("stats-summary").textContent = `Today: ${fmtDur(d.today_sec)}\nLast 7 days: ${fmtDur(d.week_sec)}\nDay streak: ${d.streak}`;
    const fill = (el, obj) => { const e = $(el); e.innerHTML = ""; const top = Object.entries(obj).sort((a, b) => b[1] - a[1]).slice(0, 5); if (!top.length) e.innerHTML = '<div class="dim">Nothing yet.</div>'; for (const [n, s] of top) { const r = document.createElement("div"); r.className = "task-row"; r.innerHTML = ""; const a = document.createElement("span"); a.className = "content"; a.textContent = n; const b = document.createElement("span"); b.className = "meta"; b.textContent = fmtDur(s); r.append(a, b); e.append(r); } };
    fill("stats-projects", d.by_project || {}); fill("stats-tasks", d.by_task || {});
  } catch (e) { $("stats-summary").textContent = "Could not read the session log."; }
}
$("btn-stats-refresh").onclick = loadStats;

// -- settings ---------------------------------------------------------
async function openSettings() {
  try { const j = await api.get("/api/settings"); $("set-focus").value = j.focus_min; $("set-break").value = j.short_break_min; $("token-status").textContent = "Todoist token: " + j.token_status; S.focusMin = j.focus_min; S.breakMin = j.break_min; }
  catch (e) { $("token-status").textContent = "Could not load settings."; }
  $("set-token").value = ""; $("settings").classList.remove("hidden");
}
$("btn-settings-save").onclick = async () => {
  try {
    const j = await api.post("/api/settings", { focus_min: +$("set-focus").value, short_break_min: +$("set-break").value, token: $("set-token").value.trim() });
    S.focusMin = j.focus_min; S.breakMin = j.short_break_min; bindTimer();
    $("settings").classList.add("hidden"); $("auth-banner").classList.add("hidden"); toast("Settings saved."); loadToday();
  } catch (e) { toast("Could not save: " + e.message); }
};

// -- keyboard: Ctrl+P palette, Ctrl+Enter start, Ctrl+Shift+C done, Ctrl+R reschedule --
document.addEventListener("keydown", (e) => {
  const inField = /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName || "");
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "p") { e.preventDefault(); openPalette(); }
  else if ((e.ctrlKey || e.metaKey) && e.key === "Enter" && !inField) { const t = selectedTask(); if (t) selectTask(t); }
  else if (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === "c" && !inField) { const t = selectedTask(); if (t) completeTask(t); }
  else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "r" && !inField) { e.preventDefault(); const t = selectedTask(); if (t) openResched(t); }
  else if (e.key === "Escape") { ["palette", "settings", "resched"].forEach(id => $(id).classList.add("hidden")); }
});

// -- boot -------------------------------------------------------------
(async () => {
  try { const j = await api.get("/api/settings"); S.focusMin = j.focus_min; S.breakMin = j.break_min; if (!j.has_token) { $("auth-banner").textContent = "No Todoist token yet — open Settings to connect."; $("auth-banner").classList.remove("hidden"); } }
  catch { /* offline: timer still works locally */ }
  bindTimer(); loadToday(); show("today");
})();
