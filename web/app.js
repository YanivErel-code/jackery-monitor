/* ========================================================================
   Jackery Monitor — UI v4 client.

   Responsibilities:
     • Login flow (POST /api/auth/credentials, GET /api/auth/status).
     • WebSocket /ws for live status + telemetry.
     • Tab switching: Live / Energy / Device.
     • Live chart (battery%, output W, input W).
     • Energy KPIs + Energy history chart (range picker 6h/24h/7d/30d).
     • Device dropdown (multi-device accounts).
     • Output state display (read-only — cloud API does not expose toggles).
   ======================================================================== */

const $ = (id) => document.getElementById(id);

// ---------- state ----------
let lastStatus = null;
let lastDevices = [];
let energyRangeHours = 6;     // current Energy tab range selection
let energyHistoryCache = null; // last fetched series for the energy tab
let activeTab = 'live';

// ---------- helpers ----------
function fmt(n, digits = 0) {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  return Number(n).toFixed(digits);
}
function fmtKwh(wh) {
  if (wh === null || wh === undefined || Number.isNaN(wh)) return '—';
  const k = wh / 1000;
  if (k >= 100) return k.toFixed(0);
  if (k >= 10)  return k.toFixed(1);
  return k.toFixed(2);
}
function show(el, on = true) { if (!el) return; if (on) el.removeAttribute('hidden'); else el.setAttribute('hidden', ''); }

// ============================================================
// LOGIN
// ============================================================
async function checkAuth() {
  try {
    const r = await fetch('/api/auth/status');
    if (!r.ok) return false;
    const j = await r.json();
    if (j.has_credentials === false) {
      showLogin();
      return false;
    }
    hideLogin();
    return true;
  } catch (e) {
    // bridge unreachable / server transient — don't trap the user; let WS retry
    hideLogin();
    return true;
  }
}
function showLogin() { show($('login-overlay'), true); setTimeout(() => $('login-email')?.focus(), 50); }
function hideLogin() { show($('login-overlay'), false); }

$('login-form')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const email = $('login-email').value.trim();
  const password = $('login-password').value;
  const region = $('login-region').value;
  const errEl = $('login-error');
  const btn = $('login-submit');
  errEl.hidden = true;
  btn.disabled = true;
  btn.textContent = 'Verifying…';
  try {
    const r = await fetch('/api/auth/credentials', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password, region }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.ok === false) {
      const msg = j.detail || j.error || ('HTTP ' + r.status);
      errEl.textContent = msg;
      errEl.hidden = false;
      return;
    }
    hideLogin();
  } catch (e) {
    errEl.textContent = String(e);
    errEl.hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Sign in';
  }
});

// ============================================================
// FORGET CREDENTIALS (Device tab > Account)
// ============================================================
$('forget-creds')?.addEventListener('click', async () => {
  const btn = $('forget-creds');
  const msg = $('forget-msg');
  if (!confirm('Wipe stored Jackery credentials? You will need to sign in again to keep monitoring.')) return;
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = 'Signing out…';
  msg.hidden = true;
  try {
    const r = await fetch('/api/auth/forget', { method: 'POST' });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.ok === false) {
      const m = j.detail || j.error || ('HTTP ' + r.status);
      msg.textContent = m + ((m && m.toLowerCase().includes('env')) ? ' — remove JACKERY_EMAIL / JACKERY_PASSWORD from your .env, then redeploy.' : '');
      msg.hidden = false;
      return;
    }
    // Show the login modal right away. Polling will also catch it within 30s.
    showLogin();
  } catch (e) {
    msg.textContent = String(e);
    msg.hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
});

// ============================================================
// TABS
// ============================================================
document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => switchTab(tab.dataset.tab));
});

function switchTab(name) {
  activeTab = name;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('on', t.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach(p => p.toggleAttribute('hidden', p.id !== `tab-${name}`));
  if (name === 'live')   { drawLiveChart(lastStatus); }
  if (name === 'energy') { fetchEnergyHistory(); fetchEnergyAllDevices(); }
}

// ============================================================
// DEVICE PICKER
// ============================================================
$('device-select')?.addEventListener('change', async (e) => {
  const device_id = e.target.value;
  if (!device_id) return;
  try {
    await fetch('/api/select_device', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ device_id }),
    });
  } catch (err) {
    console.warn('select_device failed', err);
  }
});

