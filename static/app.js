"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const SVGNS = "http://www.w3.org/2000/svg";

const store = {
  get(key, fallback) {
    try {
      const v = localStorage.getItem(`anc.${key}`);
      return v == null ? fallback : JSON.parse(v);
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(`anc.${key}`, JSON.stringify(value));
    } catch {
      /* private mode */
    }
  },
};

const state = {
  location: store.get("location", null),
  target: store.get("target", "M31"),
  settings: store.get("settings", null),
  defaults: null,
  targets: [],
  plan: null,
  selected: null,
  saved: [],
  loading: 0,
};

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const verdictClass = (v) => `v-${String(v).toLowerCase()}`;

async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => (el.hidden = true), 3200);
}

function status(kind, msg) {
  const el = $("#status");
  if (!kind) {
    el.hidden = true;
    return;
  }
  el.hidden = false;
  el.className = `status ${kind === "error" ? "error" : ""}`;
  el.innerHTML = kind === "loading" ? `<span class="spinner" aria-hidden="true"></span><span>${esc(msg)}</span>` : esc(msg);
}

function queryParams(extra = {}) {
  const loc = state.location;
  const p = new URLSearchParams({
    lat: loc.lat,
    lon: loc.lon,
    label: loc.label,
    elevation: loc.elevation || 0,
    target: state.target,
    ...state.settings,
    ...extra,
  });
  if (loc.timezone) p.set("timezone", loc.timezone);
  return p;
}

// ---------- forecast ----------
async function runForecast() {
  if (!state.location) return;
  const ticket = ++state.loading;
  status("loading", `Computing sky positions and weather for ${state.location.label}…`);
  if (!state.plan) showSkeleton();
  try {
    const plan = await api(`/api/forecast?${queryParams()}`);
    if (ticket !== state.loading) return; // a newer request superseded this one
    state.plan = plan;
    const keep = plan.nights.find((n) => n.date === state.selected);
    state.selected = keep ? keep.date : plan.best?.date || plan.nights.find((n) => n.darkness !== "none")?.date || plan.nights[0]?.date;
    status(null);
    render();
  } catch (err) {
    if (ticket !== state.loading) return;
    status("error", err.message);
    if (!state.plan) $("#results").hidden = true;
  }
}

function showSkeleton() {
  $("#results").hidden = false;
  $("#hero").innerHTML = `<div class="skeleton" style="min-height:120px;grid-column:1/-1"></div>`;
  $("#nights").innerHTML = Array.from({ length: 8 }, () => `<div class="skeleton"></div>`).join("");
  $("#detail").innerHTML = `<div class="skeleton" style="min-height:280px"></div>`;
}

function render() {
  $("#results").hidden = false;
  renderHero();
  renderNights();
  renderDetail();
  renderSaved();
}

function renderHero() {
  const { plan } = state;
  const loc = plan.location;
  const target = plan.target;
  const bortle = loc.bortle ? `Bortle ${loc.bortle}` : "Bortle ?";
  const isSaved = state.saved.some((s) => sameSpot(s, loc));
  const best = plan.best && plan.nights.find((n) => n.date === plan.best.date);
  const eyebrow = `
    <p class="hero-eyebrow">
      <span>${esc(loc.label)}</span>
      <button class="pill link" type="button" data-act="bortle" title="Set the sky brightness for this spot">${bortle}</button>
      <span class="pill">${esc(target.id === "none" ? "No target" : target.short)}</span>
    </p>`;
  let body;
  if (best) {
    const t = best.target ? `<span>${esc(target.short)} peaks <b>${best.target.peak_alt}°</b> at <b>${best.target.peak_time}</b></span>` : "";
    body = `
      <h2>Best night: ${esc(best.label)} · ${esc(best.best_window)}</h2>
      <p class="facts">
        <span><b>${best.usable_hours} h</b> shootable</span>
        <span>Score <b>${best.score}</b> <span class="verdict ${verdictClass(best.verdict)}">${best.verdict}</span></span>
        <span>Moon <b>${best.moon.illumination}%</b> ${esc(best.moon.phase.toLowerCase())}</span>
        <span>Cloud <b>${best.avg_cloud ?? "—"}%</b></span>
        ${t}
      </p>
      <p class="muted small" style="margin-top:6px">${plan.good_nights} good or better night${plan.good_nights === 1 ? "" : "s"} in the next ${plan.nights.length}.</p>`;
  } else {
    body = `
      <h2>No shootable nights in the forecast</h2>
      <p class="facts"><span>Nothing meets your limits over the next ${plan.nights.length} nights. Try loosening them, or pick a target that's higher right now.</span></p>`;
  }
  $("#hero").innerHTML = `
    <div>${eyebrow}${body}${plan.tip ? `<p class="tip">${esc(plan.tip)}</p>` : ""}</div>
    <div class="hero-actions">
      <button class="btn ghost" type="button" data-act="save">${isSaved ? "★ Saved" : "☆ Save location"}</button>
      <button class="btn ghost" type="button" data-act="refresh">Refresh</button>
    </div>`;
  $('#hero [data-act="save"]').addEventListener("click", toggleSave);
  $('#hero [data-act="refresh"]').addEventListener("click", runForecast);
  $('#hero [data-act="bortle"]').addEventListener("click", () => openDrawer("#bortleSelect"));
}

