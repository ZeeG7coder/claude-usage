const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  summary: null,
  chartRange: "hour",
  config: null,
  timer: null,
};

/* ------------------------------ формат ------------------------------ */
function fmtTokens(n) {
  if (!n) return "0";
  const abs = Math.abs(n);
  if (abs >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (abs >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (abs >= 1e3) return (n / 1e3).toFixed(abs >= 1e5 ? 0 : 1) + "k";
  return String(Math.round(n));
}
function fmtUnits(n) {
  if (!n) return "0";
  if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(0) + "k";
  return String(Math.round(n));
}
function fmtMoney(n) {
  if (!n) return "$0";
  if (n < 0.01) return "<$0.01";
  return "$" + n.toFixed(n < 10 ? 2 : 0);
}
function fmtTime(iso) {
  const d = new Date(iso);
  return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}
function fmtShort(iso) {
  const d = new Date(iso);
  return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit" }) + " " +
    d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}
function fmtDelta(seconds) {
  if (seconds == null) return "—";
  const s = Math.max(0, Math.round(seconds));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}д ${h}ч`;
  if (h > 0) return `${h}ч ${String(m).padStart(2, "0")}м`;
  return `${m}м`;
}
function pctClass(pct) {
  if (pct == null) return "none";
  if (pct >= 100) return "over";
  if (pct >= 80) return "warn";
  return "";
}

/* ------------------------------ окна лимитов ------------------------------ */
function renderWindows(summary) {
  const box = $("#windows");
  box.innerHTML = "";
  for (const win of summary.windows) {
    const u = win.usage;
    const pct = win.percent;
    const cls = pctClass(pct);
    const width = pct == null ? 0 : Math.min(100, pct);
    const limitText = win.limit > 0 ? ` / ${fmtUnits(win.limit)}` : "";
    const resetText = win.reset_at
      ? (win.reset_in != null && win.reset_in < 90
          ? "сброс сейчас"
          : `сброс через ${fmtDelta(win.reset_in)}`)
      : "";
    const el = document.createElement("div");
    el.className = "win";
    el.innerHTML = `
      <div class="win-head">
        <span class="win-label">${escapeHtml(win.label)}</span>
        <span class="win-pct ${cls}">${pct == null ? "лимит не задан" : (pct).toFixed(1) + "%"}</span>
      </div>
      <div class="win-value">
        <b>${fmtUnits(u.units)}</b>
        <span>ед.${limitText ? " из " + fmtUnits(win.limit) : ""}</span>
      </div>
      <div class="bar ${win.limit > 0 ? "" : "dim"}"><i class="${cls}" style="width:${width}%"></i></div>
      <div class="win-foot">
        <span>${fmtTokens(u.tokens)} ток. · ${fmtMoney(u.cost)}</span>
        <span class="reset">${resetText}</span>
      </div>`;
    el.title =
      `${win.label}\n` +
      `токены: вход ${fmtTokens(u.input)} · выход ${fmtTokens(u.output)} · ` +
      `cache write ${fmtTokens(u.cache_write)} · cache read ${fmtTokens(u.cache_read)}\n` +
      `единицы: ${fmtUnits(u.units)}${win.limit ? " из " + fmtUnits(win.limit) : ""}\n` +
      `сообщений: ${u.messages} · оценка стоимости ${fmtMoney(u.cost)}` +
      (win.reset_at ? `\nсброс: ${fmtTime(win.reset_at)}` : "");
    box.appendChild(el);
  }
}

/* ------------------------------ график ------------------------------ */
function renderChart() {
  const summary = state.summary;
  if (!summary) return;
  const buckets = summary.series[state.chartRange];
  const chart = $("#chart");
  const axis = $("#axis");
  chart.innerHTML = "";
  axis.innerHTML = "";

  const maxUnits = Math.max(...buckets.map((b) => b.usage.units), 1);
  const totalUnits = buckets.reduce((sum, b) => sum + b.usage.units, 0);
  $("#chart-note").textContent = `${fmtUnits(totalUnits)} ед. · ${fmtMoney(buckets.reduce((s, b) => s + b.usage.cost, 0))}`;

  buckets.forEach((b, i) => {
    const u = b.usage;
    const col = document.createElement("div");
    col.className = "col" + (u.units ? "" : " empty");
    const height = Math.max(u.units ? 3 : 2, (u.units / maxUnits) * 100);
    const stack = document.createElement("div");
    stack.className = "stack";
    stack.style.height = height + "%";
    const parts = [
      ["io", u.input + u.output],
      ["write", u.cache_write],
      ["read", u.cache_read],
    ];
    const weightSum = u.units || 1;
    for (const [cls, tokens] of parts) {
      const seg = document.createElement("div");
      seg.className = "seg " + cls;
      const share = u.units ? (tokens * (cls === "read" ? 0.1 : cls === "write" ? 1.25 : 1)) / weightSum : 1;
      seg.style.flex = String(Math.max(share, 0.0001));
      stack.appendChild(seg);
    }
    col.appendChild(stack);
    col.title =
      `${b.label}\n` +
      `${fmtUnits(u.units)} ед. · ${fmtTokens(u.tokens)} ток. · ${fmtMoney(u.cost)}\n` +
      `in ${fmtTokens(u.input)} · out ${fmtTokens(u.output)} · ` +
      `cache+ ${fmtTokens(u.cache_write)} · cache↺ ${fmtTokens(u.cache_read)}\n` +
      `сообщений: ${u.messages}`;
    chart.appendChild(col);

    const label = document.createElement("span");
    const step = state.chartRange === "hour" ? 3 : 2;
    label.textContent = (i % step === 0 || i === buckets.length - 1) ? b.label : "";
    axis.appendChild(label);
  });
}

/* ------------------------------ списки ------------------------------ */
function renderRows(container, items, opts) {
  container.innerHTML = "";
  if (!items.length) {
    container.innerHTML = `<div class="empty-note">${opts.empty || "Пока пусто"}</div>`;
    return;
  }
  const max = Math.max(...items.map((i) => i.usage.units), 1);
  for (const item of items) {
    const u = item.usage;
    const row = document.createElement("div");
    row.className = "row";
    const name = opts.name(item);
    row.innerHTML = `
      <span class="name" title="${escapeHtml(name)}">${escapeHtml(name)}</span>
      <span class="num">${fmtUnits(u.units)} ед.</span>
      <span class="mini"><i style="width:${(u.units / max) * 100}%"></i></span>
      <span class="meta">
        <span>${fmtTokens(u.tokens)} ток.</span>
        <span>${fmtMoney(u.cost)}</span>
        ${opts.meta ? `<span>${opts.meta(item)}</span>` : ""}
      </span>`;
    if (opts.onclick) {
      row.addEventListener("click", () => opts.onclick(item));
    }
    container.appendChild(row);
  }
}

function renderLists(summary) {
  renderRows($("#models"), summary.models, {
    name: (m) => m.key,
    meta: (m) => `${m.usage.messages} сообщ.`,
  });
  renderRows($("#projects"), summary.projects, {
    name: (p) => p.key,
    meta: (p) => `${p.usage.messages} сообщ.`,
  });
  renderRows($("#sessions"), summary.sessions, {
    name: (s) => s.title,
    meta: (s) => `${s.project} · ${fmtShort(s.last_ts)}`,
    onclick: (s) => openSession(s.session_id),
  });
}

/* ------------------------------ модалки ------------------------------ */
function openModal(id) {
  $("#" + id).hidden = false;
}
function closeModals() {
  $$(".modal").forEach((m) => (m.hidden = true));
}
$$("[data-close]").forEach((btn) => btn.addEventListener("click", closeModals));
$$(".modal").forEach((modal) =>
  modal.addEventListener("click", (e) => {
    if (e.target === modal) closeModals();
  })
);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeModals();
});

async function openSession(id) {
  const body = $("#s-body");
  $("#s-title").textContent = "Сессия";
  $("#s-meta").textContent = "загружаю…";
  body.innerHTML = "";
  openModal("modal-session");
  try {
    const res = await fetch("/api/session?id=" + encodeURIComponent(id));
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "ошибка");
    $("#s-title").textContent = data.title;
    $("#s-meta").textContent = `${data.cwd || "—"} · ${id}`;
    const u = data.usage;
    body.innerHTML = `
      <div class="s-summary">
        <span><b>${fmtUnits(u.units)}</b> ед.</span>
        <span><b>${fmtTokens(u.tokens)}</b> ток.</span>
        <span><b>${fmtMoney(u.cost)}</b></span>
        <span><b>${u.messages}</b> запросов</span>
        <span>вход ${fmtTokens(u.input)} · выход ${fmtTokens(u.output)}</span>
        <span>cache+ ${fmtTokens(u.cache_write)} · cache↺ ${fmtTokens(u.cache_read)}</span>
      </div>
      <table class="events">
        <thead><tr>
          <th>время</th><th>модель</th><th>in</th><th>cache+</th><th>cache↺</th><th>out</th><th>ед.</th><th>$</th>
        </tr></thead>
        <tbody>
          ${data.events
            .slice()
            .reverse()
            .map(
              (e) => `<tr>
                <td>${fmtShort(e.ts)}</td>
                <td>${escapeHtml(e.model || "—")}${e.sidechain ? " ⤳" : ""}</td>
                <td>${fmtTokens(e.input)}</td>
                <td>${fmtTokens(e.cache_write)}</td>
                <td>${fmtTokens(e.cache_read)}</td>
                <td>${fmtTokens(e.output)}</td>
                <td>${fmtUnits(e.units)}</td>
                <td>${fmtMoney(e.cost)}</td>
              </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  } catch (err) {
    $("#s-meta").textContent = "Не удалось загрузить: " + err.message;
  }
}

/* ------------------------------ настройки ------------------------------ */
const MODEL_FIELDS = [
  ["input", "вход"],
  ["output", "выход"],
  ["cache_write", "cache write"],
  ["cache_read", "cache read"],
];

function renderSettings(cfg) {
  state.config = cfg;
  const limitsBox = $("#cfg-limits");
  limitsBox.innerHTML = "";
  for (const [key, spec] of Object.entries(cfg.limits)) {
    const row = document.createElement("div");
    row.className = "cfg-row";
    row.innerHTML = `
      <span>${escapeHtml(spec.label || key)}
        <small>${spec.mode === "rolling" ? (spec.hours ? spec.hours + " ч скользящее" : spec.days + " дн. скользящее") : spec.mode === "calendar_day" ? "календарный день" : "календарная неделя"}</small>
      </span>
      <input type="number" min="0" step="0.5" data-limit="${key}" value="${(Number(spec.limit) || 0) / 1e6}">`;
    limitsBox.appendChild(row);
  }

  const weightsBox = $("#cfg-weights");
  weightsBox.innerHTML = "";
  for (const [field, label] of MODEL_FIELDS) {
    const wrap = document.createElement("label");
    wrap.textContent = label;
    const input = document.createElement("input");
    input.type = "number";
    input.min = "0";
    input.step = "0.05";
    input.value = cfg.weights[field];
    input.dataset.weight = field;
    wrap.appendChild(input);
    weightsBox.appendChild(wrap);
  }

  const pricesBox = $("#cfg-prices");
  pricesBox.innerHTML = "";
  const head = document.createElement("div");
  head.className = "price-row head";
  head.innerHTML =
    "<span>модель</span>" +
    MODEL_FIELDS.map(([, label]) => `<span>${label}</span>`).join("");
  pricesBox.appendChild(head);
  for (const [family, price] of Object.entries(cfg.prices)) {
    const row = document.createElement("div");
    row.className = "price-row";
    const name = document.createElement("span");
    name.className = "price-family";
    name.textContent = family;
    row.appendChild(name);
    for (const [field] of MODEL_FIELDS) {
      const input = document.createElement("input");
      input.type = "number";
      input.min = "0";
      input.step = "0.01";
      input.value = price[field];
      input.dataset.priceFamily = family;
      input.dataset.priceField = field;
      row.appendChild(input);
    }
    pricesBox.appendChild(row);
  }
}

async function saveSettings() {
  const err = $("#cfg-err");
  err.textContent = "";
  const limits = {};
  $$("[data-limit]").forEach((input) => {
    const key = input.dataset.limit;
    limits[key] = { limit: Math.max(0, Number(input.value) || 0) * 1e6 };
  });
  const weights = {};
  $$("[data-weight]").forEach((input) => {
    weights[input.dataset.weight] = Math.max(0, Number(input.value) || 0);
  });
  const prices = {};
  $$("[data-price-family]").forEach((input) => {
    const family = input.dataset.priceFamily;
    prices[family] = prices[family] || {};
    prices[family][input.dataset.priceField] = Math.max(0, Number(input.value) || 0);
  });
  try {
    const res = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ limits, weights, prices }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "ошибка сохранения");
    closeModals();
    await load();
    scheduleRefresh();
  } catch (e) {
    err.textContent = e.message;
  }
}

/* ------------------------------ загрузка ------------------------------ */
async function load() {
  const live = $("#live");
  try {
    const res = await fetch("/api/summary");
    if (!res.ok) throw new Error("HTTP " + res.status);
    const summary = await res.json();
    state.summary = summary;
    renderWindows(summary);
    renderChart();
    renderLists(summary);

    const now = new Date(summary.now);
    $("#meta").textContent =
      `${summary.timezone} · ${now.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}` +
      ` · всего ${fmtTokens(summary.totals.all.tokens)} ток. · ${fmtMoney(summary.totals.all.cost)}`;
    live.classList.remove("off");
    live.innerHTML = "<i></i> live";
  } catch (e) {
    live.classList.add("off");
    live.innerHTML = "<i></i> нет связи";
    console.error(e);
  }
}

async function loadStatus() {
  try {
    const res = await fetch("/api/status");
    const s = await res.json();
    $("#dbstat").textContent = `${s.events} запросов · ${s.files} файлов · ${s.db}`;
  } catch (_) {}
}

async function loadConfig() {
  const res = await fetch("/api/config");
  renderSettings(await res.json());
}

function scheduleRefresh() {
  clearInterval(state.timer);
  const seconds = state.summary?.config?.refresh_seconds || 20;
  state.timer = setInterval(() => {
    if (!document.hidden) load();
  }, Math.max(5, seconds) * 1000);
}

/* ------------------------------ события ------------------------------ */
$$("#chart-tabs .tab").forEach((tab) =>
  tab.addEventListener("click", () => {
    $$("#chart-tabs .tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    state.chartRange = tab.dataset.range;
    renderChart();
  })
);

$("#refresh").addEventListener("click", async (e) => {
  e.target.disabled = true;
  e.target.textContent = "Читаю…";
  try {
    await fetch("/api/refresh", { method: "POST" });
    await load();
    await loadStatus();
  } finally {
    e.target.disabled = false;
    e.target.textContent = "Обновить";
  }
});

$("#settings").addEventListener("click", async () => {
  await loadConfig();
  openModal("modal-settings");
});

$("#cfg-save").addEventListener("click", saveSettings);

function escapeHtml(str) {
  return String(str ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* ------------------------------ старт ------------------------------ */
(async function init() {
  await load();
  scheduleRefresh();
  await loadStatus();
  setInterval(loadStatus, 60000);

  const hash = decodeURIComponent(location.hash.replace(/^#/, ""));
  if (hash === "settings") {
    await loadConfig();
    openModal("modal-settings");
  } else if (hash.startsWith("session=")) {
    openSession(hash.slice("session=".length));
  }
})();
