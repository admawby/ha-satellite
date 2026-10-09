/* HA Satellite — ingress UI. Plain JS, no build step. All URLs are relative (ingress path). */
"use strict";

const BASE = location.pathname.endsWith("/") ? location.pathname : location.pathname + "/";
const $ = (sel, root = document) => root.querySelector(sel);
const view = $("#view");
const modal = $("#modal");
const modalBody = $("#modal-body");

const state = { controller: null, satellites: [], current: null, tab: "overview", timers: [] };
let term = null, termSocket = null, termFit = null, termResize = null;

// ------------------------------------------------------------------ helpers
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtPct = (v) => (v == null ? "–" : `${Math.round(v)}%`);
const fmtTemp = (v) => (v == null ? "–" : `${v.toFixed(1)}°C`);
const fmtBytes = (b) => { if (!b) return "–"; const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; while (b >= 1024 && i < 4) { b /= 1024; i++; } return `${b.toFixed(i ? 1 : 0)} ${u[i]}`; };
const fmtDur = (s) => { if (s == null) return "–"; const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60); return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`; };
const fmtAgo = (ts) => { if (!ts) return "never"; const s = Date.now() / 1000 - ts; return s < 90 ? "just now" : `${fmtDur(s)} ago`; };
const level = (v, warn, bad) => (v == null ? "" : v >= bad ? "bad" : v >= warn ? "warn" : "");

function toast(msg, ms = 2600) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("show"), ms);
}

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers["Content-Type"] = "application/json"; }
  const res = await fetch(BASE + path, init);
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok) throw new Error((data && data.error) || `${res.status} ${res.statusText}`);
  return data;
}
const satApi = (path, opts) => api(`api/sat/${state.current}/proxy/${path}`, opts);

function clearTimers() { state.timers.forEach(clearInterval); state.timers = []; }
function every(ms, fn) { state.timers.push(setInterval(fn, ms)); }
function closeTerminal() {
  if (termSocket) { termSocket.onclose = null; termSocket.close(); termSocket = null; }
  if (termResize) { window.removeEventListener("resize", termResize); termResize = null; }
  if (term) { term.dispose(); term = null; }
}

async function refresh() {
  const data = await api("api/state");
  state.controller = data.controller;
  state.satellites = data.satellites;
}
const currentSat = () => state.satellites.find((s) => s.id === state.current);

// ------------------------------------------------------------------ routing
function go(satId = null, tab = "overview") {
  clearTimers(); closeTerminal();
  state.current = satId; state.tab = tab;
  render();
}

async function render() {
  try { await refresh(); } catch (e) { view.innerHTML = `<div class="card bad">Failed to load: ${esc(e.message)}</div>`; return; }
  if (state.current && currentSat()) renderDetail(); else { state.current = null; renderList(); }
}

// ------------------------------------------------------------------ list view
function satCard(s) {
  const m = s.metrics || {}, upd = m.updates || {}, thr = m.throttled || {};
  const chips = [];
  if (!s.online) chips.push(`<span class="chip bad">offline${s.last_error ? " · " + esc(s.last_error.slice(0, 60)) : ""}</span>`);
  if (upd.available) chips.push(`<span class="chip warn">${upd.available} update${upd.available > 1 ? "s" : ""}</span>`);
  if (upd.reboot_required) chips.push(`<span class="chip warn">reboot required</span>`);
  if (thr.under_voltage_now) chips.push(`<span class="chip bad">under-voltage</span>`);
  if (thr.throttled_now) chips.push(`<span class="chip bad">throttled</span>`);
  const bridges = (m.serial || []).filter((b) => b.enabled);
  if (bridges.length) chips.push(`<span class="chip ok">${bridges.length} radio bridge${bridges.length > 1 ? "s" : ""}</span>`);
  if (m.agent_version && state.controller && m.agent_version !== state.controller.agent_version) chips.push(`<span class="chip">agent ${esc(m.agent_version)}</span>`);
  return `
  <div class="card sat-card" data-id="${esc(s.id)}">
    <div class="sat-head"><span class="dot ${s.online ? "on" : ""}"></span><h3>${esc(s.name)}</h3><span class="muted mono">${esc(s.host)}</span></div>
    <div class="muted">${esc(m.model || s.hostname || "")}</div>
    <div class="stats">
      <div class="stat"><div class="v ${level(m.cpu_temp, 65, 80)}">${fmtTemp(m.cpu_temp)}</div><div class="l">CPU temp</div></div>
      <div class="stat"><div class="v">${fmtPct(m.cpu_percent)}</div><div class="l">CPU</div></div>
      <div class="stat"><div class="v ${level(m.mem_percent, 80, 92)}">${fmtPct(m.mem_percent)}</div><div class="l">Memory</div></div>
      <div class="stat"><div class="v ${level(m.disk_percent, 80, 92)}">${fmtPct(m.disk_percent)}</div><div class="l">Disk</div></div>
    </div>
    <div class="chips">${chips.join("")}<span class="chip">up ${fmtDur(m.uptime)}</span></div>
  </div>`;
}

function renderList() {
  if (!state.satellites.length) {
    view.innerHTML = `
      <div class="card empty">
        <h2>No satellites yet</h2>
        <p class="muted">Adopt a Raspberry Pi on your network to use its USB radios, monitor it and manage it from here.</p>
        <button class="btn primary" id="empty-add">Add satellite</button>
      </div>`;
    $("#empty-add").onclick = openAdd;
  } else {
    view.innerHTML = `<div class="grid">${state.satellites.map(satCard).join("")}</div>`;
    view.querySelectorAll(".sat-card").forEach((el) => (el.onclick = () => go(el.dataset.id)));
  }
  every(10000, async () => { if (!state.current) { await refresh().catch(() => {}); if (!state.current && !modal.open) renderListQuiet(); } });
}
function renderListQuiet() {
  if (!state.satellites.length) return;
  view.innerHTML = `<div class="grid">${state.satellites.map(satCard).join("")}</div>`;
  view.querySelectorAll(".sat-card").forEach((el) => (el.onclick = () => go(el.dataset.id)));
}

// ------------------------------------------------------------------ add / adopt
function openAdd() {
  const c = state.controller || {};
  modalBody.innerHTML = `
    <h3>Add a satellite</h3>
    <div class="tabs"><button type="button" class="tab active" data-t="ssh">Adopt over SSH</button><button type="button" class="tab" data-t="manual">Install command</button></div>
    <div id="add-ssh">
      <p class="muted">The add-on logs in once, installs the agent and enrolls the Pi. Credentials are used for this session only and are never stored.</p>
      <div class="two">
        <label class="field"><span>Pi address</span><input type="text" id="a-host" placeholder="192.168.1.50" required></label>
        <label class="field"><span>SSH port</span><input type="number" id="a-port" value="22"></label>
        <label class="field"><span>Username</span><input type="text" id="a-user" value="pi" autocomplete="off"></label>
        <label class="field"><span>Password (also used for sudo)</span><input type="password" id="a-pass" autocomplete="new-password"></label>
      </div>
      <label class="field"><span>Name in Home Assistant (optional)</span><input type="text" id="a-name" placeholder="Garage Pi"></label>
      <details><summary class="muted">Use an SSH private key instead</summary>
        <label class="field" style="margin-top:8px"><span>Private key (OpenSSH/PEM). Password field is used as passphrase and for sudo.</span><textarea id="a-key" rows="5" spellcheck="false"></textarea></label>
      </details>
      <pre class="out" id="a-log" hidden></pre>
    </div>
    <div id="add-manual" hidden>
      <p class="muted">Run this on the Pi. It is valid for 30 minutes and can be used once. The download is pinned to this add-on's certificate.</p>
      <label class="field"><span>Name in Home Assistant (optional)</span><input type="text" id="m-name" placeholder="Garage Pi"></label>
      <div id="m-out"></div>
      <p class="muted">Enrollment endpoint: <code>${esc(c.enroll_host)}:${esc(c.enroll_port)}</code> · CA <code>${esc((c.ca_fingerprint || "").slice(0, 23))}…</code></p>
    </div>
    <div class="modal-actions">
      <button type="button" class="btn" id="add-close">Close</button>
      <button type="button" class="btn primary" id="add-go">Adopt</button>
    </div>`;
  let mode = "ssh";
  modalBody.querySelectorAll(".tab").forEach((t) => (t.onclick = () => {
    mode = t.dataset.t;
    modalBody.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === t));
    $("#add-ssh").hidden = mode !== "ssh"; $("#add-manual").hidden = mode !== "manual";
    $("#add-go").textContent = mode === "ssh" ? "Adopt" : "Generate command";
  }));
  $("#add-close").onclick = () => { modal.close(); render(); };
  $("#add-go").onclick = () => (mode === "ssh" ? startAdopt() : makeCommand());
  modal.showModal();
}

async function makeCommand() {
  try {
    const r = await api("api/enroll", { method: "POST", body: { name: $("#m-name").value } });
    $("#m-out").innerHTML = `<div class="cmdbox"><code id="m-cmd">${esc(r.command)}</code><button type="button" class="btn" id="m-copy">Copy</button></div>
      <p class="muted">Expires ${new Date(r.expires * 1000).toLocaleTimeString()}. The satellite appears here automatically once the script finishes.</p>`;
    $("#m-copy").onclick = () => navigator.clipboard.writeText(r.command).then(() => toast("Copied"));
  } catch (e) { toast(e.message); }
}

async function startAdopt() {
  const body = {
    host: $("#a-host").value.trim(), port: +$("#a-port").value || 22, username: $("#a-user").value.trim(),
    password: $("#a-pass").value, private_key: $("#a-key").value, name: $("#a-name").value.trim(),
  };
  if (!body.host || !body.username) return toast("Address and username are required");
  const go_ = $("#add-go"); go_.disabled = true;
  const log = $("#a-log"); log.hidden = false; log.textContent = "Starting…";
  try {
    const job = await api("api/adopt", { method: "POST", body });
    $("#a-pass").value = ""; $("#a-key").value = "";
    const poll = setInterval(async () => {
      try {
        const j = await api(`api/jobs/${job.id}`);
        log.textContent = j.log.join("\n"); log.scrollTop = log.scrollHeight;
        if (j.status !== "running") {
          clearInterval(poll); go_.disabled = false;
          if (j.status === "success") { toast("Satellite adopted"); setTimeout(() => { modal.close(); go(j.satellite_id); }, 1200); }
        }
      } catch (e) { clearInterval(poll); go_.disabled = false; toast(e.message); }
    }, 1000);
  } catch (e) { log.textContent = "ERROR: " + e.message; go_.disabled = false; }
}

// ------------------------------------------------------------------ detail
const TABS = [["overview", "Overview"], ["radios", "USB radios"], ["terminal", "Terminal"], ["updates", "Updates"], ["logs", "Logs"], ["settings", "Settings"]];

function renderDetail() {
  const s = currentSat();
  view.innerHTML = `
    <div class="detail-head">
      <button class="btn small" id="back">← All</button>
      <span class="dot ${s.online ? "on" : ""}"></span><h2>${esc(s.name)}</h2>
      <span class="muted mono">${esc(s.host)} · ${esc(s.id)}</span>
      <span class="spacer"></span>
      <span class="muted">${s.online ? "online" : "offline · last seen " + fmtAgo(s.last_seen)}</span>
    </div>
    <nav class="tabs">${TABS.map(([k, l]) => `<button class="tab ${k === state.tab ? "active" : ""}" data-t="${k}">${l}</button>`).join("")}</nav>
    <section id="tab"></section>`;
  $("#back").onclick = () => go();
  view.querySelectorAll("nav .tab").forEach((t) => (t.onclick = () => go(state.current, t.dataset.t)));
  const el = $("#tab");
  if (!s.online && !["settings", "overview"].includes(state.tab)) {
    el.innerHTML = `<div class="card">Satellite is offline${s.last_error ? `: <span class="bad">${esc(s.last_error)}</span>` : ""}.</div>`;
    return;
  }
  ({ overview: tabOverview, radios: tabRadios, terminal: tabTerminal, updates: tabUpdates, logs: tabLogs, settings: tabSettings })[state.tab](el, s);
}

function bar(v, warn, bad) { return `<div class="bar"><i class="${level(v, warn, bad)}" style="width:${Math.min(100, v || 0)}%"></i></div>`; }

function tabOverview(el, s) {
  const draw = () => {
    const sat = currentSat(); const m = sat.metrics || {}; const thr = m.throttled; const upd = m.updates || {};
    const addrs = Object.entries(m.addresses || {}).map(([k, v]) => `${esc(k)}: ${v.map(esc).join(", ")}`).join("<br>");
    el.innerHTML = `
      <div class="two">
        <div class="card">
          <h4>Health</h4>
          <div class="stats">
            <div class="stat"><div class="v ${level(m.cpu_temp, 65, 80)}">${fmtTemp(m.cpu_temp)}</div><div class="l">CPU temp</div></div>
            <div class="stat"><div class="v">${fmtPct(m.cpu_percent)}</div><div class="l">CPU ${m.cpu_freq_mhz ? "· " + m.cpu_freq_mhz + " MHz" : ""}</div></div>
            <div class="stat"><div class="v">${(m.load || ["–"])[0]}</div><div class="l">Load 1m</div></div>
            <div class="stat"><div class="v">${fmtDur(m.uptime)}</div><div class="l">Uptime</div></div>
          </div>
          <div class="section" style="margin-top:14px">
            <div class="row"><span>Memory</span><span class="spacer"></span><span class="muted">${fmtBytes(m.mem_used)} / ${fmtBytes(m.mem_total)}</span></div>${bar(m.mem_percent, 80, 92)}
            <div class="row" style="margin-top:8px"><span>Disk /</span><span class="spacer"></span><span class="muted">${fmtBytes(m.disk_used)} / ${fmtBytes(m.disk_total)}</span></div>${bar(m.disk_percent, 80, 92)}
          </div>
          <div class="section">
            <h4 style="margin-top:14px">Power &amp; throttling</h4>
            ${thr ? `<div class="chips">
              <span class="chip ${thr.under_voltage_now ? "bad" : "ok"}">under-voltage ${thr.under_voltage_now ? "NOW" : "no"}</span>
              <span class="chip ${thr.throttled_now ? "bad" : "ok"}">throttled ${thr.throttled_now ? "NOW" : "no"}</span>
              <span class="chip ${thr.soft_temp_limit_now ? "warn" : "ok"}">temp limit ${thr.soft_temp_limit_now ? "NOW" : "no"}</span>
              ${thr.under_voltage_occurred ? '<span class="chip warn">under-voltage since boot</span>' : ""}
              ${thr.throttled_occurred ? '<span class="chip warn">throttled since boot</span>' : ""}
              <span class="chip mono">${esc(thr.raw)}</span></div>` : '<span class="muted">Not a Raspberry Pi firmware (no throttle data).</span>'}
          </div>
        </div>
        <div class="card">
          <h4>System</h4>
          <dl class="kv">
            <dt>Model</dt><dd>${esc(m.model)}</dd>
            <dt>OS</dt><dd>${esc(m.os)}</dd>
            <dt>Kernel</dt><dd>${esc(m.kernel)} (${esc(m.arch)})</dd>
            <dt>Hostname</dt><dd>${esc(m.hostname || sat.hostname)}</dd>
            <dt>Addresses</dt><dd class="mono">${addrs || "–"}</dd>
            <dt>Agent</dt><dd>${esc(m.agent_version || "–")}${state.controller && m.agent_version && m.agent_version !== state.controller.agent_version ? ` <span class="warn">(add-on ships ${esc(state.controller.agent_version)})</span>` : ""}</dd>
            <dt>Package updates</dt><dd>${upd.available ?? "–"} available · checked ${fmtAgo(upd.last_check)}${upd.reboot_required ? ' · <span class="warn">reboot required</span>' : ""}</dd>
            <dt>Radio bridges</dt><dd>${(m.serial || []).map((b) => `${esc(b.name || b.device)} → :${b.port} ${b.listening ? '<span class="ok">●</span>' : '<span class="bad">●</span>'}`).join("<br>") || "none"}</dd>
          </dl>
          ${Object.keys(m.errors || {}).length ? `<p class="bad">${Object.entries(m.errors).map(([k, v]) => `${esc(k)}: ${esc(v)}`).join("<br>")}</p>` : ""}
          <div class="row" style="margin-top:12px">
            <button class="btn" id="o-term">Open terminal</button>
            <button class="btn" id="o-reboot">Reboot</button>
          </div>
        </div>
      </div>`;
    $("#o-term").onclick = () => go(state.current, "terminal");
    $("#o-reboot").onclick = () => reboot();
  };
  draw();
  every(10000, async () => { await refresh().catch(() => {}); if (state.tab === "overview" && currentSat()) draw(); });
}

async function reboot() {
  if (!confirm(`Reboot ${currentSat().name}?`)) return;
  try { await satApi("system/reboot", { method: "POST" }); toast("Rebooting…"); } catch (e) { toast(e.message); }
}

// ------------------------------------------------------------------ radios
async function tabRadios(el, s) {
  el.innerHTML = `<div class="card muted">Scanning USB devices…</div>`;
  let usb, bridges;
  try { [usb, bridges] = await Promise.all([satApi("usb"), satApi("serial")]); } catch (e) { el.innerHTML = `<div class="card bad">${esc(e.message)}</div>`; return; }
  const host = s.host;
  const used = new Set(bridges.map((b) => b.device));
  const nextPort = () => { const ports = new Set(bridges.map((b) => +b.port)); let p = 20108; while (ports.has(p)) p++; return p; };

  const draw = () => {
    el.innerHTML = `
      <div class="card section">
        <h4>Detected serial radios</h4>
        ${usb.serial.length ? `<table><thead><tr><th>Device</th><th>Identified as</th><th></th></tr></thead><tbody>
          ${usb.serial.map((d, i) => `<tr>
            <td><div class="mono">${esc(d.path)}</div><div class="muted">${esc([d.manufacturer, d.product].filter(Boolean).join(" · "))} ${d.vid ? `<span class="mono">(${esc(d.vid)}:${esc(d.pid)})</span>` : ""}</div></td>
            <td>${esc(d.hint || (d.usb ? "USB serial device" : "On-board UART"))}</td>
            <td style="text-align:right">${used.has(d.path) ? '<span class="ok">bridged</span>' : `<button class="btn small" data-add="${i}">Share over network</button>`}</td></tr>`).join("")}
          </tbody></table>` : `<p class="muted">No USB serial devices found. Plug in a Z-Wave or Zigbee stick and refresh.</p>`}
        <div class="row" style="margin-top:10px"><button class="btn small" id="r-refresh">Refresh</button></div>
      </div>
      <div class="card section">
        <h4>Network bridges (ser2net)</h4>
        <p class="muted">Each bridge exposes one radio as a TCP port. The firewall only lets this Home Assistant host connect.</p>
        <table><thead><tr><th>Name</th><th>Device</th><th style="width:110px">Port</th><th style="width:120px">Baud</th><th>RTS/CTS</th><th>On</th><th>Status</th><th></th></tr></thead>
          <tbody>${bridges.map((b, i) => `<tr>
            <td><input type="text" data-f="name" data-i="${i}" value="${esc(b.name)}" placeholder="zwave"></td>
            <td class="mono" style="max-width:260px;overflow-wrap:anywhere">${esc(b.device)}</td>
            <td><input type="number" data-f="port" data-i="${i}" value="${b.port}"></td>
            <td><select data-f="baud" data-i="${i}">${[57600, 115200, 230400, 460800].map((r) => `<option ${+b.baud === r ? "selected" : ""}>${r}</option>`).join("")}</select></td>
            <td><input type="checkbox" data-f="rtscts" data-i="${i}" ${b.rtscts ? "checked" : ""}></td>
            <td><input type="checkbox" data-f="enabled" data-i="${i}" ${b.enabled ? "checked" : ""}></td>
            <td>${b.listening === undefined ? '<span class="muted">unsaved</span>' : b.listening ? `<span class="ok">listening</span>${b.clients && b.clients.length ? `<div class="muted">client ${b.clients.map(esc).join(", ")}</div>` : ""}` : `<span class="bad">${b.present === false ? "device missing" : "not listening"}</span>`}</td>
            <td><button class="btn small danger" data-del="${i}">✕</button></td></tr>`).join("") || `<tr><td colspan="8" class="muted">No bridges yet — use “Share over network” above.</td></tr>`}</tbody></table>
        <div class="row" style="margin-top:12px"><span class="spacer"></span><button class="btn primary" id="r-save">Save &amp; apply</button></div>
      </div>
      ${bridges.filter((b) => b.listening).length ? `<div class="card section"><h4>Connect your integrations</h4>
        <table><thead><tr><th>Bridge</th><th>Z-Wave JS</th><th>Zigbee2MQTT</th><th>ZHA</th></tr></thead><tbody>
        ${bridges.filter((b) => b.listening).map((b) => `<tr><td>${esc(b.name || b.device)}</td>
          <td><span class="conn">tcp://${esc(host)}:${b.port}</span></td>
          <td><span class="conn">tcp://${esc(host)}:${b.port}</span></td>
          <td><span class="conn">socket://${esc(host)}:${b.port}</span></td></tr>`).join("")}
        </tbody></table>
        <p class="muted">Z-Wave JS: set this as the device path. Zigbee2MQTT: <code>serial.port</code> (plus <code>adapter</code>). ZHA: choose “Enter manually” when adding the integration. Give the Pi a fixed IP (DHCP reservation) so the address never changes.</p></div>` : ""}`;

    el.querySelectorAll("[data-add]").forEach((btn) => (btn.onclick = () => {
      const d = usb.serial[+btn.dataset.add];
      const hint = (d.hint || "").toLowerCase();
      bridges.push({ name: hint.includes("z-wave") ? "zwave" : "zigbee", device: d.path, port: nextPort(), baud: 115200, rtscts: false, enabled: true });
      used.add(d.path); draw();
    }));
    el.querySelectorAll("[data-del]").forEach((btn) => (btn.onclick = () => { const b = bridges.splice(+btn.dataset.del, 1)[0]; used.delete(b.device); draw(); }));
    el.querySelectorAll("[data-f]").forEach((inp) => (inp.onchange = () => {
      const b = bridges[+inp.dataset.i], f = inp.dataset.f;
      b[f] = inp.type === "checkbox" ? inp.checked : (f === "port" || f === "baud") ? +inp.value : inp.value;
    }));
    $("#r-refresh").onclick = () => tabRadios(el, s);
    $("#r-save").onclick = async () => {
      try {
        const payload = bridges.map(({ name, device, port, baud, rtscts, enabled }) => ({ name, device, port, baud, rtscts, enabled }));
        const r = await satApi("serial", { method: "PUT", body: { bridges: payload } });
        bridges = r.bridges;
        toast(Object.keys(r.errors || {}).length ? "Saved with errors: " + Object.values(r.errors).join("; ") : "Bridges applied");
        draw();
      } catch (e) { toast(e.message, 5000); }
    };
  };
  draw();
}