function renderNights() {
  const list = $("#nights");
  list.innerHTML = "";
  const south = state.plan.location.lat < 0;
  for (const n of state.plan.nights) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.role = "listitem";
    btn.className = `night${n.score === 0 ? " is-poor" : ""}`;
    btn.setAttribute("aria-current", String(n.date === state.selected));
    const [dow, ...rest] = n.label.split(" ");
    btn.innerHTML = `
      <div class="night-top">
        <div class="night-date"><b>${esc(dow)}</b><small>${esc(rest.join(" "))}</small></div>
        ${ringSvg(n.score, n.verdict)}
      </div>
      <div class="night-mid">
        ${moonSvg(n.moon.elongation, south)}
        <div class="night-stat">${n.usable_hours ? `<b>${n.usable_hours} h</b> clear` : "No window"}<br /><span class="verdict ${verdictClass(n.verdict)}">${n.verdict}</span></div>
      </div>
      ${cloudBar(n)}
      <p class="night-why">${esc(n.reasons[0] || "")}</p>`;
    btn.setAttribute("aria-label", `${n.label}: ${n.verdict}, score ${n.score}. ${n.reasons[0] || ""}`);
    btn.addEventListener("click", () => {
      state.selected = n.date;
      $$(".night", list).forEach((el) => el.setAttribute("aria-current", String(el === btn)));
      renderDetail();
      if (window.innerWidth < 860) $("#detail").scrollIntoView({ behavior: "smooth", block: "start" });
    });
    list.appendChild(btn);
  }
}

function ringSvg(score, verdict) {
  const r = 19;
  const c = 2 * Math.PI * r;
  const color = `var(--${verdict.toLowerCase()})`;
  return `<svg class="ring" viewBox="0 0 46 46" aria-hidden="true">
    <circle class="bg" cx="23" cy="23" r="${r}" fill="none" stroke-width="4" />
    <circle cx="23" cy="23" r="${r}" fill="none" stroke="${color}" stroke-width="4" stroke-linecap="round"
      stroke-dasharray="${(c * score) / 100} ${c}" transform="rotate(-90 23 23)" />
    <text x="23" y="27.5" text-anchor="middle">${score}</text></svg>`;
}

// Moon drawn from its elongation: lit limb on the right while waxing (northern sky).
function moonSvg(elong, south) {
  const r = 12;
  const e = ((elong % 360) + 360) % 360;
  const waxing = e < 180;
  const k = (1 - Math.cos((e * Math.PI) / 180)) / 2;
  const rx = Math.abs(Math.cos((e * Math.PI) / 180)) * r;
  let lit = "";
  if (k > 0.02) {
    const side = waxing ? 1 : 0; // outer semicircle sweep
    const crescent = k < 0.5;
    const inner = waxing ? (crescent ? 0 : 1) : crescent ? 1 : 0;
    lit = k > 0.98
      ? `<circle class="lit" cx="0" cy="0" r="${r}" />`
      : `<path class="lit" d="M0 ${-r} A${r} ${r} 0 0 ${side} 0 ${r} A${rx.toFixed(2)} ${r} 0 0 ${inner} 0 ${-r}Z" />`;
  }
  const flip = south ? " scale(-1,1)" : "";
  return `<svg class="moon-icon" viewBox="-13 -13 26 26" aria-hidden="true"><g transform="rotate(0)${flip}">
    <circle class="dark" cx="0" cy="0" r="${r}" />${lit}</g></svg>`;
}

