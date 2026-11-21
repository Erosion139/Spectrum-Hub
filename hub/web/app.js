'use strict';
// Spectrum Hub web UI. One WebSocket (/ws/ui) brings live events; REST for everything else.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmtBytes = (n) => { if (!n && n !== 0) return '–'; const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0; while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; } return `${n.toFixed(n >= 100 || i === 0 ? 0 : 1)} ${u[i]}`; };
const ago = (t) => { if (!t) return '–'; const s = Math.max(0, Date.now() / 1000 - t); if (s < 60) return `${s | 0}s ago`; if (s < 3600) return `${(s / 60) | 0}m ago`; if (s < 86400) return `${(s / 3600) | 0}h ago`; return new Date(t * 1000).toLocaleDateString(); };
const clock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

async function api(path, opts = {}) {
  const o = { ...opts, headers: { ...(opts.body && !(opts.body instanceof Blob) ? { 'Content-Type': 'application/json' } : {}), ...(opts.headers || {}) } };
  if (o.body && typeof o.body === 'object' && !(o.body instanceof Blob)) o.body = JSON.stringify(o.body);
  const r = await fetch(path, o);
  if (r.status === 401) { location.href = '/login'; throw new Error('Sign in'); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || j.error || `HTTP ${r.status}`);
  return j;
}

function toast(msg) {
  const t = $('#toast'); t.textContent = msg; t.hidden = false;
  clearTimeout(toast.h); toast.h = setTimeout(() => (t.hidden = true), 3500);
}

// ---- state ------------------------------------------------------------------------------
const state = { status: null, requests: new Map(), metrics: [], logPaused: false, turns: new Map() };

// ---- tabs -------------------------------------------------------------------------------
const loaders = {};
$$('#tabs button').forEach((b) => b.addEventListener('click', () => showTab(b.dataset.tab)));
function showTab(name) {
  $$('#tabs button').forEach((b) => b.classList.toggle('on', b.dataset.tab === name));
  $$('.tab').forEach((t) => t.classList.toggle('on', t.id === `tab-${name}`));
  try { localStorage.setItem('tab', name); } catch {}
  loaders[name]?.();
}