// ------------------------------------------------------------------ terminal
function tabTerminal(el, s) {
  el.innerHTML = `
    <div class="card section">
      <div class="cli"><input type="text" id="cli" placeholder="Run a one-off command, e.g.  vcgencmd measure_temp" spellcheck="false"><button class="btn" id="cli-run">Run</button></div>
      <pre class="out" id="cli-out" hidden></pre>
    </div>
    <div class="card section">
      <div class="row" style="margin-bottom:8px"><h4 style="margin:0">Interactive shell</h4><span class="spacer"></span><span class="muted" id="t-status">connecting…</span><button class="btn small" id="t-reconnect">Reconnect</button></div>
      <div id="terminal-wrap"></div>
    </div>`;
  const run = async () => {
    const cmd = $("#cli").value.trim(); if (!cmd) return;
    const out = $("#cli-out"); out.hidden = false; out.textContent = `$ ${cmd}\n…`;
    try { const r = await satApi("exec", { method: "POST", body: { command: cmd, timeout: 120 } }); out.textContent = `$ ${cmd}\n${r.output}${r.rc ? `\n[exit ${r.rc}]` : ""}`; }
    catch (e) { out.textContent = `$ ${cmd}\nERROR: ${e.message}`; }
  };
  $("#cli-run").onclick = run;
  $("#cli").onkeydown = (e) => { if (e.key === "Enter") run(); };
  $("#t-reconnect").onclick = () => { closeTerminal(); openTerminal(); };
  openTerminal();
}