function cloudBar(n) {
  const tl = n.timeline;
  const idx = tl.sun.map((s, i) => (s != null && s <= tl.dark_level ? i : -1)).filter((i) => i >= 0);
  if (!idx.length) return `<div class="cloudbar" aria-hidden="true"></div>`;
  const step = Math.max(1, Math.floor(idx.length / 18));
  const cells = [];
  for (let j = 0; j < idx.length; j += step) {
    const i = idx[j];
    const c = tl.cloud[i];
    const color = c == null ? "var(--line)" : tl.shoot[i] ? "var(--shoot)" : `color-mix(in srgb, var(--cloud) ${Math.round(25 + c * 0.75)}%, transparent)`;
    cells.push(`<i style="background:${color}"></i>`);
  }
  return `<div class="cloudbar" title="Green = shootable; grey = cloud" aria-hidden="true">${cells.join("")}</div>`;
}

// ---------- detail ----------
function renderDetail() {
  const n = state.plan.nights.find((x) => x.date === state.selected);
  const box = $("#detail");
  if (!n) {
    box.innerHTML = "";
    return;
  }
  const target = state.plan.target;
  const hasWindow = !!n.best_window;
  const events = [n.moon.rise && `rises ${n.moon.rise}`, n.moon.set && `sets ${n.moon.set}`].filter(Boolean);
  const moonUp = events.length ? events.join(" · ") : n.moon.up_fraction > 0.5 ? "up all night" : "down all night";
  const tiles = [
    ["Shootable", n.usable_hours ? `${n.usable_hours} h` : "None", n.windows.length ? n.windows.join(", ") : "no window meets your limits"],
    ["Darkness", n.dark_start ? `${n.dark_start}–${n.dark_end}` : "None", n.darkness === "astronomical" ? `${n.dark_hours} h astronomical` : n.darkness === "nautical" ? "nautical twilight only" : "never dark"],
    ["Moon", `${n.moon.illumination}% lit`, `${n.moon.phase} · ${moonUp}`],
    n.target ? [target.short, `${n.target.peak_alt}° peak`, `at ${n.target.peak_time} · ${n.target.hours_above_min} h above ${state.plan.settings.min_alt}°`] : null,
    ["Cloud", n.avg_cloud != null ? `${n.avg_cloud}%` : "—", `low ${n.clouds.low ?? "—"} · mid ${n.clouds.mid ?? "—"} · high ${n.clouds.high ?? "—"}`],
    ["Conditions", n.min_temp != null ? `${n.min_temp}°C low` : "—", `wind ${n.avg_wind ?? "—"} km/h${n.dew_risk ? " · dew risk" : ""}`],
    ["Sun", n.sunset ? `↓ ${n.sunset}` : "—", n.sunrise ? `rises ${n.sunrise}` : ""],
  ].filter(Boolean);
  const icsHref = hasWindow ? `/api/ics?${queryParams({ date: n.date })}` : null;
  const emailReady = state.plan.email_ready;
  box.innerHTML = `
    <div class="detail-head">
      <div>
        <p class="hero-eyebrow"><span class="verdict ${verdictClass(n.verdict)}">${n.verdict} · ${n.score}</span></p>
        <h2>${esc(n.label)}${hasWindow ? ` · best ${esc(n.best_window)}` : ""}</h2>
      </div>
      <div class="detail-actions">
        ${icsHref ? `<a class="btn ghost" href="${icsHref}" download>Add to calendar</a>` : ""}
        <button id="emailBtn" class="btn ghost" type="button" ${emailReady ? "" : 'disabled title="Add SENDER_EMAIL and APP_PASSWORD to .env to enable email"'}>Email forecast</button>
      </div>
    </div>
    <ul class="reasons">${n.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
    <div class="tiles">${tiles.map(([k, v, s]) => `<div class="tile"><span>${esc(k)}</span><b>${esc(v)}</b><small>${esc(s)}</small></div>`).join("")}</div>
    <div>
      <div class="chart-key">
        <span><i style="background:var(--shoot)"></i>Shootable</span>
        ${n.target ? `<span><i style="background:var(--target)"></i>${esc(target.short)} altitude</span>` : ""}
        <span><i style="background:var(--moon)"></i>Moon altitude</span>
        <span><i style="background:var(--cloud)"></i>Cloud cover</span>
        <span><i style="background:var(--rain)"></i>Rain chance</span>
      </div>
      <div class="chart-box">${chartSvg(n)}</div>
    </div>
    <details class="hours"><summary>Hour-by-hour table</summary><div class="table-box">${hourTable(n)}</div></details>`;
  $("#emailBtn").addEventListener("click", sendEmail);
  wireChart(n);
}

