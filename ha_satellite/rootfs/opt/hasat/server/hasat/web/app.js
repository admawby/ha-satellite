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
const fmtTemp = (v) => (v == null ? "–" : `${v.toFixed(1)}°C<span class="tf">${(v * 9 / 5 + 32).toFixed(0)}°F</span>`);
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
  const dk = m.docker || {};
  if (dk.available) chips.push(`<span class="chip ${dk.updates_available ? "warn" : ""}">Docker ${dk.running}/${dk.total}${dk.updates_available ? ` · ${dk.updates_available} image update${dk.updates_available > 1 ? "s" : ""}` : ""}</span>`);
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
const TABS = [["overview", "Overview"], ["radios", "USB and Storage"], ["terminal", "Terminal"], ["updates", "Updates"], ["docker", "Docker"], ["logs", "Logs"], ["settings", "Settings"]];

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
  ({ overview: tabOverview, radios: tabRadios, terminal: tabTerminal, updates: tabUpdates, docker: tabDocker, logs: tabLogs, settings: tabSettings })[state.tab](el, s);
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
            <dt>Docker</dt><dd>${(m.docker || {}).available ? `${m.docker.running}/${m.docker.total} containers running · ${m.docker.updates_available} image update(s) · checked ${fmtAgo(m.docker.last_check)}` : "not installed"}</dd>
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
// ------------------------------------------------------------------ USB & storage tab
async function tabRadios(el, s) {
  el.innerHTML = `<div id="sec-radios"></div><div id="sec-storage" style="margin-top:14px"></div><div id="sec-files" style="margin-top:14px"></div>`;
  const files = $("#sec-files");
  const browse = (path) => { renderFiles(files, path); files.scrollIntoView({ behavior: "smooth", block: "start" }); };
  renderRadios($("#sec-radios"), s);
  renderStorage($("#sec-storage"), browse);
  renderFiles(files, "/");
}

async function renderStorage(el, browse) {
  el.innerHTML = `<div class="card muted">Reading drives…</div>`;
  let data;
  try { data = await satApi("storage"); } catch (e) { el.innerHTML = `<div class="card bad">${esc(e.message)}</div>`; return; }
  const devs = data.devices.filter((d) => d.type === "disk" || d.type === "part");
  const row = (d) => {
    const mounted = d.mountpoint ? `<span class="mono">${esc(d.mountpoint)}</span>${d.percent != null ? `<div class="muted" style="font-size:12px">${fmtBytes(d.used)} used · ${fmtBytes(d.free)} free</div>${bar(d.percent, 80, 92)}` : ""}` : '<span class="muted">not mounted</span>';
    const name = d.type === "disk" ? `<b>${esc(d.path)}</b>` : `<span style="padding-left:14px">└ ${esc(d.path)}</span>`;
    const desc = [d.label, d.type === "disk" ? [d.vendor, d.model].filter(Boolean).join(" ") : null].filter(Boolean).join(" · ");
    const actions = [
      d.can_mount ? `<button class="btn small" data-mount="${esc(d.path)}">Mount</button>` : "",
      d.can_unmount ? `<button class="btn small" data-unmount="${esc(d.mountpoint)}">Unmount</button>` : "",
      d.mountpoint ? `<button class="btn small" data-browse="${esc(d.mountpoint)}">Browse</button>` : "",
    ].join(" ");
    return `<tr><td>${name}${desc ? `<div class="muted" style="font-size:12px">${esc(desc)}</div>` : ""}</td>
      <td>${d.usb ? '<span class="chip ok">USB</span> ' : ""}${d.removable && !d.usb ? '<span class="chip">removable</span>' : ""}</td>
      <td class="mono">${esc(d.fstype || "–")}</td><td>${fmtBytes(d.size)}</td><td style="min-width:180px">${mounted}</td>
      <td style="white-space:nowrap;text-align:right">${actions}</td></tr>`;
  };
  el.innerHTML = `
    <div class="card">
      <div class="row" style="margin-bottom:8px"><h4 style="margin:0">Drives &amp; USB storage</h4><span class="spacer"></span><button class="btn small" id="st-refresh">Refresh</button></div>
      ${devs.length ? `<div style="overflow-x:auto"><table><thead><tr><th>Device</th><th></th><th>File system</th><th>Size</th><th>Mounted at</th><th></th></tr></thead>
        <tbody>${devs.map(row).join("")}</tbody></table></div>` : '<p class="muted">No block devices reported (lsblk unavailable).</p>'}
      <p class="muted" style="margin-bottom:0">USB drives are mounted under <code>${esc(data.mount_base)}/&lt;label&gt;</code>. Unmount before unplugging. Mounts made here do not persist across reboots.</p>
    </div>`;
  $("#st-refresh").onclick = () => renderStorage(el, browse);
  el.querySelectorAll("[data-browse]").forEach((b) => (b.onclick = () => browse(b.dataset.browse)));
  el.querySelectorAll("[data-mount]").forEach((b) => (b.onclick = async () => {
    b.disabled = true;
    try { const r = await satApi("storage/mount", { method: "POST", body: { device: b.dataset.mount } }); toast(`Mounted at ${r.mountpoint}`); renderStorage(el, browse); browse(r.mountpoint); }
    catch (e) { toast(e.message, 6000); b.disabled = false; }
  }));
  el.querySelectorAll("[data-unmount]").forEach((b) => (b.onclick = async () => {
    if (!confirm(`Unmount ${b.dataset.unmount}?`)) return;
    b.disabled = true;
    try { await satApi("storage/unmount", { method: "POST", body: { mountpoint: b.dataset.unmount } }); toast("Unmounted — safe to unplug"); renderStorage(el, browse); }
    catch (e) { toast(e.message, 6000); b.disabled = false; }
  }));
}