function renderDevicePicker(devices, selectedId) {
  const wrap = $('device-picker');
  const sel = $('device-select');
  if (!wrap || !sel) return;
  if (!devices || devices.length < 2) { show(wrap, false); return; }
  show(wrap, true);
  // Only rebuild if the option set changed
  const sig = devices.map(d => d.device_id).join('|') + '#' + (selectedId || '');
  if (sel.dataset.sig === sig) return;
  sel.dataset.sig = sig;
  sel.innerHTML = '';
  for (const d of devices) {
    const o = document.createElement('option');
    o.value = d.device_id;
    o.textContent = d.name || d.model_name || d.device_sn || d.device_id;
    if (d.device_id === selectedId) o.selected = true;
    sel.appendChild(o);
  }
}

// ============================================================
// RECONNECT
// ============================================================
$('reconnect')?.addEventListener('click', async () => {
  $('reconnect').disabled = true;
  try { await fetch('/api/reconnect', { method: 'POST' }); }
  finally { setTimeout(() => $('reconnect').disabled = false, 800); }
});

// Output toggles are not supported in cloud-only mode (Jackery cloud API
// is read-only). The .switch tiles are now plain status indicators.

// ============================================================
// WEBSOCKET
// ============================================================
function connectWs() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${location.host}/ws`);
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === 'snapshot' || msg.type === 'telemetry' || msg.type === 'status') {
      applyStatus(msg.data);
    } else if (msg.type === 'alert') {
      const b = $('alert-banner');
      b.textContent = msg.data?.message || 'Alert';
      show(b, true);
      setTimeout(() => show(b, false), 8000);
    }
  };
  ws.onclose = () => setTimeout(connectWs, 2000);
  ws.onerror = () => { try { ws.close(); } catch {} };
}

// ============================================================
// APPLY STATUS
// ============================================================
function applyStatus(s) {
  if (!s) return;
  lastStatus = s;

  // Connection pill
  const pill = $('conn-pill');
  const txt = $('conn-text');
  pill.classList.remove('is-connected', 'is-connecting', 'is-error');
  let label = s.connection_status || 'unknown';
  if (s.connection_status === 'connected') { pill.classList.add('is-connected'); label = 'connected'; }
  else if (['scanning', 'connecting'].includes(s.connection_status)) { pill.classList.add('is-connecting'); }
  else if (s.connection_status === 'error') { pill.classList.add('is-error'); label = s.connection_error || 'error'; }
  txt.textContent = label;

  // Devices dropdown (cloud meta)
  const devices = s.cloud?.devices || [];
  const selectedId = s.cloud?.selected_device_id;
  lastDevices = devices;
  renderDevicePicker(devices, selectedId);

  // Source badges
  const srcLabel = s.source ? s.source.toUpperCase() : '—';
  if ($('source-badge'))   $('source-badge').textContent = srcLabel;
  if ($('source-badge-2')) $('source-badge-2').textContent = srcLabel;

  // Telemetry
  const t = s.telemetry || {};
  if (t.battery_percent != null) {
    $('battery-pct').textContent = fmt(t.battery_percent);
    $('battery-bar-fill').style.width = `${Math.max(0, Math.min(100, t.battery_percent))}%`;
  }
  // Battery time label. The Jackery cloud sends two fields:
  //   time_to_full_h     -> ETA to 100% when the unit is charging
  //   time_remaining_h   -> ETA to empty when the unit is discharging
  // It populates only the relevant one; the other is 0 or stale. We pick the
  // right one based on net power flow. If neither is meaningful and the unit
  // is barely doing anything, show "Idle". As a last resort, synthesise an
  // ETA from SOC and net W.
  const inW   = Number(t.input_power_w  ?? 0);
  const outW  = Number(t.output_power_w ?? 0);
  const netW  = inW - outW;                      // + charging, - discharging
  const soc   = Number(t.battery_percent ?? 0);  // 0..100
  const PACK_KWH = 5.04;                         // Explorer 5000 Plus usable kWh
  const IDLE_W   = 25;                           // below this we call it idle
  const ttFull  = Number(t.time_to_full_h    ?? 0);
  const ttEmpty = Number(t.time_remaining_h  ?? 0);
  let timeLabel;
  if (Math.abs(netW) < IDLE_W) {
    timeLabel = 'Idle';
  } else if (netW > 0) {
    // Charging
    if (ttFull > 0) {
      timeLabel = `${fmt(ttFull, 1)} h to full`;
    } else {
      const wh = ((100 - soc) / 100) * PACK_KWH * 1000;
      const eta = wh / netW;
      timeLabel = eta > 0 && isFinite(eta) ? `${fmt(eta, 1)} h to full` : 'Charging…';
    }
  } else {
    // Discharging
    if (ttEmpty > 0) {
      timeLabel = `${fmt(ttEmpty, 1)} h remaining`;
    } else {
      const wh = (soc / 100) * PACK_KWH * 1000;
      const eta = wh / Math.abs(netW);
      timeLabel = eta > 0 && isFinite(eta) ? `${fmt(eta, 1)} h remaining` : 'Discharging…';
    }
  }
  $('battery-time').textContent = timeLabel;
  if (t.battery_temp_c != null) $('battery-temp').textContent = `${fmt(t.battery_temp_c, 0)} °C`;

  $('output-w').textContent = fmt(t.output_power_w);
  $('input-w').textContent  = fmt(t.input_power_w);
  if (t.ac_output_v != null) $('ac-out-v').textContent  = fmt(t.ac_output_v, 0);
  if (t.ac_output_hz != null)        $('ac-out-hz').textContent = fmt(t.ac_output_hz, 0);

  // Today KPIs from energy aggregator
  if (s.energy?.today) {
    $('today-out-kwh').textContent = fmtKwh(s.energy.today.output_wh);
    $('today-in-kwh').textContent  = fmtKwh(s.energy.today.input_wh);
  }

  // Output state (read-only display from cloud telemetry)
  for (const port of ['ac', 'dc', 'usb', 'car']) {
    const v = t[`${port}_on`];
    const sw = document.querySelector(`.switch[data-port="${port}"]`);
    const lbl = $(`sw-${port}`);
    if (!sw || !lbl) continue;
    if (v === true)       { sw.classList.add('on');  lbl.textContent = 'ON';  }
    else if (v === false) { sw.classList.remove('on'); lbl.textContent = 'OFF'; }
    else                  { sw.classList.remove('on'); lbl.textContent = '—'; }
  }

  // Device tab
  const dev = s.device || {};
  $('dev-name').textContent  = dev.name  || '—';
  $('dev-model').textContent = dev.model_code != null ? `model ${dev.model_code}` : '—';
  $('dev-sn').textContent    = dev.device_sn || '—';
  $('src-cloud').textContent = describeSrc(s.cloud);
  $('dev-updated').textContent = s.last_update_ts ? new Date(s.last_update_ts * 1000).toLocaleString() : '—';
  const upsParts = [t.ups_on && 'UPS', t.super_charge_on && 'Super charge'].filter(Boolean);
  $('dev-ups').textContent = upsParts.length ? upsParts.join(' + ')
    : (t.ups_on === false || t.super_charge_on === false) ? 'Off' : '—';
  $('dev-err').textContent     = t.error_code != null ? String(t.error_code) : '—';

  // Energy KPIs (cards on Energy tab)
  if (s.energy) renderEnergyKpis(s.energy);

  // Live chart
  if (activeTab === 'live') drawLiveChart(s);
}

function describeSrc(meta) {
  if (!meta) return '—';
  const parts = [];
  if (meta.state) parts.push(meta.state);
  if (meta.age_s != null) parts.push(`${meta.age_s}s ago`);
  if (meta.error) parts.push(`(${meta.error})`);
  return parts.join(' · ') || '—';
}

// ============================================================
// ENERGY KPIs
// ============================================================
function renderEnergyKpis(e) {
  const set = (id, wh) => { const el = $(id); if (el) el.textContent = fmtKwh(wh); };
  set('e-today-out', e.today?.output_wh);
  set('e-today-in',  e.today?.input_wh);
  set('e-7d-out',    e.last_7d?.output_wh);
  set('e-7d-in',     e.last_7d?.input_wh);
  set('e-30d-out',   e.last_30d?.output_wh);
  set('e-30d-in',    e.last_30d?.input_wh);
  set('e-life-out',  e.lifetime?.output_wh);
  set('e-life-in',   e.lifetime?.input_wh);
}

// ============================================================
// ENERGY HISTORY CHART + RANGE PICKER
// ============================================================
document.querySelectorAll('.range-btn').forEach((btn) => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.range-btn').forEach(b => b.classList.remove('on'));
    btn.classList.add('on');
    energyRangeHours = parseInt(btn.dataset.hours, 10);
    fetchEnergyHistory();
  });
});

async function fetchEnergyHistory() {
  try {
    const r = await fetch(`/api/energy/history?hours=${energyRangeHours}`);
    if (!r.ok) return;
    const j = await r.json();
    energyHistoryCache = j;
    drawEnergyChart(j);
  } catch (e) { console.warn('energy history fetch failed', e); }
}

async function fetchEnergyAllDevices() {
  try {
    const r = await fetch('/api/energy/devices');
    if (!r.ok) return;
    const j = await r.json();
    const devs = j.devices || [];
    const tbody = $('energy-devices-body');
    if (!tbody) return;
    if (devs.length < 2) { show($('energy-devices'), false); return; }
    show($('energy-devices'), true);
    tbody.innerHTML = '';
    for (const d of devs) {
      const row = document.createElement('tr');
      row.innerHTML = `
        <td>${escapeHtml(d.name || d.device_sn)}</td>
        <td>${fmtKwh(d.today?.output_wh)} / ${fmtKwh(d.today?.input_wh)} kWh</td>
        <td>${fmtKwh(d.last_7d?.output_wh)} / ${fmtKwh(d.last_7d?.input_wh)} kWh</td>
        <td>${fmtKwh(d.last_30d?.output_wh)} / ${fmtKwh(d.last_30d?.input_wh)} kWh</td>
        <td>${fmtKwh(d.lifetime?.output_wh)} / ${fmtKwh(d.lifetime?.input_wh)} kWh</td>
      `;
      tbody.appendChild(row);
    }
  } catch (e) { console.warn('energy devices fetch failed', e); }
}

function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ============================================================
// CHART RENDERERS (canvas, no deps)
// ============================================================
function setCanvasSize(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.max(1, Math.floor(rect.width * dpr));
  canvas.height = Math.max(1, Math.floor(rect.height * dpr));
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w: rect.width, h: rect.height };
}

function drawAxes(ctx, w, h, padL, padR, padT, padB) {
  ctx.strokeStyle = '#232a33';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(padL, padT);
  ctx.lineTo(padL, h - padB);
  ctx.lineTo(w - padR, h - padB);
  ctx.stroke();
}

function drawSeries(ctx, points, color, padL, padR, padT, padB, w, h, minY, maxY) {
  if (!points.length) return;
  const xs = (i) => padL + (i / Math.max(1, points.length - 1)) * (w - padL - padR);
  const ys = (v) => h - padB - ((v - minY) / Math.max(1e-6, maxY - minY)) * (h - padT - padB);
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  ctx.lineJoin = 'round';
  ctx.beginPath();
  let started = false;
  points.forEach((v, i) => {
    if (v == null || Number.isNaN(v)) { started = false; return; }
    const x = xs(i), y = ys(v);
    if (!started) { ctx.moveTo(x, y); started = true; }
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
}

function drawLiveChart(s) {
  const canvas = $('chart-live');
  if (!canvas) return;
  const { ctx, w, h } = setCanvasSize(canvas);
  ctx.clearRect(0, 0, w, h);
  const padL = 36, padR = 16, padT = 14, padB = 22;
  drawAxes(ctx, w, h, padL, padR, padT, padB);

  const hist = (s?.history) || [];
  if (!hist.length) {
    ctx.fillStyle = '#6b7280'; ctx.font = '12px Inter';
    ctx.fillText('Waiting for data…', padL + 8, padT + 16);
    return;
  }
  const bat = hist.map(p => p.battery_percent);
  const out = hist.map(p => p.output_power_w);
  const inp = hist.map(p => p.input_power_w);
  const maxW = Math.max(50, Math.max(...out, ...inp));
  // shared y-axis: scale battery 0..100 to 0..maxW visually for overlay
  const batScaled = bat.map(v => (v == null ? null : (v / 100) * maxW));

  // Grid lines (4 horizontal)
  ctx.strokeStyle = '#1c2128';
  ctx.lineWidth = 1;
  for (let i = 1; i <= 4; i++) {
    const y = padT + ((h - padT - padB) * i) / 5;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
  }

  // Y-axis labels (W)
  ctx.fillStyle = '#6b7280'; ctx.font = '11px Inter';
  ctx.textAlign = 'right';
  for (let i = 0; i <= 4; i++) {
    const v = (maxW * (4 - i)) / 4;
    const y = padT + ((h - padT - padB) * i) / 4;
    ctx.fillText(`${Math.round(v)}`, padL - 6, y + 3);
  }
  ctx.textAlign = 'start';

  drawSeries(ctx, batScaled, '#fbbf24', padL, padR, padT, padB, w, h, 0, maxW);
  drawSeries(ctx, out,       '#4ade80', padL, padR, padT, padB, w, h, 0, maxW);
  drawSeries(ctx, inp,       '#38bdf8', padL, padR, padT, padB, w, h, 0, maxW);
}

function drawEnergyChart(j) {
  const canvas = $('chart-energy');
  if (!canvas) return;
  const { ctx, w, h } = setCanvasSize(canvas);
  ctx.clearRect(0, 0, w, h);
  const padL = 42, padR = 16, padT = 14, padB = 28;
  drawAxes(ctx, w, h, padL, padR, padT, padB);

  const hist = (j?.history) || [];
  if (!hist.length) {
    ctx.fillStyle = '#6b7280'; ctx.font = '12px Inter';
    ctx.fillText('No energy data yet — keep the monitor running.', padL + 8, padT + 16);
    return;
  }

  const out = hist.map(p => p.output_wh || 0);
  const inp = hist.map(p => p.input_wh || 0);
  const bat = hist.map(p => (p.avg_battery_percent ?? p.battery_pct));
  const maxWh = Math.max(1, Math.max(...out, ...inp));

  // Grid + y-axis labels (Wh per bucket)
  ctx.strokeStyle = '#1c2128'; ctx.lineWidth = 1;
  ctx.fillStyle = '#6b7280'; ctx.font = '11px Inter';
  ctx.textAlign = 'right';
  for (let i = 0; i <= 4; i++) {
    const y = padT + ((h - padT - padB) * i) / 4;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
    const v = (maxWh * (4 - i)) / 4;
    ctx.fillText(`${Math.round(v)}`, padL - 6, y + 3);
  }
  ctx.textAlign = 'start';

  // X-axis labels (a few timestamps)
  if (hist.length >= 2) {
    const first = hist[0].ts, last = hist[hist.length - 1].ts;
    const span = last - first;
    const fmtTs = (ts) => {
      const d = new Date(ts * 1000);
      if (span > 24 * 3600) return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
      return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    };
    const ticks = 4;
    ctx.fillStyle = '#6b7280';
    for (let i = 0; i <= ticks; i++) {
      const idx = Math.floor((hist.length - 1) * (i / ticks));
      const x = padL + (i / ticks) * (w - padL - padR);
      ctx.fillText(fmtTs(hist[idx].ts), x - 18, h - 8);
    }
  }

  // Bars: output (green) and input (sky), interleaved
  const n = hist.length;
  const totalW = w - padL - padR;
  const slot = totalW / n;
  const barW = Math.max(1, Math.min(slot * 0.4, 16));
  for (let i = 0; i < n; i++) {
    const xCenter = padL + slot * (i + 0.5);
    const yBase = h - padB;
    const yOut = yBase - (out[i] / maxWh) * (h - padT - padB);
    const yIn  = yBase - (inp[i] / maxWh) * (h - padT - padB);
    ctx.fillStyle = '#4ade80';
    ctx.fillRect(xCenter - barW - 1, yOut, barW, yBase - yOut);
    ctx.fillStyle = '#38bdf8';
    ctx.fillRect(xCenter + 1, yIn, barW, yBase - yIn);
  }

  // Battery % overlay (right axis 0..100)
  if (bat.some(v => v != null)) {
    ctx.strokeStyle = '#fbbf24';
    ctx.lineWidth = 2;
    ctx.beginPath();
    let started = false;
    bat.forEach((v, i) => {
      if (v == null) { started = false; return; }
      const x = padL + slot * (i + 0.5);
      const y = (h - padB) - (v / 100) * (h - padT - padB);
      if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }
}

// Redraw on resize
window.addEventListener('resize', () => {
  if (activeTab === 'live')   drawLiveChart(lastStatus);
  if (activeTab === 'energy' && energyHistoryCache) drawEnergyChart(energyHistoryCache);
});

// ============================================================
// BOOT
// ============================================================
(async function boot() {
  const ok = await checkAuth();
  // Always start the WS — server returns whatever it has, even if cloud is logging in
  connectWs();

  // Pre-load history so the Live chart and Energy tab have data immediately
  // (otherwise the user has to switch tabs once before history populates).
  fetchEnergyHistory();
  fetchEnergyAllDevices();

  // Poll /api/status every 2s as a safety net in case the WebSocket lags
  // or drops a frame. The WS still pushes telemetry as it arrives — this
  // just guarantees the UI is never more than 2 seconds stale.
  setInterval(async () => {
    try {
      const r = await fetch('/api/status', { cache: 'no-store' });
      if (r.ok) applyStatus(await r.json());
    } catch {}
  }, 2000);

  // Refresh energy history at a slower cadence — it's a heavier query and
  // doesn't change every tick. Picks up new samples for the chart.
  setInterval(fetchEnergyHistory, 30000);

  // If auth was missing the user is in the modal — when they sign in successfully
  // the modal hides and the WS snapshot will populate the UI.
  setInterval(checkAuth, 30000);
})();