const CH = { w: 1000, h: 330, l: 40, r: 14, cloudTop: 22, cloudH: 64, altTop: 110, altH: 170, shootY: 292, axisY: 318 };

function chartSvg(n) {
  const tl = n.timeline;
  const N = tl.t.length;
  if (!N) return "";
  const x = (i) => CH.l + (i / Math.max(N - 1, 1)) * (CH.w - CH.l - CH.r);
  const bw = (CH.w - CH.l - CH.r) / Math.max(N - 1, 1);
  const altY = (a) => CH.altTop + CH.altH - (Math.max(0, Math.min(90, a)) / 90) * CH.altH;
  const cloudY = (c) => CH.cloudTop + CH.cloudH - (c / 100) * CH.cloudH;
  const parts = [];

  // Twilight bands behind the altitude panel.
  const band = (s) => (s == null ? "day" : s > 0 ? "day" : s > -6 ? "civil" : s > -12 ? "naut" : s > -18 ? "astro" : "dark");
  let start = 0;
  for (let i = 1; i <= N; i++) {
    if (i === N || band(tl.sun[i]) !== band(tl.sun[start])) {
      const fill = { day: "--tw-day", civil: "--tw-civil", naut: "--tw-naut", astro: "--tw-astro", dark: "--tw-dark" }[band(tl.sun[start])];
      parts.push(`<rect x="${x(start) - bw / 2}" y="${CH.cloudTop}" width="${(i - start) * bw}" height="${CH.shootY + 10 - CH.cloudTop}" style="fill:var(${fill})" />`);
      start = i;
    }
  }

  // Grid + labels.
  for (const a of [0, 30, 60, 90]) {
    parts.push(`<line class="grid" x1="${CH.l}" x2="${CH.w - CH.r}" y1="${altY(a)}" y2="${altY(a)}" />`);
    parts.push(`<text class="axis" x="${CH.l - 6}" y="${altY(a) + 4}" text-anchor="end">${a}°</text>`);
  }
  parts.push(`<text class="panel-label" x="${CH.l}" y="${CH.cloudTop - 8}">Cloud &amp; rain</text>`);
  parts.push(`<text class="panel-label" x="${CH.l}" y="${CH.altTop - 8}">Altitude</text>`);
  parts.push(`<line class="grid" x1="${CH.l}" x2="${CH.w - CH.r}" y1="${CH.cloudTop + CH.cloudH}" y2="${CH.cloudTop + CH.cloudH}" />`);
  parts.push(`<text class="axis" x="${CH.l - 6}" y="${CH.cloudTop + 8}" text-anchor="end">100%</text>`);

  // Cloud area and rain bars.
  const seg = [];
  tl.cloud.forEach((c, i) => {
    if (c == null) return;
    seg.push(`${x(i).toFixed(1)},${cloudY(c).toFixed(1)}`);
  });
  if (seg.length) {
    const firstI = tl.cloud.findIndex((c) => c != null);
    const lastI = tl.cloud.length - 1 - [...tl.cloud].reverse().findIndex((c) => c != null);
    parts.push(`<polygon points="${x(firstI)},${CH.cloudTop + CH.cloudH} ${seg.join(" ")} ${x(lastI)},${CH.cloudTop + CH.cloudH}" style="fill:var(--cloud);opacity:.55" />`);
  }
  tl.precip.forEach((p, i) => {
    if (!p || p < 5 || i % 3) return;
    const h = (p / 100) * CH.cloudH;
    parts.push(`<rect x="${x(i) - 2}" y="${CH.cloudTop + CH.cloudH - h}" width="4" height="${h}" rx="1" style="fill:var(--rain);opacity:.9" />`);
  });

  // Minimum altitude line, moon and target curves.
  if (tl.target) parts.push(`<line class="minline" x1="${CH.l}" x2="${CH.w - CH.r}" y1="${altY(state.plan.settings.min_alt)}" y2="${altY(state.plan.settings.min_alt)}" />`);
  const curve = (arr) => {
    let d = "";
    let pen = false;
    arr.forEach((a, i) => {
      if (a == null || a < 0) {
        pen = false;
        return;
      }
      d += `${pen ? "L" : "M"}${x(i).toFixed(1)} ${altY(a).toFixed(1)}`;
      pen = true;
    });
    return d;
  };
  const moonOpacity = (0.35 + 0.65 * (n.moon.illumination / 100)).toFixed(2);
  parts.push(`<path d="${curve(tl.moon)}" fill="none" stroke-width="2" stroke-dasharray="1 0" style="stroke:var(--moon);opacity:${moonOpacity}" />`);
  if (tl.target) parts.push(`<path d="${curve(tl.target)}" fill="none" stroke-width="2.6" style="stroke:var(--target)" />`);

  // Shootable strip.
  start = -1;
  for (let i = 0; i <= N; i++) {
    const ok = i < N && tl.shoot[i];
    if (ok && start < 0) start = i;
    if (!ok && start >= 0) {
      parts.push(`<rect x="${x(start) - bw / 2}" y="${CH.shootY}" width="${(i - start) * bw}" height="10" rx="3" style="fill:var(--shoot)" />`);
      start = -1;
    }
  }
  parts.push(`<text class="panel-label" x="${CH.l - 6}" y="${CH.shootY + 9}" text-anchor="end">GO</text>`);

  // Hour labels.
  const every = N > 110 ? 2 : 1;
  let shown = 0;
  tl.labels.forEach((lab, i) => {
    if (!lab.endsWith(":00")) return;
    if (shown++ % every) return;
    parts.push(`<text class="axis" x="${x(i)}" y="${CH.axisY}" text-anchor="middle">${lab.slice(0, 2)}</text>`);
  });

  parts.push(`<line id="cursor" x1="0" x2="0" y1="${CH.cloudTop}" y2="${CH.shootY + 10}" style="stroke:var(--ink);opacity:0" />`);
  parts.push(`<rect id="hit" x="${CH.l}" y="0" width="${CH.w - CH.l - CH.r}" height="${CH.h}" fill="transparent" />`);
  return `<svg class="chart" viewBox="0 0 ${CH.w} ${CH.h}" role="img" aria-label="Night timeline for ${esc(n.label)}">${parts.join("")}</svg>`;
}