// ---- live socket ------------------------------------------------------------------------
function connect() {
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/ui`);
  ws.onopen = () => $('#conn').classList.add('ok');
  ws.onclose = () => { $('#conn').classList.remove('ok'); setTimeout(connect, 2000); };
  ws.onmessage = (m) => handle(JSON.parse(m.data));
}

function handle(ev) {
  switch (ev.kind) {
    case 'hello':
      state.status = ev.status;
      state.requests = new Map(ev.requests.map((r) => [r.id, r]));
      $('#logView').innerHTML = '';
      ev.backlog.forEach(addEvent);
      renderStatus(); renderRequests();
      break;
    case 'metrics': state.metrics.push(ev.metrics); if (state.metrics.length > 180) state.metrics.shift(); if (state.status) state.status.llm = ev.llm; renderMetrics(); renderLlm(); break;
    case 'services': if (state.status) { state.status.services = ev.services; renderStatus(); } break;
    case 'phone': if (state.status) { const { kind, seq, t, ...p } = ev; state.status.phone = p; renderStatus(); } break;
    case 'request': state.requests.set(ev.request.id, ev.request); renderRequests(); break;
    default: addEvent(ev);
  }
}

function addEvent(ev) {
  if (ev.kind === 'log' || ev.kind === 'llm_server') logLine(ev);
  if (ev.kind === 'llm_turn') llmTurn(ev);
}

// ---- status pills -----------------------------------------------------------------------
function renderStatus() {
  const s = state.status; if (!s) return;
  $('#ver').textContent = `v${s.version}`;
  const ph = s.phone, llm = s.llm, svc = s.services;
  const phoneDetail = ph.connected ? `${esc(ph.phones.at(-1).device)} · ${esc(ph.phones.at(-1).remote)}` : ph.last_disconnect ? `Last seen ${ago(ph.last_disconnect)}` : 'Not connected yet';
  const llmCls = { ready: 'ok', loading: 'warn', error: 'bad' }[llm.state] || '';
  const pills = [
    ['Phone', ph.connected ? 'ok' : 'bad', phoneDetail],
    ['Soulseek', svc.soulseek.ok ? 'ok' : svc.soulseek.ok === null ? '' : 'bad', svc.soulseek.detail],
    ['Jellyfin', svc.jellyfin.ok ? 'ok' : svc.jellyfin.ok === null ? '' : 'bad', svc.jellyfin.detail],
    ['LLM', llmCls, llm.state === 'ready' ? esc(llm.model) : llm.error || llm.state],
    ['MCP', s.mcp.active ? 'ok' : '', s.mcp.last_call ? `${s.mcp.calls} calls · last ${ago(s.mcp.last_call)}` : 'No client calls yet'],
  ];
  $('#pills').innerHTML = pills.map(([n, c, d]) => `<div class="pill"><span class="dot ${c}"></span><div><div class="name">${n}</div><div class="detail">${esc(d)}</div></div></div>`).join('');

  const np = ph.connected ? ph.phones.at(-1).now_playing || {} : {};
  $('#phoneCard').innerHTML = ph.connected
    ? `<dt>Device</dt><dd>${esc(ph.phones.at(-1).device)} ${ph.phones.at(-1).app ? `(app ${esc(ph.phones.at(-1).app)})` : ''}</dd>
       <dt>Connected</dt><dd>${ago(ph.phones.at(-1).connected_at)} from ${esc(ph.phones.at(-1).remote)}</dd>
       <dt>Now playing</dt><dd>${np.title ? `${esc(np.title)}${np.artist ? ' · ' + esc(np.artist) : ''} <span class="muted">(${esc(np.state || '')})</span>` : '<span class="muted">Nothing</span>'}</dd>`
    : `<dt>Status</dt><dd>Not connected</dd><dt>Last seen</dt><dd>${ago(ph.last_disconnect)}</dd>`;
  renderLlm();
}

function renderLlm() {
  const l = state.status?.llm; if (!l) return;
  const last = l.last || {};
  $('#llmCard').innerHTML = `
    <dt>State</dt><dd>${esc(l.state)}${l.error ? ` <span class="muted">· ${esc(l.error)}</span>` : ''}</dd>
    <dt>Model</dt><dd>${esc(l.model || '–')}</dd>
    <dt>Generation</dt><dd>${last.gen_tps ? `${last.gen_tps} tok/s` : '–'}${l.avg_gen_tps ? ` <span class="muted">(avg ${l.avg_gen_tps})</span>` : ''}</dd>
    <dt>Prompt</dt><dd>${last.prompt_tps ? `${last.prompt_tps} tok/s · ${last.prompt_tokens} tokens` : '–'}</dd>
    <dt>Last answer</dt><dd>${last.latency_s ? `${last.latency_s} s (first token ${last.first_token_s} s)` : '–'}</dd>
    <dt>Requests</dt><dd>${l.requests}${l.uptime_s ? ` · up ${Math.round(l.uptime_s / 60)} min` : ''}</dd>`;
  const st = $('#llmState'); if (st) st.textContent = `${l.state}${l.model ? ' · ' + l.model : ''}`;
}

// ---- metrics ----------------------------------------------------------------------------
function spark(canvas, values, max, color) {
  const dpr = devicePixelRatio || 1, w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w) return;
  canvas.width = w * dpr; canvas.height = h * dpr;
  const c = canvas.getContext('2d'); c.scale(dpr, dpr);
  if (values.length < 2) return;
  const n = 180, step = w / (n - 1), off = (n - values.length) * step;
  c.beginPath();
  values.forEach((v, i) => { const x = off + i * step, y = h - 2 - (Math.min(v, max) / max) * (h - 6); i ? c.lineTo(x, y) : c.moveTo(x, y); });
  c.strokeStyle = color; c.lineWidth = 1.5; c.stroke();
  c.lineTo(off + (values.length - 1) * step, h); c.lineTo(off, h); c.closePath();
  const g = c.createLinearGradient(0, 0, 0, h); g.addColorStop(0, color + '55'); g.addColorStop(1, color + '00');
  c.fillStyle = g; c.fill();
}

function renderMetrics() {
  const m = state.metrics, s = m.at(-1); if (!s) return;
  const accent = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim() || '#19d3c5';
  const set = (id, v, sub, vals, max) => { const el = $(id); $('.v', el).textContent = v; $('.s', el).textContent = sub; spark($('canvas', el), vals, max, accent); };
  set('#m-cpu', `${s.cpu.toFixed(0)}%`, `${s.cpu_count} threads${s.load != null ? ` · load ${s.load.toFixed(2)}` : ''}`, m.map((x) => x.cpu), 100);
  set('#m-ram', `${((s.ram_used / s.ram_total) * 100).toFixed(0)}%`, `${fmtBytes(s.ram_used)} of ${fmtBytes(s.ram_total)}`, m.map((x) => (x.ram_used / x.ram_total) * 100), 100);
  const g = s.gpus[0];
  if (g) {
    set('#m-gpu', `${g.util}%`, `${g.name}${g.temp_c != null ? ` · ${g.temp_c} °C` : ''}${g.power_w != null ? ` · ${g.power_w.toFixed(0)} W` : ''}`, m.map((x) => x.gpus[0]?.util ?? 0), 100);
    set('#m-vram', `${fmtBytes(g.mem_used)}`, `of ${fmtBytes(g.mem_total)} · ${g.processes} GPU processes`, m.map((x) => x.gpus[0] ? (x.gpus[0].mem_used / x.gpus[0].mem_total) * 100 : 0), 100);
  } else {
    set('#m-gpu', 'n/a', 'No NVIDIA GPU visible to the container', [], 100);
    set('#m-vram', 'n/a', '', [], 100);
  }
}

// ---- requests ---------------------------------------------------------------------------
const STAGES = ['understanding', 'library', 'searching', 'downloading', 'importing', 'indexing', 'sending', 'done'];

function reqHtml(r, full) {
  const c = r.command || {};
  const idx = STAGES.indexOf(r.stage);
  const stages = STAGES.map((s, i) => `<span class="stage ${i < idx || r.status === 'done' ? 'past' : ''} ${s === r.stage && r.status === 'active' ? 'now' : ''}">${s}</span>`).join('');
  const prog = r.stage === 'downloading' && r.status === 'active' ? `<div class="bar"><div style="width:${r.progress || 0}%"></div></div>` : '';
  const actions = full
    ? `<div class="row gap" style="margin-top:8px">${r.status === 'active' || r.status === 'waiting_phone' ? `<button class="small" data-cancel="${r.id}">Cancel</button>` : ''}${r.status === 'failed' || r.status === 'cancelled' ? `<button class="small" data-retry="${r.id}">Retry</button>` : ''}<button class="small danger" data-del="${r.id}">Remove</button></div>`
    : '';
  const details = full
    ? `<details><summary>Details</summary>
        ${c.action ? `<div class="small muted">Command: <code>${esc(JSON.stringify(Object.fromEntries(Object.entries(c).filter(([k, v]) => v && k !== 'extra' && k !== 'raw'))))}</code></div>` : ''}
        <ol class="timeline">${(r.timeline || []).map((t) => `<li>${clock(t.t)} · ${esc(t.message)}</li>`).join('')}</ol>
        ${r.candidates?.length ? `<table class="tbl cands"><tr><th>Score</th><th>File</th><th>User</th><th>Why</th></tr>${r.candidates.map((x) => `<tr><td class="num">${x.score}</td><td>${esc(x.filename.split(/[\\/]/).slice(-2).join('/'))}<br><span class="muted">${x.ext} · ${x.bitrate ? x.bitrate + ' kbps · ' : ''}${fmtBytes(x.size)}</span></td><td>${esc(x.username)}</td><td class="muted">${esc(x.reasons.join(', '))}</td></tr>`).join('')}</table>` : ''}
      </details>`
    : '';
  return `<div class="req"><div class="head"><div class="text">${esc(r.text)}</div><div class="meta"><span class="chip ${r.status}">${r.status.replace('_', ' ')}</span> · ${ago(r.created)} · ${esc(r.source)}</div></div>
    <div class="msg">${esc(r.message)}</div>${full || r.status === 'active' ? `<div class="stages">${stages}</div>` : ''}${prog}${details}${actions}</div>`;
}

function renderRequests() {
  const all = [...state.requests.values()].sort((a, b) => b.created - a.created);
  const active = all.filter((r) => r.status === 'active' || r.status === 'waiting_phone');
  $('#activeReqs').innerHTML = active.length ? active.slice(0, 8).map((r) => reqHtml(r, false)).join('') : (all.length ? all.slice(0, 3).map((r) => reqHtml(r, false)).join('') : '<div class="empty">Nothing yet. Say something to the phone or type a command above.</div>');
  const badge = $('#reqBadge'); badge.hidden = !active.length; badge.textContent = active.length;
  const only = $('#reqActiveOnly').checked;
  const list = only ? active : all;
  // Keep open <details> open across re-renders.
  const open = new Set($$('#reqList details[open]').map((d) => d.closest('.req').dataset.id));
  $('#reqList').innerHTML = list.length ? list.map((r) => reqHtml(r, true).replace('<div class="req">', `<div class="req" data-id="${r.id}">`)).join('') : '<div class="empty">No requests.</div>';
  open.forEach((id) => { const d = $(`#reqList .req[data-id="${id}"] details`); if (d) d.open = true; });
}
$('#reqActiveOnly').addEventListener('change', renderRequests);
document.addEventListener('click', async (e) => {
  const b = e.target.closest('button'); if (!b) return;
  try {
    if (b.dataset.cancel) await api(`/api/requests/${b.dataset.cancel}/cancel`, { method: 'POST' });
    if (b.dataset.retry) await api(`/api/requests/${b.dataset.retry}/retry`, { method: 'POST' });
    if (b.dataset.del) { await api(`/api/requests/${b.dataset.del}`, { method: 'DELETE' }); state.requests.delete(b.dataset.del); renderRequests(); }
    if (b.dataset.pcmd) { const r = await api('/api/phone/command', { method: 'POST', body: { action: b.dataset.pcmd } }); toast(r.ok ? 'Done' : r.message); }
  } catch (err) { toast(err.message); }
});