const joinPath = (dir, name) => (dir === "/" ? "/" + name : `${dir}/${name}`);
const fileUrl = (kind, path, extra = "") => `${BASE}api/sat/${state.current}/files/${kind}?path=${encodeURIComponent(path)}${extra}`;
const fmtDate = (ts) => (ts ? new Date(ts * 1000).toLocaleString([], { year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "–");

function downloadFile(path) {
  const a = document.createElement("a");
  a.href = fileUrl("download", path);
  a.download = path.split("/").pop();
  document.body.appendChild(a); a.click(); a.remove();
}

async function renderFiles(el, path) {
  let data;
  try { data = await satApi(`files/list?path=${encodeURIComponent(path)}`); }
  catch (e) {
    if (path !== "/") { toast(e.message, 5000); return renderFiles(el, "/"); }
    el.innerHTML = `<div class="card bad">${esc(e.message)}</div>`; return;
  }
  const cur = data.path;
  const crumbs = cur.split("/").filter(Boolean);
  const crumbHtml = [`<a href="#" data-go="/">/</a>`].concat(crumbs.map((c, i) => `<a href="#" data-go="${esc("/" + crumbs.slice(0, i + 1).join("/"))}">${esc(c)}</a>`)).join('<span class="muted"> / </span>');
  const icon = (e) => (e.type === "dir" || e.target_is_dir ? "📁" : e.type === "link" ? "🔗" : "📄");
  el.innerHTML = `
    <div class="card">
      <div class="row" style="margin-bottom:8px">
        <h4 style="margin:0">Files</h4>
        <span class="muted">${data.usage ? `${fmtBytes(data.usage.free)} free of ${fmtBytes(data.usage.total)}` : ""}</span>
        <span class="spacer"></span>
        ${data.quick_links.map((p) => `<button class="btn small" data-go="${esc(p)}">${esc(p)}</button>`).join("")}
      </div>
      <div class="row" style="margin-bottom:8px">
        <button class="btn small" id="f-up" ${data.parent == null ? "disabled" : ""}>↑ Up</button>
        <input type="text" id="f-path" class="mono" value="${esc(cur)}" style="flex:1;min-width:200px" spellcheck="false">
        <button class="btn small" id="f-refresh">Refresh</button>
        <button class="btn small" id="f-mkdir">New folder</button>
        <button class="btn small" id="f-new">New file</button>
        <button class="btn small primary" id="f-upload">Upload</button>
        <input type="file" id="f-input" multiple hidden>
      </div>
      <div class="muted" style="margin-bottom:6px">${crumbHtml}</div>
      <div style="overflow-x:auto;max-height:560px;overflow-y:auto"><table>
        <thead><tr><th>Name</th><th style="text-align:right">Size</th><th>Modified</th><th>Permissions</th><th>Owner</th><th></th></tr></thead>
        <tbody>${data.entries.map((e, i) => `<tr>
          <td><a href="#" data-open="${i}">${icon(e)} ${esc(e.name)}</a>${e.type === "link" ? `<span class="muted"> → ${esc(e.target)}</span>` : ""}</td>
          <td style="text-align:right;white-space:nowrap">${e.type === "dir" ? "–" : fmtBytes(e.size)}</td>
          <td style="white-space:nowrap">${fmtDate(e.mtime)}</td>
          <td class="mono">${esc(e.mode)}</td><td class="mono">${esc(e.owner)}</td>
          <td style="white-space:nowrap;text-align:right">
            ${e.type === "file" ? `<button class="btn small" data-dl="${i}">Download</button>` : ""}
            <button class="btn small" data-ren="${i}">Rename</button>
            <button class="btn small danger" data-del="${i}">Delete</button></td></tr>`).join("") || '<tr><td colspan="6" class="muted">Empty folder</td></tr>'}
        </tbody></table></div>
      ${data.truncated ? '<p class="warn">Only the first 5000 entries are shown.</p>' : ""}
    </div>`;

  const go = (p) => renderFiles(el, p);
  el.querySelectorAll("[data-go]").forEach((a) => (a.onclick = (ev) => { ev.preventDefault(); go(a.dataset.go); }));
  $("#f-up").onclick = () => data.parent != null && go(data.parent);
  $("#f-refresh").onclick = () => go(cur);
  $("#f-path").onkeydown = (ev) => { if (ev.key === "Enter") go($("#f-path").value.trim() || "/"); };
  $("#f-mkdir").onclick = async () => {
    const name = prompt("New folder name"); if (!name) return;
    try { await satApi("files/mkdir", { method: "POST", body: { path: joinPath(cur, name) } }); go(cur); } catch (e) { toast(e.message, 5000); }
  };
  $("#f-new").onclick = async () => {
    const name = prompt("New file name"); if (!name) return;
    const p = joinPath(cur, name);
    try { await satApi("files/write", { method: "PUT", body: { path: p, content: "", create: true } }); await go(cur); openEditor(p, () => go(cur)); } catch (e) { toast(e.message, 5000); }
  };
  $("#f-upload").onclick = () => $("#f-input").click();
  $("#f-input").onchange = async () => {
    const list = [...$("#f-input").files];
    for (const [i, f] of list.entries()) {
      toast(`Uploading ${f.name} (${i + 1}/${list.length})…`, 60000);
      let res = await fetch(fileUrl("upload", joinPath(cur, f.name)), { method: "POST", body: f });
      if (!res.ok) {
        const err = (await res.json().catch(() => ({}))).error || res.statusText;
        if (/already exists/.test(err) && confirm(`${f.name} already exists. Overwrite?`)) {
          res = await fetch(fileUrl("upload", joinPath(cur, f.name), "&overwrite=1"), { method: "POST", body: f });
        } else { toast(`${f.name}: ${err}`, 6000); continue; }
      }
      if (!res.ok) toast(`${f.name}: ${(await res.json().catch(() => ({}))).error || res.statusText}`, 6000);
    }
    toast(`Upload finished`);
    go(cur);
  };
  el.querySelectorAll("[data-open]").forEach((a) => (a.onclick = (ev) => {
    ev.preventDefault();
    const e = data.entries[+a.dataset.open];
    const p = joinPath(cur, e.name);
    if (e.type === "dir" || e.target_is_dir) go(p); else openEditor(p, () => go(cur));
  }));
  el.querySelectorAll("[data-dl]").forEach((b) => (b.onclick = () => downloadFile(joinPath(cur, data.entries[+b.dataset.dl].name))));
  el.querySelectorAll("[data-ren]").forEach((b) => (b.onclick = async () => {
    const e = data.entries[+b.dataset.ren];
    const name = prompt(`Rename ${e.name} to`, e.name); if (!name || name === e.name) return;
    try { await satApi("files/rename", { method: "POST", body: { from: joinPath(cur, e.name), to: name.startsWith("/") ? name : joinPath(cur, name) } }); go(cur); }
    catch (err) { toast(err.message, 5000); }
  }));
  el.querySelectorAll("[data-del]").forEach((b) => (b.onclick = async () => {
    const e = data.entries[+b.dataset.del];
    const isDir = e.type === "dir";
    if (!confirm(`Delete ${isDir ? "folder" : "file"} ${joinPath(cur, e.name)}${isDir ? " and everything in it" : ""}? This cannot be undone.`)) return;
    try { await satApi("files/delete", { method: "POST", body: { path: joinPath(cur, e.name), recursive: isDir } }); toast("Deleted"); go(cur); }
    catch (err) { toast(err.message, 5000); }
  }));
}

async function openEditor(path, onClose) {
  let f;
  try { f = await satApi(`files/read?path=${encodeURIComponent(path)}`); } catch (e) { toast(e.message, 5000); return; }
  modal.style.width = "min(1100px, calc(100vw - 32px))";
  modalBody.innerHTML = `
    <h3 class="mono" style="overflow-wrap:anywhere">${esc(path)}</h3>
    <div class="muted" style="margin-bottom:8px">${fmtBytes(f.size) || "0 B"} · ${esc(f.mode)} · modified ${fmtDate(f.mtime)}</div>
    ${f.editable ? `<textarea id="ed-text" class="mono" rows="24" spellcheck="false" style="white-space:pre;tab-size:4"></textarea>
      <div class="muted" style="font-size:12px;margin-top:4px">Ctrl+S to save</div>` : `<p class="warn">${esc(f.reason)}</p>`}
    <div class="modal-actions">
      <button type="button" class="btn" id="ed-dl">Download</button>
      <span class="spacer"></span>
      <button type="button" class="btn" id="ed-close">Close</button>
      ${f.editable ? '<button type="button" class="btn primary" id="ed-save">Save</button>' : ""}
    </div>`;
  let mtime = f.mtime, dirty = false;
  const close = () => {
    if (dirty && !confirm("Discard unsaved changes?")) return;
    modal.oncancel = null; modal.close(); modal.style.width = ""; onClose && onClose();
  };
  $("#ed-close").onclick = close;
  $("#ed-dl").onclick = () => downloadFile(path);
  if (f.editable) {
    const ta = $("#ed-text");
    ta.value = f.content;
    ta.oninput = () => (dirty = true);
    const save = async () => {
      try {
        const r = await satApi("files/write", { method: "PUT", body: { path, content: ta.value, expect_mtime: mtime } });
        mtime = r.mtime; dirty = false; toast(`Saved ${fmtBytes(r.size) || "0 B"}`);
      } catch (e) { toast(e.message, 6000); }
    };
    $("#ed-save").onclick = save;
    ta.onkeydown = (ev) => {
      if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "s") { ev.preventDefault(); save(); }
      if (ev.key === "Tab") { ev.preventDefault(); const s0 = ta.selectionStart; ta.setRangeText("\t", s0, ta.selectionEnd, "end"); dirty = true; }
    };
  }
  modal.oncancel = (ev) => { ev.preventDefault(); close(); };
  modal.showModal();
}

async function renderRadios(el, s) {
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
    $("#r-refresh").onclick = () => renderRadios(el, s);
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

// ------------------------------------------------------------------ docker
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

async function tabDocker(el) {
  let data;
  try { data = await satApi("docker"); } catch (e) { el.innerHTML = `<div class="card bad">${esc(e.message)}</div>`; return; }
  if (!data.available) {
    const job = data.job || {};
    const busy = job.running === "install";
    el.innerHTML = `
      <div class="two">
        <div class="card">
          <h4>Docker</h4>
          ${data.installed
            ? `<p>Docker is installed but its daemon is not running.</p>
               <button class="btn primary" id="dk-start" ${busy ? "disabled" : ""}>Start Docker</button>`
            : `<p class="muted">Docker is not installed on this satellite.</p>
               <label class="field"><span>Install from</span>
                 <select id="dk-method">
                   <option value="official">Docker's official script (get.docker.com): latest Docker CE + compose plugin</option>
                   <option value="debian">Raspberry Pi OS / Debian package (docker.io): older, distro-maintained</option>
                 </select></label>
               <button class="btn primary" id="dk-install" ${busy ? "disabled" : ""}>${busy ? "Installing…" : "Install Docker"}</button>
               <p class="muted" style="margin-bottom:0">Takes a few minutes and needs internet access on the Pi. The Docker service is enabled to start on boot.</p>`}
        </div>
        <div class="card">
          <h4>Install log</h4>
          <div class="muted" id="dk-result">${busy ? '<span class="warn">Running…</span>' : esc(job.last_result || "")}</div>
          <pre class="out" id="dk-log" style="margin-top:8px;max-height:360px">${esc((job.log || []).join("\n") || "–")}</pre>
        </div>
      </div>`;
    const run = async (method) => {
      if (method !== "start" && !confirm("Install Docker on this satellite now?")) return;
      try { await satApi("docker/install", { method: "POST", body: { method } }); toast(method === "start" ? "Starting Docker…" : "Installing Docker…"); }
      catch (e) { toast(e.message, 6000); }
      tabDocker(el);
    };
    if ($("#dk-install")) $("#dk-install").onclick = () => run($("#dk-method").value);
    if ($("#dk-start")) $("#dk-start").onclick = () => run("start");
    every(3000, async () => {
      if (state.tab !== "docker") return;
      try {
        const d = await satApi("docker");
        if (d.available) { clearTimers(); toast("Docker is ready"); return tabDocker(el); }
        const j = d.job || {};
        const log = $("#dk-log"); if (log) { log.textContent = (j.log || []).join("\n") || "–"; log.scrollTop = log.scrollHeight; }
        const res = $("#dk-result"); if (res) res.innerHTML = j.running ? '<span class="warn">Running…</span>' : esc(j.last_result || "");
        const btn = $("#dk-install"); if (btn) { btn.disabled = !!j.running; btn.textContent = j.running ? "Installing…" : "Install Docker"; }
      } catch { /* agent busy or restarting */ }
    });
    return;
  }
  const cfg = data.settings;
  const exclude = new Set(cfg.exclude || []);
  el.innerHTML = `
    <div class="card section">
      <div class="row" style="margin-bottom:10px">
        <h4 style="margin:0">Containers</h4><span class="muted" id="d-summary"></span><span class="spacer"></span>
        <button class="btn" id="d-check">Check for updates</button>
        <button class="btn primary" id="d-update">Update all</button>
      </div>
      <div style="overflow-x:auto"><table id="d-table"></table></div>
      <p class="muted" style="margin-bottom:0">Updating pulls the new image and recreates the container with the same settings (volumes, ports, env, networks, labels, restart policy). If anything fails the original container is put back. Untick “Auto” to keep a container out of scheduled updates.</p>
    </div>
    <div class="two">
      <div class="card">
        <h4>Automatic image updates</h4>
        <label class="check"><input type="checkbox" id="ds-en" ${cfg.enabled ? "checked" : ""}> Run on a schedule</label>
        <label class="field"><span>What to do</span>
          <select id="ds-mode"><option value="update" ${cfg.mode === "update" ? "selected" : ""}>Check and update containers</option>
          <option value="check" ${cfg.mode === "check" ? "selected" : ""}>Only check (report in Home Assistant)</option></select></label>
        <div class="row" style="margin-bottom:10px"><span>Maintenance window starts at</span><input type="time" id="ds-time" value="${esc(cfg.time)}"></div>
        <div class="row" style="margin-bottom:10px">${DAYS.map((d, i) => `<label class="check" style="margin:0"><input type="checkbox" data-dday="${i}" ${cfg.days.includes(i) ? "checked" : ""}>${d}</label>`).join("")}</div>
        <label class="check"><input type="checkbox" id="ds-prune" ${cfg.prune ? "checked" : ""}> Remove the old image after a successful update</label>
        <p class="muted">Containers labelled <code>hasat.update=false</code> (or Watchtower's <code>com.centurylinklabs.watchtower.enable=false</code>) are always skipped.</p>
        <div class="row"><span class="muted" id="ds-excl"></span><span class="spacer"></span><button class="btn primary" id="ds-save">Save schedule</button></div>
      </div>
      <div class="card">
        <h4>Last run</h4>
        <div id="d-job" class="muted"></div>
        <pre class="out" id="d-log" style="margin-top:10px;max-height:300px">–</pre>
      </div>
    </div>`;

  const stateBadge = (c) => {
    const cls = c.state === "running" ? "ok" : c.state === "exited" || c.state === "dead" ? "bad" : "warn";
    return `<span class="chip ${cls}">${esc(c.state)}</span>`;
  };
  const updBadge = (c) => {
    if (c.check_error) return `<span class="chip bad" title="${esc(c.check_error)}">check failed</span>`;
    if (c.update_available) return `<span class="chip warn">update available</span>`;
    if (c.skip_reason && c.skip_reason !== "excluded in settings") return `<span class="chip" title="${esc(c.skip_reason)}">pinned / opted out</span>`;
    return c.checked ? `<span class="chip ok">up to date</span>` : `<span class="muted">not checked</span>`;
  };
  const exclText = () => (exclude.size ? `Excluded: ${[...exclude].join(", ")}` : "");

  const draw = (d) => {
    const job = d.job || {};
    const busy = !!job.running;
    const updates = d.containers.filter((c) => c.update_available).length;
    $("#d-summary").textContent = `${d.containers.filter((c) => c.state === "running").length}/${d.containers.length} running · ${updates} update${updates === 1 ? "" : "s"} · checked ${fmtAgo(job.last_check)}`;
    $("#d-check").disabled = $("#d-update").disabled = busy;
    const eligible = d.containers.filter((c) => c.update_available && c.auto_update && !exclude.has(c.name)).length;
    $("#d-update").textContent = eligible ? `Update all (${eligible})` : "Update all";
    $("#d-table").innerHTML = `<thead><tr><th title="Include in scheduled updates">Auto</th><th>Container</th><th>Image</th><th>State</th><th>Ports</th><th>Image update</th><th></th></tr></thead><tbody>
      ${d.containers.map((c) => {
        const labelOptOut = c.skip_reason && c.skip_reason !== "excluded in settings";
        return `<tr>
        <td><input type="checkbox" data-auto="${esc(c.name)}" ${labelOptOut ? "disabled" : ""} ${!labelOptOut && !exclude.has(c.name) ? "checked" : ""} title="${esc(c.skip_reason || "")}"></td>
        <td><b>${esc(c.name)}</b>${c.project ? `<div class="muted">stack: ${esc(c.project)}</div>` : ""}<div class="muted" style="font-size:12px">${esc(c.status)}</div></td>
        <td class="mono" style="max-width:240px;overflow-wrap:anywhere">${esc(c.image)}<div class="muted">${esc(c.image_id)}</div></td>
        <td>${stateBadge(c)}</td>
        <td class="mono" style="font-size:12px">${c.ports.map(esc).join("<br>") || "–"}</td>
        <td>${updBadge(c)}</td>
        <td style="white-space:nowrap;text-align:right">
          ${c.state === "running" ? `<button class="btn small" data-act="stop" data-n="${esc(c.name)}">Stop</button>` : `<button class="btn small" data-act="start" data-n="${esc(c.name)}">Start</button>`}
          <button class="btn small" data-act="restart" data-n="${esc(c.name)}">Restart</button>
          <button class="btn small" data-act="update" data-n="${esc(c.name)}" ${busy ? "disabled" : ""}>Update</button>
          <button class="btn small" data-logs="${esc(c.name)}">Logs</button>
        </td></tr>`;
      }).join("") || '<tr><td colspan="7" class="muted">No containers.</td></tr>'}</tbody>`;
    $("#d-job").innerHTML = `${busy ? `<span class="warn">Running: ${esc(job.running)}…</span><br>` : ""}
      Last check: ${fmtAgo(job.last_check)} · Last update run: ${fmtAgo(job.last_update)}${job.last_result ? `<br>Result: ${esc(job.last_result)}` : ""}`;
    const log = $("#d-log"); const stick = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
    log.textContent = (job.log || []).join("\n") || "–"; if (stick) log.scrollTop = log.scrollHeight;
    $("#ds-excl").textContent = exclText();

    el.querySelectorAll("[data-auto]").forEach((cb) => (cb.onchange = () => {
      cb.checked ? exclude.delete(cb.dataset.auto) : exclude.add(cb.dataset.auto);
      $("#ds-excl").textContent = `${exclText()} — save schedule to apply`;
    }));
    el.querySelectorAll("[data-act]").forEach((b) => (b.onclick = async () => {
      const { act, n } = b.dataset;
      if (act === "update" && !confirm(`Pull the latest image for ${n} and recreate the container?`)) return;
      if (act === "stop" && !confirm(`Stop ${n}?`)) return;
      b.disabled = true;
      try { await satApi(`docker/containers/${encodeURIComponent(n)}/${act}`, { method: "POST" }); toast(act === "update" ? `Updating ${n}…` : `${n}: ${act} done`); }
      catch (e) { toast(e.message, 5000); }
      load();
    }));
    el.querySelectorAll("[data-logs]").forEach((b) => (b.onclick = () => containerLogs(b.dataset.logs)));
  };

  const load = async () => {
    try { const d = await satApi("docker"); if (d.available && state.tab === "docker") draw(d); } catch (e) { toast(e.message); }
  };
  $("#d-check").onclick = async () => { try { await satApi("docker/check", { method: "POST" }); toast("Checking registries…"); } catch (e) { toast(e.message); } load(); };
  $("#d-update").onclick = async () => {
    if (!confirm("Pull new images and recreate every container that has an update (except excluded ones)?")) return;
    try { await satApi("docker/update", { method: "POST" }); toast("Update started"); } catch (e) { toast(e.message); } load();
  };
  $("#ds-save").onclick = async () => {
    const body = { docker_update: {
      enabled: $("#ds-en").checked, mode: $("#ds-mode").value, time: $("#ds-time").value, prune: $("#ds-prune").checked,
      days: [...el.querySelectorAll("[data-dday]")].filter((c) => c.checked).map((c) => +c.dataset.dday),
      exclude: [...exclude] } };
    try { await satApi("settings", { method: "PUT", body }); toast("Docker schedule saved"); load(); } catch (e) { toast(e.message); }
  };
  draw(data);
  every(4000, load);
}

async function containerLogs(name) {
  modalBody.innerHTML = `<h3>Logs: ${esc(name)}</h3>
    <div class="row" style="margin-bottom:8px"><select id="cl-lines"><option>100</option><option selected>300</option><option>1000</option></select>
    <button type="button" class="btn small" id="cl-refresh">Refresh</button></div>
    <pre class="out" id="cl-out">Loading…</pre>
    <div class="modal-actions"><button type="button" class="btn" id="cl-close">Close</button></div>`;
  modal.style.width = "min(1000px, calc(100vw - 32px))";
  const load = async () => {
    try { const r = await satApi(`docker/containers/${encodeURIComponent(name)}/logs?lines=${$("#cl-lines").value}`); const o = $("#cl-out"); o.textContent = r.output || "(no output)"; o.scrollTop = o.scrollHeight; }
    catch (e) { $("#cl-out").textContent = e.message; }
  };
  $("#cl-refresh").onclick = load; $("#cl-lines").onchange = load;
  $("#cl-close").onclick = () => { modal.close(); modal.style.width = ""; };
  modal.showModal();
  load();
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