function wireChart(n) {
  const svg = $("#detail .chart");
  if (!svg) return;
  const tl = n.timeline;
  const N = tl.t.length;
  const tip = $("#tip");
  const cursor = $("#cursor", svg);
  const show = (e) => {
    const r = svg.getBoundingClientRect();
    const vx = ((e.clientX - r.left) / r.width) * CH.w;
    const i = Math.round(((vx - CH.l) / (CH.w - CH.l - CH.r)) * (N - 1));
    if (i < 0 || i >= N) return hide();
    const px = CH.l + (i / Math.max(N - 1, 1)) * (CH.w - CH.l - CH.r);
    cursor.setAttribute("x1", px);
    cursor.setAttribute("x2", px);
    cursor.style.opacity = 0.5;
    const row = (k, v) => `<div class="row"><span>${k}</span><span>${v}</span></div>`;
    const f = (v, u = "") => (v == null ? "—" : `${Math.round(v)}${u}`);
    tip.innerHTML = `<b>${tl.labels[i]}</b> ${tl.shoot[i] ? '<span class="verdict v-excellent">· shootable</span>' : ""}
      ${row("Sun", f(tl.sun[i], "°"))}
      ${tl.target ? row(esc(state.plan.target.short), f(tl.target[i], "°")) : ""}
      ${row("Moon", f(tl.moon[i], "°"))}
      ${row("Cloud", `${f(tl.cloud[i], "%")} <small class="muted">(L${f(tl.low[i])} M${f(tl.mid[i])} H${f(tl.high[i])})</small>`)}
      ${row("Rain", f(tl.precip[i], "%"))}
      ${row("Temp / dew", `${f(tl.temp[i], "°")} / ${f(tl.dew[i], "°")}`)}
      ${row("Wind", f(tl.wind[i], " km/h"))}`;
    tip.hidden = false;
    const tw = tip.offsetWidth;
    const th = tip.offsetHeight;
    let left = e.clientX + 14;
    if (left + tw > window.innerWidth - 8) left = e.clientX - tw - 14;
    tip.style.left = `${Math.max(8, left)}px`;
    tip.style.top = `${Math.max(8, Math.min(window.innerHeight - th - 8, e.clientY - th / 2))}px`;
  };
  const hide = () => {
    tip.hidden = true;
    cursor.style.opacity = 0;
  };
  svg.addEventListener("pointermove", show);
  svg.addEventListener("pointerdown", show);
  svg.addEventListener("pointerleave", hide);
}