$('#cmdForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = $('#cmdText').value.trim(); if (!text) return;
  try { await api('/api/requests', { method: 'POST', body: { text } }); $('#cmdText').value = ''; } catch (err) { toast(err.message); }
});

// ---- LLM live output --------------------------------------------------------------------
function llmTurn(ev) {
  let el = state.turns.get(ev.turn);
  const box = $('#llmLive');
  if (!el) {
    el = document.createElement('div'); el.className = 'turn';
    el.innerHTML = `<div class="u"></div><div class="o"></div><div class="st"></div>`;
    box.prepend(el); state.turns.set(ev.turn, el);
    while (box.children.length > 20) { box.lastChild.remove(); }
  }
  if (ev.phase === 'start') $('.u', el).textContent = `${clock(ev.t)}  › ${ev.user}`;
  if (ev.phase === 'token') $('.o', el).textContent += ev.text;
  if (ev.phase === 'end') {
    $('.o', el).textContent = ev.output;
    const s = ev.stats || {};
    $('.st', el).textContent = [s.latency_s && `${s.latency_s} s`, s.gen_tps && `${s.gen_tps} tok/s`, s.prompt_tokens && `${s.prompt_tokens} prompt tokens`].filter(Boolean).join(' · ');
  }
}

// ---- log --------------------------------------------------------------------------------
function logLine(ev) {
  if (state.logPaused) return;
  const view = $('#logView');
  const srv = ev.kind === 'llm_server';
  const cat = srv ? 'llm_server' : ev.category;
  const d = document.createElement('div');
  d.className = `logline ${ev.level || ''} ${srv ? 'srv' : ''}`; d.dataset.cat = cat;
  d.innerHTML = `<span class="t muted">${clock(ev.t)}</span><span class="c">${srv ? 'llama' : esc(cat)}</span><span class="m">${esc(srv ? ev.line : ev.message)}</span>`;
  d.hidden = !logCats().has(cat);
  const stick = view.scrollTop + view.clientHeight >= view.scrollHeight - 30;
  view.append(d);
  while (view.children.length > 2000) view.firstChild.remove();
  if (stick) view.scrollTop = view.scrollHeight;
}
const logCats = () => new Set($$('#logFilters input:checked').map((i) => i.value));
$('#logFilters').addEventListener('change', () => { const c = logCats(); $$('#logView .logline').forEach((l) => (l.hidden = !c.has(l.dataset.cat))); });
$('#logPause').addEventListener('click', (e) => { state.logPaused = !state.logPaused; e.target.textContent = state.logPaused ? 'Resume' : 'Pause'; });