function openTerminal() {
  const wrap = $("#terminal-wrap"); if (!wrap) return;
  wrap.innerHTML = "";
  if (!window.Terminal) { wrap.innerHTML = '<p class="bad">xterm.js failed to load.</p>'; return; }
  term = new Terminal({ cursorBlink: true, fontFamily: 'ui-monospace, Menlo, Consolas, monospace', fontSize: 13, theme: { background: "#0d1014" }, scrollback: 5000 });
  termFit = new FitAddon.FitAddon(); term.loadAddon(termFit); term.open(wrap); termFit.fit();
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  termSocket = new WebSocket(`${proto}//${location.host}${BASE}api/sat/${state.current}/terminal`);
  termSocket.binaryType = "arraybuffer";
  const send = (o) => termSocket && termSocket.readyState === 1 && termSocket.send(JSON.stringify(o));
  const status = (t) => { const el = $("#t-status"); if (el) el.textContent = t; };
  termSocket.onopen = () => { status("connected"); send({ t: "r", c: term.cols, r: term.rows }); term.focus(); };
  termSocket.onmessage = (ev) => term.write(typeof ev.data === "string" ? ev.data : new Uint8Array(ev.data));
  termSocket.onclose = () => { status("disconnected"); term && term.write("\r\n\x1b[90m[session closed]\x1b[0m\r\n"); };
  term.onData((d) => send({ t: "i", d }));
  term.onResize(({ cols, rows }) => send({ t: "r", c: cols, r: rows }));
  termResize = () => termFit && termFit.fit();
  window.addEventListener("resize", termResize);
}