function hourTable(n) {
  const tl = n.timeline;
  const rows = [];
  tl.labels.forEach((lab, i) => {
    if (!lab.endsWith(":00") || tl.sun[i] > 0) return;
    const f = (v, u = "") => (v == null ? "—" : `${Math.round(v)}${u}`);
    rows.push(`<tr class="${tl.shoot[i] ? "ok" : ""}"><td>${lab}</td><td>${f(tl.sun[i], "°")}</td>${tl.target ? `<td>${f(tl.target[i], "°")}</td>` : ""}<td>${f(tl.moon[i], "°")}</td><td>${f(tl.cloud[i], "%")}</td><td>${f(tl.low[i])}/${f(tl.mid[i])}/${f(tl.high[i])}</td><td>${f(tl.precip[i], "%")}</td><td>${f(tl.temp[i], "°")}</td><td>${f(tl.dew[i], "°")}</td><td>${f(tl.wind[i])}</td><td>${tl.shoot[i] ? "✓" : ""}</td></tr>`);
  });
  const tcol = tl.target ? `<th>${esc(state.plan.target.short)}</th>` : "";
  return `<table><thead><tr><th>Time</th><th>Sun</th>${tcol}<th>Moon</th><th>Cloud</th><th>L/M/H</th><th>Rain</th><th>Temp</th><th>Dew</th><th>Wind</th><th>Go</th></tr></thead><tbody>${rows.join("")}</tbody></table>`;
}

async function sendEmail() {
  const btn = $("#emailBtn");
  btn.disabled = true;
  btn.textContent = "Sending…";
  try {
    const r = await api("/api/email", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.fromEntries(queryParams())),
    });
    toast(`Forecast emailed to ${r.to}`);
  } catch (err) {
    toast(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Email forecast";
  }
}

// ---------- location search ----------
const input = $("#locInput");
const list = $("#locList");
let results = [];
let active = -1;
let searchTimer;

input.addEventListener("input", () => {
  clearTimeout(searchTimer);
  const q = input.value.trim();
  if (q.length < 2) return closeList();
  searchTimer = setTimeout(async () => {
    try {
      const data = await api(`/api/search?q=${encodeURIComponent(q)}`);
      if (input.value.trim() !== q) return;
      results = data.results;
      active = results.length ? 0 : -1;
      drawList(q);
    } catch (err) {
      results = [];
      list.innerHTML = `<li aria-disabled="true">${esc(err.message)}</li>`;
      openList();
    }
  }, 250);
});
input.addEventListener("keydown", (e) => {
  if (list.hidden) return;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    active = (active + (e.key === "ArrowDown" ? 1 : -1) + results.length) % results.length;
    drawList();
  } else if (e.key === "Enter" && results[active]) {
    e.preventDefault();
    choose(results[active]);
  } else if (e.key === "Escape") {
    closeList();
  }
});
input.addEventListener("blur", () => setTimeout(closeList, 150));
input.addEventListener("focus", () => input.select());

function drawList(q) {
  if (!results.length) {
    list.innerHTML = `<li aria-disabled="true">No places match “${esc(q || input.value)}”.</li>`;
  } else {
    list.innerHTML = results
      .map((r, i) => `<li id="opt${i}" role="option" aria-selected="${i === active}" data-i="${i}">${esc(r.name)}<small>${esc(r.label)} · ${r.lat.toFixed(2)}, ${r.lon.toFixed(2)}</small></li>`)
      .join("");
    input.setAttribute("aria-activedescendant", active >= 0 ? `opt${active}` : "");
  }
  openList();
}
function openList() {
  list.hidden = false;
  input.setAttribute("aria-expanded", "true");
}
function closeList() {
  list.hidden = true;
  input.setAttribute("aria-expanded", "false");
}
list.addEventListener("mousedown", (e) => {
  const li = e.target.closest("li[data-i]");
  if (li) {
    e.preventDefault();
    choose(results[Number(li.dataset.i)]);
  }
});