// ---- Soulseek tab -----------------------------------------------------------------------
let slskTimer;
loaders.soulseek = async () => {
  clearTimeout(slskTimer);
  if (!$('#tab-soulseek').classList.contains('on')) return;
  try {
    const d = await api('/api/soulseek');
    const sv = state.status?.services?.soulseek;
    $('#slskState').textContent = d.error ? d.error : sv ? sv.detail : '';
    $('#slskSearches').innerHTML = '<tr><th>Search</th><th>State</th><th class="num">Users</th><th class="num">Files</th><th>When</th></tr>' +
      (d.searches.map((s) => `<tr><td>${esc(s.searchText)}</td><td class="muted">${esc(s.state)}</td><td class="num">${s.responseCount}</td><td class="num">${s.fileCount}</td><td class="muted">${s.startedAt ? new Date(s.startedAt).toLocaleTimeString() : ''}</td></tr>`).join('') || '<tr><td colspan="5" class="empty">No searches yet</td></tr>');
    $('#slskDownloads').innerHTML = '<tr><th>File</th><th>User</th><th>State</th><th class="num">%</th><th class="num">Speed</th></tr>' +
      (d.downloads.map((t) => `<tr><td>${esc(t.filename.split(/[\\/]/).pop())}</td><td>${esc(t.username)}</td><td class="muted">${esc(t.state)}</td><td class="num">${(t.percentComplete || 0).toFixed(0)}</td><td class="num">${t.averageSpeed ? fmtBytes(t.averageSpeed) + '/s' : ''}</td></tr>`).join('') || '<tr><td colspan="5" class="empty">No transfers</td></tr>');
  } catch (err) { $('#slskState').textContent = err.message; }
  slskTimer = setTimeout(loaders.soulseek, 3000);
};