// ------------------------------------------------------------------ updates
async function tabUpdates(el) {
  let settings;
  try { settings = await satApi("settings"); } catch (e) { el.innerHTML = `<div class="card bad">${esc(e.message)}</div>`; return; }
  const au = settings.auto_update;
  const days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  el.innerHTML = `
    <div class="two">
      <div class="card">
        <h4>Packages</h4>
        <div id="u-status" class="muted">Loading…</div>
        <div class="row" style="margin-top:12px">
          <button class="btn" id="u-check">Check now</button>
          <button class="btn primary" id="u-apply">Install updates</button>
          <button class="btn" id="u-reboot" hidden>Reboot</button>
        </div>
      </div>
      <div class="card">
        <h4>Automatic updates</h4>
        <label class="check"><input type="checkbox" id="s-en" ${au.enabled ? "checked" : ""}> Install updates automatically</label>
        <div class="row" style="margin-bottom:10px"><span>Maintenance window starts at</span><input type="time" id="s-time" value="${esc(au.time)}"></div>
        <div class="row" style="margin-bottom:10px">${days.map((d, i) => `<label class="check" style="margin:0"><input type="checkbox" data-day="${i}" ${au.days.includes(i) ? "checked" : ""}>${d}</label>`).join("")}</div>
        <label class="check"><input type="checkbox" id="s-full" ${au.full_upgrade ? "checked" : ""}> Allow full-upgrade (may add/remove packages)</label>
        <label class="check"><input type="checkbox" id="s-auto" ${au.autoremove ? "checked" : ""}> Remove unused packages afterwards</label>
        <label class="check"><input type="checkbox" id="s-reboot" ${au.auto_reboot ? "checked" : ""}> Reboot automatically when required</label>
        <div class="row"><span class="spacer"></span><button class="btn primary" id="s-save">Save schedule</button></div>
      </div>
    </div>
    <div class="card section"><h4>Packages to upgrade</h4><div id="u-pkgs" class="muted">–</div></div>
    <div class="card section"><h4>Last run output</h4><pre class="out" id="u-log">–</pre></div>`;

  const load = async () => {
    let u; try { u = await satApi("updates"); } catch (e) { $("#u-status").textContent = e.message; return; }
    $("#u-status").innerHTML = `${u.running ? `<span class="warn">Running: ${esc(u.running)}…</span><br>` : ""}
      <b>${u.available ?? "?"}</b> package update(s) available<br>
      Last check: ${fmtAgo(u.last_check)} · Last upgrade: ${fmtAgo(u.last_upgrade)}<br>
      ${u.last_result ? `Result: ${esc(u.last_result)}<br>` : ""}${u.reboot_required ? '<span class="warn">A reboot is required to finish updating.</span>' : ""}`;
    $("#u-reboot").hidden = !u.reboot_required;
    $("#u-check").disabled = $("#u-apply").disabled = !!u.running;
    $("#u-pkgs").innerHTML = u.packages && u.packages.length
      ? `<table><thead><tr><th>Package</th><th>Installed</th><th>New</th></tr></thead><tbody>${u.packages.map((p) => `<tr><td class="mono">${esc(p.name)}</td><td class="mono muted">${esc(p.old)}</td><td class="mono">${esc(p.new)}</td></tr>`).join("")}</tbody></table>`
      : u.available == null ? "Not checked yet." : "Everything is up to date.";
    const log = $("#u-log"); const stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
    log.textContent = (u.log || []).join("\n") || "–"; if (stick) log.scrollTop = log.scrollHeight;
  };
  $("#u-check").onclick = async () => { $("#u-check").disabled = true; toast("Checking…"); try { await satApi("updates/check", { method: "POST" }); } catch (e) { toast(e.message); } load(); };
  $("#u-apply").onclick = async () => { if (!confirm("Install all pending package updates now?")) return; try { await satApi("updates/apply", { method: "POST" }); toast("Upgrade started"); } catch (e) { toast(e.message); } load(); };
  $("#u-reboot").onclick = reboot;
  $("#s-save").onclick = async () => {
    const body = { auto_update: {
      enabled: $("#s-en").checked, time: $("#s-time").value,
      days: [...el.querySelectorAll("[data-day]")].filter((c) => c.checked).map((c) => +c.dataset.day),
      full_upgrade: $("#s-full").checked, autoremove: $("#s-auto").checked, auto_reboot: $("#s-reboot").checked } };
    try { await satApi("settings", { method: "PUT", body }); toast("Schedule saved"); } catch (e) { toast(e.message); }
  };
  await load();
  every(3000, load);
}