function choose(loc) {
  closeList();
  setLocation({ label: loc.label, lat: loc.lat, lon: loc.lon, elevation: loc.elevation, timezone: loc.timezone });
}

function setLocation(loc) {
  state.location = loc;
  store.set("location", loc);
  input.value = loc.label;
  input.blur();
  runForecast();
}

$("#geoBtn").addEventListener("click", () => {
  if (!navigator.geolocation) return toast("This browser can't share its location.");
  const btn = $("#geoBtn");
  btn.disabled = true;
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      btn.disabled = false;
      const { latitude: lat, longitude: lon, altitude } = pos.coords;
      setLocation({ label: `My location (${lat.toFixed(2)}, ${lon.toFixed(2)})`, lat, lon, elevation: altitude || 0, timezone: null });
    },
    (err) => {
      btn.disabled = false;
      toast(err.code === 1 ? "Location permission denied. Search for a city instead." : "Couldn't get your location.");
    },
    { enableHighAccuracy: false, timeout: 10000 }
  );
});

// ---------- target ----------
async function loadTargets() {
  const data = await api("/api/targets");
  state.targets = data.targets;
  state.defaults = data.defaults;
  if (!state.settings) state.settings = { ...data.defaults };
  const sel = $("#targetSelect");
  const groups = {};
  for (const t of data.targets) (groups[t.type] ||= []).push(t);
  sel.innerHTML = Object.entries(groups)
    .map(([type, ts]) => `<optgroup label="${esc(type === "Any" ? "Sky only" : type)}">${ts.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("")}</optgroup>`)
    .join("");
  if (!data.targets.some((t) => t.id === state.target)) state.target = "M31";
  sel.value = state.target;
}
$("#targetSelect").addEventListener("change", (e) => {
  state.target = e.target.value;
  store.set("target", state.target);
  runForecast();
});

// ---------- saved locations ----------
const sameSpot = (a, b) => Math.abs(a.lat - b.lat) < 1e-3 && Math.abs(a.lon - b.lon) < 1e-3;

async function loadSaved() {
  try {
    state.saved = (await api("/api/locations")).locations;
  } catch {
    state.saved = [];
  }
  renderSaved();
}

function renderSaved() {
  const box = $("#saved");
  box.innerHTML = "";
  for (const s of state.saved) {
    const chip = document.createElement("span");
    chip.className = `chip${state.location && sameSpot(s, state.location) ? " on" : ""}`;
    const short = s.label.split(",")[0];
    const twin = state.saved.some((o) => o !== s && o.label.split(",")[0] === short);
    const name = twin ? s.label.split(",").slice(0, 2).join(",") : short;
    chip.innerHTML = `<button class="chip-main" type="button" title="${esc(s.label)}">${esc(name)}</button>${s.bortle ? `<span class="b">B${s.bortle}</span>` : ""}<button class="x" type="button" aria-label="Remove ${esc(s.label)}">×</button>`;
    $(".chip-main", chip).addEventListener("click", () => setLocation({ label: s.label, lat: s.lat, lon: s.lon, elevation: s.elevation, timezone: s.timezone }));
    $(".x", chip).addEventListener("click", async () => {
      state.saved = (await api(`/api/locations/${s.id}`, { method: "DELETE" })).locations;
      renderSaved();
      if (state.plan) renderHero();
    });
    box.appendChild(chip);
  }
}