// ---- library tab ------------------------------------------------------------------------
let libData = [];
loaders.library = async () => {
  try { libData = await api('/api/library'); renderLib(); } catch (err) { toast(err.message); }
};
function renderLib() {
  const f = $('#libFilter').value.toLowerCase();
  const rows = libData.filter((s) => !f || `${s.title} ${s.artist} ${s.album}`.toLowerCase().includes(f));
  $('#libInfo').textContent = `${libData.length} songs · ${fmtBytes(libData.reduce((a, s) => a + s.size, 0))}`;
  $('#libTable').innerHTML = '<tr><th>Title</th><th>Artist</th><th>Album</th><th>Format</th><th class="num">Size</th><th>Added</th></tr>' +
    (rows.map((s) => `<tr title="${esc(s.path)}"><td>${esc(s.title)}</td><td>${esc(s.artist)}</td><td>${esc(s.album)}</td><td class="muted">${esc(s.format)}</td><td class="num">${fmtBytes(s.size)}</td><td class="muted">${ago(s.added)}</td></tr>`).join('') || '<tr><td colspan="6" class="empty">Nothing downloaded yet</td></tr>');
}
$('#libFilter').addEventListener('input', renderLib);

// ---- models tab -------------------------------------------------------------------------
let modelTimer;
loaders.models = async () => {
  clearTimeout(modelTimer);
  try {
    const d = await api('/api/models');
    $('#diskFree').textContent = `· ${fmtBytes(d.disk_free)} free`;
    $('#modelTable').innerHTML = '<tr><th>File</th><th class="num">Size</th><th></th></tr>' +
      (d.models.map((m) => `<tr><td>${esc(m.name)}${m.active ? ' <span class="chip done">selected</span>' : ''}</td><td class="num">${fmtBytes(m.size)}</td>
        <td class="num"><button class="small primary" data-use="${esc(m.name)}">${m.active && d.llm.state === 'ready' ? 'Restart' : 'Use'}</button> <button class="small danger" data-delmodel="${esc(m.name)}">Delete</button></td></tr>`).join('') || '<tr><td colspan="3" class="empty">No models yet. Get one from Hugging Face or upload a .gguf file.</td></tr>');
    const f = $('#llmParams');
    if (!f.dataset.loaded) {
      f.llm_gpu_layers.value = d.params.llm_gpu_layers; f.llm_ctx.value = d.params.llm_ctx; f.llm_threads.value = d.params.llm_threads;
      f.llm_extra_args.value = d.params.llm_extra_args; f.llm_autostart.checked = d.params.llm_autostart; f.dataset.loaded = '1';
    }
    renderDownloads(d.downloads);
    if (d.downloads.some((x) => x.state === 'downloading') || d.llm.state === 'loading') modelTimer = setTimeout(loaders.models, 2000);
  } catch (err) { toast(err.message); }
};
function renderDownloads(list) {
  $('#dlProgress').innerHTML = list.map((x) => `<div class="mt small"><b>${esc(x.name)}</b> · ${x.state}${x.error ? ' · ' + esc(x.error) : ''} · ${fmtBytes(x.done)}${x.total ? ' of ' + fmtBytes(x.total) : ''}${x.state === 'downloading' ? ` · ${fmtBytes(x.speed)}/s` : ''}
    <div class="bar"><div style="width:${x.total ? (x.done / x.total) * 100 : 0}%"></div></div></div>`).join('');
}
document.addEventListener('click', async (e) => {
  const b = e.target.closest('button'); if (!b) return;
  try {
    if (b.dataset.use) {
      const f = $('#llmParams');
      await api('/api/llm/start', { method: 'POST', body: { model: b.dataset.use, llm_gpu_layers: +f.llm_gpu_layers.value, llm_ctx: +f.llm_ctx.value, llm_threads: +f.llm_threads.value, llm_extra_args: f.llm_extra_args.value, llm_autostart: f.llm_autostart.checked } });
      toast(`Loading ${b.dataset.use}…`); loaders.models();
    }
    if (b.dataset.delmodel && confirm(`Delete ${b.dataset.delmodel}? The file is removed from the server.`)) { await api(`/api/models/${encodeURIComponent(b.dataset.delmodel)}`, { method: 'DELETE' }); loaders.models(); }
    if (b.dataset.hfget) { await api('/api/models/hf/download', { method: 'POST', body: { repo: $('#hfRepo').value, path: b.dataset.hfget } }); toast('Download started'); loaders.models(); }
  } catch (err) { toast(err.message); }
});
$('#llmStop').addEventListener('click', async () => { try { await api('/api/llm/stop', { method: 'POST' }); loaders.models(); } catch (err) { toast(err.message); } });
$('#hfForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  $('#hfFiles').innerHTML = '<tr><td class="muted">Loading…</td></tr>';
  try {
    const files = await api('/api/models/hf/files', { method: 'POST', body: { repo: $('#hfRepo').value } });
    $('#hfFiles').innerHTML = '<tr><th>File</th><th class="num">Size</th><th></th></tr>' + (files.map((f) => `<tr><td>${esc(f.path)}</td><td class="num">${fmtBytes(f.size)}</td><td class="num"><button class="small" data-hfget="${esc(f.path)}">Download</button></td></tr>`).join('') || '<tr><td colspan="3" class="empty">No .gguf files in that repo</td></tr>');
  } catch (err) { $('#hfFiles').innerHTML = `<tr><td class="muted">${esc(err.message)}</td></tr>`; }
});
$('#upBtn').addEventListener('click', () => {
  const file = $('#upFile').files[0]; if (!file) return toast('Pick a .gguf file first');
  const xhr = new XMLHttpRequest();
  xhr.open('PUT', `/api/models/upload/${encodeURIComponent(file.name)}`);
  xhr.upload.onprogress = (e) => { $('#upBar').style.width = `${(e.loaded / e.total) * 100}%`; $('#upInfo').textContent = `${fmtBytes(e.loaded)} of ${fmtBytes(e.total)}`; };
  xhr.onload = () => { $('#upInfo').textContent = xhr.status < 300 ? 'Uploaded' : `Failed: ${xhr.responseText.slice(0, 120)}`; loaders.models(); };
  xhr.onerror = () => ($('#upInfo').textContent = 'Upload failed');
  xhr.send(file);
});
$('#testForm').addEventListener('submit', async (e) => {
  e.preventDefault(); $('#testOut').textContent = 'Thinking…';
  try { const r = await api('/api/llm/test', { method: 'POST', body: { text: $('#testText').value } }); $('#testOut').textContent = JSON.stringify(r, null, 2); } catch (err) { $('#testOut').textContent = err.message; }
});