// ------------------------------------------------------------------ logs
function tabLogs(el) {
  el.innerHTML = `
    <div class="card">
      <div class="row" style="margin-bottom:10px">
        <select id="l-unit">
          <option value="hasat-agent">HA Satellite agent</option><option value="ser2net">ser2net (radio bridges)</option>
          <option value="">Whole system journal</option><option value="ssh">SSH</option>
        </select>
        <select id="l-lines"><option>100</option><option selected>300</option><option>1000</option></select>
        <button class="btn" id="l-load">Refresh</button>
        <label class="check" style="margin:0"><input type="checkbox" id="l-follow"> Auto-refresh</label>
      </div>
      <pre class="out" id="l-out">Loading…</pre>
    </div>`;
  const load = async () => {
    try { const r = await satApi(`logs?unit=${encodeURIComponent($("#l-unit").value)}&lines=${$("#l-lines").value}`); const o = $("#l-out"); o.textContent = r.output || "(empty)"; o.scrollTop = o.scrollHeight; }
    catch (e) { $("#l-out").textContent = e.message; }
  };
  $("#l-load").onclick = load; $("#l-unit").onchange = load; $("#l-lines").onchange = load;
  every(5000, () => $("#l-follow") && $("#l-follow").checked && load());
  load();
}

// ------------------------------------------------------------------ settings
async function tabSettings(el, s) {
  let settings = null;
  if (s.online) { try { settings = await satApi("settings"); } catch { settings = null; } }
  const c = state.controller;
  el.innerHTML = `
    <div class="two">
      <div class="card">
        <h4>Identity</h4>
        <label class="field"><span>Name (used for Home Assistant entity ids)</span><input type="text" id="e-name" value="${esc(s.name)}"></label>
        <label class="field"><span>Address</span><input type="text" id="e-host" value="${esc(s.host)}"></label>
        <div class="row"><span class="spacer"></span><button class="btn primary" id="e-save">Save</button></div>
      </div>
      <div class="card">
        <h4>Security</h4>
        ${settings ? `
        <label class="check"><input type="checkbox" id="e-fw" ${settings.firewall_enabled ? "checked" : ""}> Firewall: only ${esc((c.trusted_ips || []).join(", ") || "the HA host")} may reach agent and radio ports</label>
        <label class="field"><span>Terminal runs as user</span><input type="text" id="e-user" value="${esc(settings.terminal_user)}"></label>
        <div class="row"><span class="spacer"></span><button class="btn" id="e-sec">Save security settings</button></div>` : '<p class="muted">Available when the satellite is online.</p>'}
        <p class="muted" style="margin-top:10px">Agent API: mutual TLS on port ${esc(s.port)}. CA fingerprint:<br><code>${esc(c.ca_fingerprint)}</code></p>
      </div>
    </div>
    <div class="card section">
      <h4>Maintenance</h4>
      <div class="row">
        <button class="btn" id="e-agent" ${s.online ? "" : "disabled"}>Reinstall agent ${esc(c.agent_version)}</button>
        <button class="btn" id="e-restart-ser" ${s.online ? "" : "disabled"}>Restart ser2net</button>
        <button class="btn" id="e-reboot" ${s.online ? "" : "disabled"}>Reboot</button>
        <button class="btn danger" id="e-off" ${s.online ? "" : "disabled"}>Shut down</button>
        <span class="spacer"></span>
        <button class="btn danger" id="e-remove">Remove satellite…</button>
      </div>
    </div>`;
  $("#e-save").onclick = async () => {
    try { await api(`api/sat/${s.id}`, { method: "PATCH", body: { name: $("#e-name").value, host: $("#e-host").value } }); toast("Saved"); render(); } catch (e) { toast(e.message); }
  };
  if (settings) $("#e-sec").onclick = async () => {
    try { await satApi("settings", { method: "PUT", body: { firewall_enabled: $("#e-fw").checked, terminal_user: $("#e-user").value.trim() } }); toast("Security settings applied"); } catch (e) { toast(e.message); }
  };
  $("#e-agent").onclick = async () => { try { await api(`api/sat/${s.id}/agent-update`, { method: "POST" }); toast("Agent updated, restarting…"); } catch (e) { toast(e.message); } };
  $("#e-restart-ser").onclick = async () => { try { await satApi("services/ser2net/restart", { method: "POST" }); toast("ser2net restarted"); } catch (e) { toast(e.message); } };
  $("#e-reboot").onclick = reboot;
  $("#e-off").onclick = async () => { if (!confirm(`Shut down ${s.name}? You will need physical access to power it back on.`)) return; try { await satApi("system/shutdown", { method: "POST" }); toast("Shutting down…"); } catch (e) { toast(e.message); } };
  $("#e-remove").onclick = () => {
    modalBody.innerHTML = `<h3>Remove ${esc(s.name)}?</h3>
      <p>This removes the satellite and its Home Assistant entities.</p>
      <label class="check"><input type="checkbox" id="rm-un" ${s.online ? "checked" : "disabled"}> Also uninstall the agent from the Pi (restores ser2net config and removes firewall rules)</label>
      <div class="modal-actions"><button type="button" class="btn" id="rm-no">Cancel</button><button type="button" class="btn danger" id="rm-yes">Remove</button></div>`;
    $("#rm-no").onclick = () => modal.close();
    $("#rm-yes").onclick = async () => {
      try { await api(`api/sat/${s.id}?uninstall=${$("#rm-un").checked ? 1 : 0}`, { method: "DELETE" }); modal.close(); toast("Satellite removed"); go(); } catch (e) { toast(e.message); }
    };
    modal.showModal();
  };
}

// ------------------------------------------------------------------ boot
$("#add-btn").onclick = openAdd;
$("#brand").onclick = () => go();
render();