async function saveLocation(bortle) {
  const loc = state.plan.location;
  const body = { label: loc.label, lat: loc.lat, lon: loc.lon, elevation: loc.elevation, timezone: loc.timezone, bortle };
  state.saved = (await api("/api/locations", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).locations;
}

async function toggleSave() {
  const loc = state.plan.location;
  const existing = state.saved.find((s) => sameSpot(s, loc));
  try {
    if (existing) {
      state.saved = (await api(`/api/locations/${existing.id}`, { method: "DELETE" })).locations;
      toast("Location removed");
    } else {
      await saveLocation(loc.bortle ?? null);
      toast("Location saved");
    }
  } catch (err) {
    toast(err.message);
  }
  renderSaved();
  renderHero();
}

// ---------- limits drawer ----------
const drawer = $("#drawer");
function syncSliders() {
  $$(".slider input").forEach((el) => {
    el.value = state.settings[el.dataset.key];
    $(`#o_${el.dataset.key}`).textContent = `${el.value}${el.dataset.key === "min_alt" ? "°" : "%"}`;
  });
  $("#bortleSelect").value = state.plan?.location.bortle ?? "";
  $("#bortleLoc").textContent = state.plan ? state.plan.location.label.split(",")[0] : "this location";
  $("#bortleSelect").disabled = !state.plan;
}
function openDrawer(focusSel) {
  syncSliders();
  drawer.hidden = false;
  ($(focusSel || ".slider input") || drawer).focus();
}
function closeDrawer() {
  drawer.hidden = true;
}
$$(".slider input").forEach((el) =>
  el.addEventListener("input", () => {
    $(`#o_${el.dataset.key}`).textContent = `${el.value}${el.dataset.key === "min_alt" ? "°" : "%"}`;
  })
);
$("#settingsBtn").addEventListener("click", () => openDrawer());
$("#drawerClose").addEventListener("click", closeDrawer);
drawer.addEventListener("click", (e) => e.target === drawer && closeDrawer());
$("#resetLimits").addEventListener("click", () => {
  state.settings = { ...state.defaults };
  syncSliders();
});
$("#applyLimits").addEventListener("click", async () => {
  const next = {};
  $$(".slider input").forEach((el) => (next[el.dataset.key] = Number(el.value)));
  state.settings = next;
  store.set("settings", next);
  const bortle = $("#bortleSelect").value;
  closeDrawer();
  if (state.plan && String(state.plan.location.bortle ?? "") !== bortle) {
    try {
      await saveLocation(bortle ? Number(bortle) : null);
      toast(bortle ? `Saved ${state.plan.location.label.split(",")[0]} as Bortle ${bortle}` : "Bortle cleared");
    } catch (err) {
      toast(err.message);
    }
  }
  runForecast();
});

// ---------- history ----------
$("#historyBtn").addEventListener("click", async () => {
  const dlg = $("#historyDlg");
  const body = $("#historyBody");
  body.innerHTML = `<p class="muted">Loading…</p>`;
  dlg.showModal();
  try {
    const { runs } = await api("/api/history");
    if (!runs.length) {
      body.innerHTML = `<p class="muted">No forecasts yet.</p>`;
      return;
    }
    body.innerHTML = `<table><thead><tr><th>When</th><th>Location</th><th>Target</th><th>Good nights</th><th>Best</th></tr></thead><tbody>${runs
      .map(
        (r, i) => `<tr data-i="${i}"><td>${esc(new Date(r.timestamp.replace(" ", "T") + "Z").toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }))}</td><td>${esc(r.location)}</td><td>${esc(r.target)}</td><td>${r.good_nights ?? "—"}</td><td>${r.best_date ? `${esc(r.best_date.slice(5))} ${esc(r.best_window)} (${r.best_score})` : "—"}</td></tr>`
      )
      .join("")}</tbody></table>`;
    $$("tr[data-i]", body).forEach((tr) =>
      tr.addEventListener("click", () => {
        const r = runs[Number(tr.dataset.i)];
        const t = state.targets.find((x) => x.name === r.target);
        if (t) {
          state.target = t.id;
          $("#targetSelect").value = t.id;
          store.set("target", t.id);
        }
        dlg.close();
        setLocation({ label: r.location, lat: r.lat, lon: r.lon, elevation: 0, timezone: null });
      })
    );
  } catch (err) {
    body.innerHTML = `<p class="muted">${esc(err.message)}</p>`;
  }
});
$$("[data-close]").forEach((b) => b.addEventListener("click", () => b.closest("dialog").close()));

// ---------- night vision ----------
function setRed(on) {
  document.documentElement.classList.toggle("red", on);
  $("#redBtn").setAttribute("aria-pressed", String(on));
  document.querySelector('meta[name="theme-color"]').content = on ? "#000000" : "#070a12";
  store.set("red", on);
}
$("#redBtn").addEventListener("click", () => setRed(!document.documentElement.classList.contains("red")));
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !drawer.hidden) closeDrawer();
});

// ---------- boot ----------
(async function boot() {
  setRed(store.get("red", false));
  try {
    await Promise.all([loadTargets(), loadSaved()]);
  } catch (err) {
    status("error", `Can't reach the local server: ${err.message}`);
    return;
  }
  if (state.location) {
    input.value = state.location.label;
    runForecast();
  } else {
    status("info", "Search for a city (or use your location) to see the best astrophotography nights ahead.");
    input.focus();
  }
})();