// ---- settings tab -----------------------------------------------------------------------
loaders.settings = async () => {
  const s = await api('/api/settings');
  const f = $('#settingsForm');
  for (const [k, v] of Object.entries(s)) {
    const el = f.elements[k]; if (!el) continue;
    if (el.type === 'checkbox') el.checked = !!v;
    else if (el.type === 'password') { el.value = ''; el.placeholder = v.set ? '•••••• (saved; type to replace)' : 'Not set'; }
    else el.value = Array.isArray(v) ? v.join(', ') : v;
  }
  const wsProto = location.protocol === 'https:' ? 'wss' : 'ws';
  $('#wsUrl').textContent = `${wsProto}://${location.host}/ws/phone`;
  $('#mcpUrl').textContent = `${location.origin}/mcp`;
};
$('#settingsForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = e.target, body = {};
  for (const el of f.elements) {
    if (!el.name) continue;
    body[el.name] = el.type === 'checkbox' ? el.checked : el.type === 'number' ? Number(el.value) : el.value;
  }
  try { await api('/api/settings', { method: 'PUT', body }); $('#saveInfo').textContent = 'Saved'; setTimeout(() => ($('#saveInfo').textContent = ''), 2500); loaders.settings(); } catch (err) { toast(err.message); }
});
$('#tokenShow').addEventListener('click', async () => { $('#tokenVal').textContent = (await api('/api/settings/token')).api_token; });
$('#tokenRotate').addEventListener('click', async () => {
  if (!confirm('Make a new token? The phone and MCP clients stop working until you give them the new one.')) return;
  $('#tokenVal').textContent = (await api('/api/settings/token/rotate', { method: 'POST' })).api_token;
});

// ---- start ------------------------------------------------------------------------------
(async () => {
  try { const h = await api('/api/metrics'); state.metrics = h.history; } catch {}
  connect();
  let tab = 'dash'; try { tab = localStorage.getItem('tab') || 'dash'; } catch {}
  showTab($(`#tabs button[data-tab="${tab}"]`) ? tab : 'dash');
  setInterval(() => { renderStatus(); }, 15000);
  addEventListener('resize', renderMetrics);
})();
