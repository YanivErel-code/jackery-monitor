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
let forecastCache = null;     // last /api/forecast response
let activeTab = 'live';

// ---------- chart palette ----------
// Single source of truth so the chart drawing code, hover tooltips, and
// (future) legend swatches can't drift out of sync.
const SERIES_COLORS = {
  battery: '#fbbf24',  // amber, also matches CSS .lg-bat
  output:  '#4ade80',  // green, also matches CSS .lg-out
  input:   '#38bdf8',  // sky,   also matches CSS .lg-in
};

// ---------- legend / series visibility ----------
const _seriesVisible = {
  live:     { battery: true, output: true, input: true },
  energy:   { battery: true, output: true, input: true },
  forecast: { soc: true, load: true, solar: true },
};

// Adding a new chart? Just register its redraw + cache-getter here.
const _chartRedraw = {
  live:     () => lastStatus && drawLiveChart(lastStatus),
  energy:   () => energyHistoryCache && drawEnergyChart(energyHistoryCache),
  forecast: () => forecastCache && drawForecastChart(forecastCache),
};

document.addEventListener('click', (e) => {
  const item = e.target.closest('.legend-item');
  if (!item) return;
  const chart  = item.closest('.legend')?.dataset.chart;
  const series = item.dataset.series;
  if (!chart || !series || !_seriesVisible[chart]) return;
  e.preventDefault();
  _seriesVisible[chart][series] = !_seriesVisible[chart][series];
  item.classList.toggle('off', !_seriesVisible[chart][series]);
  _chartRedraw[chart]?.();
});

// ---------- service worker ----------
// Register on next idle so it doesn't compete with first-paint. The SW
// caches the static shell so the dashboard loads fast (and works briefly
// offline) once it's installed via "Add to Home Screen".
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js', { scope: '/' })
      .catch((e) => console.warn('SW register failed:', e));
  });
}

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

// Animate a number element from its current value to a new one, with a brief
// amber pulse on the parent .kpi-value when the value actually changes. Used
// for "live" feel on output/input/battery KPIs without per-tick churn.
const _animState = new WeakMap();
function animateNumber(el, target, digits = 0, duration = 600) {
  if (!el || target == null || Number.isNaN(target)) return;
  const numericTarget = Number(target);
  const previous = _animState.get(el)?.value ?? Number(el.textContent.replace(/[^\-\d.]/g,''));
  if (!isFinite(previous) || previous === numericTarget) {
    el.textContent = numericTarget.toFixed(digits);
    _animState.set(el, { value: numericTarget });
    return;
  }
  // Pulse the wrapping .kpi-value (if any) to flash the colour briefly.
  const pulseHost = el.closest('.kpi-value');
  if (pulseHost) {
    pulseHost.classList.remove('changed');
    void pulseHost.offsetWidth;  // restart animation
    pulseHost.classList.add('changed');
  }
  // Cancel any previous tween still running.
  const prev = _animState.get(el);
  if (prev?.raf) cancelAnimationFrame(prev.raf);
  const start = performance.now();
  const from = previous;
  const tick = (now) => {
    const t = Math.min(1, (now - start) / duration);
    // ease-out cubic
    const k = 1 - Math.pow(1 - t, 3);
    const v = from + (numericTarget - from) * k;
    el.textContent = v.toFixed(digits);
    if (t < 1) {
      _animState.set(el, { value: v, raf: requestAnimationFrame(tick) });
    } else {
      _animState.set(el, { value: numericTarget });
    }
  };
  _animState.set(el, { value: from, raf: requestAnimationFrame(tick) });
}

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
// ============================================================
// PAUSE / RESUME polling — hand the cloud session to the phone app
// ============================================================
let _lastCloudMeta = null;        // last s.cloud snapshot for the countdown tick

function fmtSecsMMSS(secs) {
  if (!isFinite(secs) || secs <= 0) return '0:00';
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  return m + ':' + (s < 10 ? '0' + s : s);
}

function renderPausePill() {
  const btn = $('pause-poll');
  const status = $('pause-status');
  const sel = $('pause-duration');
  if (!btn || !status) return;
  const meta = _lastCloudMeta;
  if (!meta) return;
  // Recompute remaining from absolute timestamps so we don't drift if the
  // server's payload is stale by a few seconds.
  const now = Date.now() / 1000;
  const pauseLeft = meta.pause_until ? Math.max(0, meta.pause_until - now) : 0;
  const contestedLeft = meta.contested_until ? Math.max(0, meta.contested_until - now) : 0;

  if (pauseLeft > 0) {
    btn.textContent = 'Resume polling';
    status.hidden = false;
    status.textContent = `paused — ${fmtSecsMMSS(pauseLeft)} left`;
    if (sel) sel.hidden = true;
  } else if (contestedLeft > 0) {
    btn.textContent = 'Pause polling';
    status.hidden = false;
    status.textContent = `session contested — auto-reclaiming in ${fmtSecsMMSS(contestedLeft)}`;
    if (sel) sel.hidden = false;
  } else {
    btn.textContent = 'Pause polling';
    status.hidden = true;
    status.textContent = '';
    if (sel) sel.hidden = false;
  }
}

$('pause-poll')?.addEventListener('click', async () => {
  const btn = $('pause-poll');
  const sel = $('pause-duration');
  const meta = _lastCloudMeta || {};
  const now = Date.now() / 1000;
  const isPaused = meta.pause_until && meta.pause_until > now;
  const seconds = sel ? parseInt(sel.value, 10) || 600 : 600;
  btn.disabled = true;
  try {
    const url = isPaused ? '/api/resume_polling' : '/api/pause_polling';
    const opts = { method: 'POST', headers: { 'content-type': 'application/json' } };
    if (!isPaused) opts.body = JSON.stringify({ seconds });
    await fetch(url, opts);
    // Optimistic update; the WS broadcast / next /api/status poll will reconcile.
    if (isPaused) {
      _lastCloudMeta = { ..._lastCloudMeta, pause_until: null, contested_until: null };
    } else {
      _lastCloudMeta = { ..._lastCloudMeta, pause_until: now + seconds, contested_until: null };
    }
    renderPausePill();
  } catch (e) {
    console.error('pause toggle failed', e);
  } finally {
    btn.disabled = false;
  }
});

// 1-second countdown tick so the "x:yy left" label updates live
setInterval(renderPausePill, 1000);

// ============================================================
// OUTPUT TOGGLES — click an AC/DC/USB/Car card to toggle it
// ============================================================
// After a successful toggle, we don't trust the next ~30s of telemetry to
// reflect the change — the device takes ~5-30s to apply the command and
// push the new state back to the Jackery cloud. During that window we hold
// the optimistic value and tag the button as "pending"; it clears as soon
// as telemetry matches what we asked for, or after the timeout (revert).
const PENDING_TOGGLE_MS = 30000;
const _pendingToggle = {};   // port -> { expected: bool, until: ms }

document.querySelectorAll('.switch').forEach((btn) => {
  btn.addEventListener('click', async () => {
    const port = btn.dataset.port;
    if (!port) return;
    const lbl = $(`sw-${port}`);
    const currentState = (lbl?.textContent || '').trim();
    if (currentState !== 'ON' && currentState !== 'OFF') {
      return;  // unknown state — don't risk a blind toggle
    }
    const turnOn = currentState === 'OFF';
    // AC has highest blast radius (powering whatever's plugged in) — confirm
    // before turning it OFF. Turning ON is fine to do without confirm.
    if (port === 'ac' && !turnOn) {
      if (!confirm('Turn AC output OFF? Anything plugged in will lose power.')) return;
    }
    btn.disabled = true;
    btn.classList.add('pending');
    const original = lbl.textContent;
    lbl.textContent = '…';
    try {
      const r = await fetch('/api/set_output', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ port, on: turnOn }),
      });
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        throw new Error(j.detail || j.error || ('HTTP ' + r.status));
      }
      // Optimistic update + remember the expectation so subsequent telemetry
      // polls can't flip the label back during the device's apply window.
      _pendingToggle[port] = { expected: turnOn, until: Date.now() + PENDING_TOGGLE_MS };
      lbl.textContent = turnOn ? 'ON' : 'OFF';
      btn.classList.toggle('on', turnOn);
    } catch (e) {
      lbl.textContent = original;
      alert(`Failed to toggle ${port.toUpperCase()}: ${e.message || e}`);
    } finally {
      btn.disabled = false;
      // Note: we keep .pending until telemetry confirms the change
    }
  });
});

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
  if (name === 'live')     { drawLiveChart(lastStatus); }
  if (name === 'energy')   { fetchEnergyHistory(); fetchEnergyAllDevices(); }
  if (name === 'forecast') { fetchForecast(); }
  if (name === 'settings') { loadSettings(); loadCostPlan(); initKeepAwakeToggle(); loadAnthropicKeyStatus(); loadAnthropicModelPickers(); }
  if (name === 'logs')     { loadLogs(); }
  if (name === 'automation') {
    loadAutomation(); loadSmartCharge(); loadAlgorithmAdvisor(); resumeAdvisorPollIfRunning();
    // User is now looking — clear the "new insights" dot.
    setAutomationDot(false);
  }
  if (name === 'device')   { loadDeviceCapacity(); loadDeviceParams(); }
}

// Tab-level "new insights pending" badge. Lights up when the daily
// advisor (or a manual run) leaves pending items behind and the user
// isn't currently on the Automation tab.
function setAutomationDot(visible) {
  const dot = $('tab-automation-dot');
  if (!dot) return;
  dot.hidden = !visible;
}

async function refreshAutomationDot() {
  // Skip the network call when we're already on Automation — clicking
  // the tab clears the dot directly. Polling here would be wasted work.
  if (activeTab === 'automation') { setAutomationDot(false); return; }
  try {
    const dev = activeJackeryDevice();
    const params = dev?.device_sn
      ? `?device_sn=${encodeURIComponent(dev.device_sn)}&status=pending`
      : '?status=pending';
    const [insightsR, kasaHealthR] = await Promise.all([
      fetch(`/api/algorithm/suggestions${params}`),
      fetch('/api/kasa/health'),
    ]);
    let insightCount = 0;
    let kasaOffline = 0;
    if (insightsR.ok) {
      const j = await insightsR.json();
      insightCount = (j.suggestions || []).length;
    }
    if (kasaHealthR.ok) {
      const k = await kasaHealthR.json();
      kasaOffline = k.offline_count || 0;
    }
    const dot = $('tab-automation-dot');
    setAutomationDot(insightCount > 0 || kasaOffline > 0);
    if (dot) {
      // Dynamic title so hovering reveals what actually needs attention.
      const reasons = [];
      if (insightCount > 0) reasons.push(`${insightCount} AI insight(s) pending`);
      if (kasaOffline > 0) reasons.push(`${kasaOffline} Kasa device(s) offline`);
      dot.title = reasons.join(' · ') || 'Automation';
    }
  } catch { /* network blip — leave dot unchanged */ }
}

// ============================================================
// DEVICE TAB: capacity override + raw props viewer
// ============================================================
async function loadDeviceCapacity() {
  try {
    const r = await fetch('/api/devices/capacity');
    if (!r.ok) return;
    const j = await r.json();
    const active = activeJackeryDevice();
    const sn = active?.device_sn;
    const dev = (j.devices || []).find(d => d.device_sn === sn) || (j.devices || [])[0];
    if (!dev) return;
    const inp = $('capacity-input');
    const def = $('capacity-default');
    // Prefer the auto-detected value (main + N x pack from the live
    // battery_packs cache) over the bare device default — it's the
    // capacity actually used by the forecaster + smart-charge.
    if (def) {
      if (dev.auto_capacity_wh) {
        def.textContent =
          `(auto-detected: ${dev.auto_capacity_wh} Wh — ${dev.pack_count} packs + main; ` +
          `override only if Jackery's pack capacity differs from spec)`;
      } else {
        def.textContent = `(device default: ${dev.default_capacity_wh} Wh)`;
      }
    }
    if (inp) inp.value = dev.capacity_wh_override ?? '';
    inp.dataset.deviceSn = dev.device_sn;
  } catch (e) {
    console.warn('capacity load failed', e);
  }
  // Refresh the unknown-model banner alongside the capacity widget.
  refreshUnknownModelBanner();
}

// Banner state — sessionStorage so a dismiss survives tab switches but
// a reload still re-flags the unknown model (intentional; we want it
// visible until the user actually fixes it).
const UNKNOWN_MODEL_DISMISS_KEY = 'jackery-umb-dismissed';

// ============================================================
// DEVICE TAB: per-device learned parameters panel
// ============================================================
// Renders the resolution-ladder result for every key in
// energy_db.DEVICE_PARAM_KEYS. Each row shows where the value came
// from (user override / fit / probe / catalog / default / unknown),
// with inline override + reset controls. Same data is exposed to the
// AI advisor so the parameter story is uniform across the app.

async function loadDeviceParams() {
  const list = $('device-params-list');
  if (!list) return;
  const dev = activeJackeryDevice();
  if (!dev?.device_sn) {
    list.innerHTML = '<div class="hint">No active device.</div>';
    return;
  }
  list.innerHTML = '<div class="hint">Loading…</div>';
  try {
    const r = await fetch(`/api/devices/params?device_sn=${encodeURIComponent(dev.device_sn)}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const j = await r.json();
    renderDeviceParams(j.params || [], dev.device_sn);
  } catch (e) {
    list.innerHTML = `<div class="hint">Failed to load: ${e.message || e}</div>`;
  }
}

function renderDeviceParams(rows, deviceSn) {
  const list = $('device-params-list');
  if (!list) return;
  if (!rows.length) {
    list.innerHTML = '<div class="hint">No parameters available yet.</div>';
    return;
  }
  const safe = (s) => String(s == null ? '' : s).replace(/[<>&"]/g, (c) =>
    ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
  const fmtVal = (v, unit) => v == null ? '—' : (
    unit === 'ratio' ? Number(v).toFixed(3) : `${Math.round(Number(v))} ${safe(unit)}`
  );
  const sourceTag = (src) => `<span class="device-param-source device-param-source-${safe(src)}">${safe(src)}</span>`;
  // Currently only max_charge_w has a debug-samples endpoint. As more
  // fits add return_candidates support, list them here.
  const DEBUGGABLE = new Set(['max_charge_w']);
  list.innerHTML = rows.map((p) => `
    <div class="device-param-row" data-key="${safe(p.key)}">
      <div>
        <div class="device-param-label">${safe(p.label || p.key)}</div>
        <div class="device-param-desc">${safe(p.description || '')}${
          p.n_samples ? ` · fit from ${p.n_samples} samples` : ''
        }</div>
      </div>
      <div class="device-param-value">${fmtVal(p.value, p.unit)}</div>
      <div class="device-param-edit" style="display:flex; gap:6px; align-items:center">
        ${sourceTag(p.source || 'unknown')}
        ${DEBUGGABLE.has(p.key)
          ? '<button class="btn btn-ghost btn-small" data-action="debug" type="button">Inspect</button>'
          : ''}
        ${(p.source === 'fit' || p.source === 'probe' || p.source === 'catalog')
          ? '<button class="btn btn-ghost btn-small" data-action="refit" type="button" title="Drop the stored value and re-resolve from the ladder">Refit</button>'
          : ''}
        <button class="btn btn-ghost btn-small" data-action="override" type="button">Override</button>
        ${p.source === 'user' ? '<button class="btn btn-ghost btn-small" data-action="reset" type="button">Reset</button>' : ''}
      </div>
    </div>
    <div class="device-param-debug" data-debug-for="${safe(p.key)}" hidden></div>`).join('');
  list.querySelectorAll('[data-action="override"]').forEach((btn) => {
    btn.addEventListener('click', () => promptOverrideParam(btn, deviceSn, rows));
  });
  list.querySelectorAll('[data-action="reset"]').forEach((btn) => {
    btn.addEventListener('click', () => resetParam(btn, deviceSn));
  });
  list.querySelectorAll('[data-action="debug"]').forEach((btn) => {
    btn.addEventListener('click', () => inspectParam(btn, deviceSn));
  });
  list.querySelectorAll('[data-action="refit"]').forEach((btn) => {
    btn.addEventListener('click', () => refitParam(btn, deviceSn));
  });
}

async function refitParam(btn, deviceSn) {
  const row = btn.closest('.device-param-row');
  const key = row?.dataset.key;
  if (!key) return;
  const status = $('device-params-status');
  if (status) {
    status.hidden = false;
    status.textContent = 'Re-resolving…';
  }
  try {
    const r = await fetch('/api/devices/params/refit', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ device_sn: deviceSn, key }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
    if (status) {
      status.textContent = `Refit: ${j.value} (source=${j.source}${j.n_samples ? `, n=${j.n_samples}` : ''})`;
      setTimeout(() => { status.hidden = true; }, 4000);
    }
    loadDeviceParams();
  } catch (e) {
    if (status) status.textContent = `Refit failed: ${e.message || e}`;
  }
}

async function inspectParam(btn, deviceSn) {
  const row = btn.closest('.device-param-row');
  const key = row?.dataset.key;
  if (!key) return;
  const panel = document.querySelector(`.device-param-debug[data-debug-for="${key}"]`);
  if (!panel) return;
  if (!panel.hidden) { panel.hidden = true; return; }
  panel.hidden = false;
  panel.innerHTML = '<div class="hint" style="padding:8px 12px">Loading samples…</div>';
  try {
    const r = await fetch(
      `/api/devices/params?device_sn=${encodeURIComponent(deviceSn)}&debug_key=${encodeURIComponent(key)}`,
    );
    const j = await r.json();
    const dbg = j.debug || {};
    if (dbg.error) {
      panel.innerHTML = `<div class="hint" style="padding:8px 12px;color:#ef4444">Failed: ${dbg.error}</div>`;
      return;
    }
    const samples = dbg.samples || [];
    if (!samples.length) {
      panel.innerHTML = '<div class="hint" style="padding:8px 12px">No samples returned by the fit.</div>';
      return;
    }
    const safe = (s) => String(s == null ? '' : s).replace(/[<>&"]/g, (c) =>
      ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
    const fmtTime = (ts) => ts ? new Date(ts * 1000).toLocaleString() : '—';
    const rows_html = samples.map(s => `
      <tr>
        <td>${fmtTime(s.ts)}</td>
        <td>${Math.round(s.input_w || 0)}</td>
        <td>${Math.round(s.ac_input_w || 0)}</td>
        <td>${Math.round(s.solar_w || 0)}</td>
        <td>${s.ghi_w_m2 == null ? '—' : Math.round(s.ghi_w_m2)}</td>
        <td>${s.value_used == null ? '—' : Math.round(s.value_used)}</td>
        <td>${safe(s.path)}</td>
      </tr>`).join('');
    panel.innerHTML = `
      <div class="dd-section" style="padding:8px 12px">
        <h4>Samples used by the fit (n=${dbg.n_used_in_fit}, weather_observations=${dbg.weather_observations || 0}, tz_offset=${dbg.tz_offset_seconds}s)</h4>
        <p class="hint" style="margin:4px 0 8px">Each row is one hourly bucket. The 95th percentile of <strong>value_used</strong> gives the fitted result. Path values: <code>ac_input_w</code> = cloud-classified AC, <code>input_w_dark_ghi</code> = no-solar via weather GHI &lt; 50 W/m² (best signal), <code>input_w_night</code> = no-solar via local clock fallback when no weather, <code>skipped_solar_possible</code> = daytime with weather, <code>skipped_daytime_no_ghi</code> = daytime no weather, <code>below_min_input</code> = idle.</p>
        <div style="overflow-x:auto">
          <table class="dd-table">
            <thead><tr>
              <th>hour</th><th>input_w</th><th>ac_input_w</th><th>solar_w</th><th>GHI W/m²</th><th>value_used</th><th>path</th>
            </tr></thead>
            <tbody>${rows_html}</tbody>
          </table>
        </div>
      </div>`;
  } catch (e) {
    panel.innerHTML = `<div class="hint" style="padding:8px 12px;color:#ef4444">Failed: ${e.message || e}</div>`;
  }
}

async function promptOverrideParam(btn, deviceSn, rows) {
  const row = btn.closest('.device-param-row');
  const key = row?.dataset.key;
  if (!key) return;
  const meta = rows.find(p => p.key === key) || {};
  const cur = meta.value != null ? Number(meta.value).toString() : '';
  const v = prompt(
    `Override ${meta.label || key} (in ${meta.unit || 'units'}).\n\n` +
    `Current: ${cur || '(unknown)'}\nLeave blank to cancel.`,
    cur,
  );
  if (v === null || v.trim() === '') return;
  const num = parseFloat(v);
  if (Number.isNaN(num)) { alert('Not a number.'); return; }
  await saveDeviceParam(deviceSn, key, num);
}

async function resetParam(btn, deviceSn) {
  const row = btn.closest('.device-param-row');
  const key = row?.dataset.key;
  if (!key) return;
  if (!confirm('Clear your override and let the dashboard auto-learn this value?')) return;
  await saveDeviceParam(deviceSn, key, null);
}

async function saveDeviceParam(deviceSn, key, value) {
  const status = $('device-params-status');
  if (status) {
    status.hidden = false;
    status.textContent = value == null ? 'Resetting…' : 'Saving…';
  }
  try {
    const r = await fetch('/api/devices/params', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ device_sn: deviceSn, key, value }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
    if (status) {
      status.textContent = 'Saved.';
      setTimeout(() => { status.hidden = true; }, 2000);
    }
    loadDeviceParams();  // re-render so "Reset" button appears/disappears
  } catch (e) {
    if (status) status.textContent = `Failed: ${e.message || e}`;
  }
}


async function refreshUnknownModelBanner() {
  const banner = $('unknown-model-banner');
  if (!banner) return;
  if (sessionStorage.getItem(UNKNOWN_MODEL_DISMISS_KEY) === '1') {
    banner.hidden = true;
    return;
  }
  try {
    const r = await fetch('/api/devices');
    if (!r.ok) { banner.hidden = true; return; }
    const j = await r.json();
    const active = activeJackeryDevice();
    const sn = active?.device_sn;
    const dev = (j.devices || []).find(d => d.device_sn === sn) || (j.devices || [])[0];
    if (!dev || dev.model_recognized !== false) {
      banner.hidden = true;
      return;
    }
    banner.hidden = false;
    $('umb-name').textContent = dev.model_name || dev.name || 'this device';
    $('umb-code').textContent = String(dev.model_code ?? '?');
    $('umb-fallback').textContent = `${dev.inferred_capacity_wh ?? '—'} Wh`;
    banner.dataset.modelCode = String(dev.model_code ?? '');
    banner.dataset.modelName = dev.model_name || '';
    banner.dataset.deviceSn  = dev.device_sn || '';
    refreshProbeCandidates(dev.device_sn);
  } catch (e) {
    console.warn('unknown-model banner refresh failed', e);
    banner.hidden = true;
  }
}

let _probePollTimer = null;
async function refreshProbeCandidates(deviceSn) {
  const status = $('umb-probe-status');
  const list = $('umb-candidates');
  if (!status || !list || !deviceSn) return;
  if (_probePollTimer) { clearTimeout(_probePollTimer); _probePollTimer = null; }
  try {
    const params = `?device_sn=${encodeURIComponent(deviceSn)}`;
    const r = await fetch(`/api/devices/probe_results${params}`);
    if (!r.ok) { status.textContent = 'Probe results unavailable.'; list.innerHTML = ''; return; }
    const j = await r.json();
    const safe = (s) => String(s || '').replace(/[<>&"]/g, (c) =>
      ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
    if (!j.found && j.in_flight) {
      status.textContent = 'Probing cloud for capacity hints… (~10-30s)';
      list.innerHTML = '';
      _probePollTimer = setTimeout(() => refreshProbeCandidates(deviceSn), 4000);
      return;
    }
    if (!j.found) {
      status.textContent = 'No probe yet. Click "Probe again" to try.';
      list.innerHTML = '';
      return;
    }
    if (j.error) {
      status.textContent = `Probe failed: ${j.error}`;
      list.innerHTML = '';
      return;
    }
    const cands = j.candidates || [];
    if (!cands.length) {
      status.textContent = 'Cloud probe completed but no capacity-shaped fields found. Set a manual override below.';
      list.innerHTML = '';
      return;
    }
    status.textContent = `Cloud probe found ${cands.length} capacity-shaped value(s):`;
    list.innerHTML = cands.map((c, i) => `
      <div class="probe-candidate">
        <span class="probe-cap-value">${Math.round(c.capacity_wh)} Wh</span>
        <span class="probe-cap-source hint">${safe(c.endpoint)} → <code>${safe(c.key_path)}</code> (${c.units}: ${safe(c.raw_value)})</span>
        <button class="btn btn-primary btn-small" data-cand-index="${i}" type="button">Use this value</button>
      </div>`).join('');
    list.querySelectorAll('[data-cand-index]').forEach((btn) => {
      btn.addEventListener('click', () => useProbeCandidate(deviceSn, cands[parseInt(btn.dataset.candIndex, 10)]));
    });
  } catch (e) {
    status.textContent = `Probe lookup failed: ${e.message || e}`;
    list.innerHTML = '';
  }
}

async function useProbeCandidate(deviceSn, candidate) {
  if (!candidate) return;
  const wh = Math.round(candidate.capacity_wh);
  if (!confirm(`Set capacity for this device to ${wh} Wh from ${candidate.endpoint}?`)) return;
  try {
    const r = await fetch('/api/devices/capacity', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ device_sn: deviceSn, capacity_wh: wh }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
    const status = $('capacity-status');
    if (status) {
      status.hidden = false;
      status.textContent = `Saved ${wh} Wh from cloud probe.`;
      setTimeout(() => { status.hidden = true; }, 3000);
    }
    loadDeviceCapacity();
  } catch (e) {
    alert(`Failed to apply capacity: ${e.message || e}`);
  }
}

document.getElementById('umb-reprobe')?.addEventListener('click', async () => {
  const banner = $('unknown-model-banner');
  const sn = banner?.dataset?.deviceSn;
  if (!sn) return;
  const status = $('umb-probe-status');
  if (status) status.textContent = 'Re-probing cloud…';
  try {
    await fetch(`/api/devices/probe_now?device_sn=${encodeURIComponent(sn)}`, { method: 'POST' });
    refreshProbeCandidates(sn);
  } catch (e) {
    if (status) status.textContent = `Re-probe failed: ${e.message || e}`;
  }
});

document.getElementById('umb-dismiss')?.addEventListener('click', () => {
  sessionStorage.setItem(UNKNOWN_MODEL_DISMISS_KEY, '1');
  const banner = $('unknown-model-banner');
  if (banner) banner.hidden = true;
});

document.getElementById('umb-copy')?.addEventListener('click', async () => {
  const banner = $('unknown-model-banner');
  if (!banner) return;
  const mc = banner.dataset.modelCode || '?';
  const mn = banner.dataset.modelName || 'Unknown model';
  const inp = $('capacity-input');
  const guessedCap = inp?.value?.trim() || '<TODO: look up your model\'s nominal Wh>';
  const today = new Date().toISOString().slice(0, 10);
  const snippet = [
    `// Add this entry to models.json under "models":`,
    `"${mc}": {`,
    `  "capacity_wh": ${guessedCap},`,
    `  "name": "${mn}",`,
    `  "comment": "Auto-detected on ${today} via Device tab"`,
    `}`,
  ].join('\n');
  const status = $('capacity-status');
  try {
    await navigator.clipboard.writeText(snippet);
    if (status) {
      status.hidden = false;
      status.textContent = 'PR snippet copied — paste into models.json and open a PR.';
      setTimeout(() => { status.hidden = true; }, 5000);
    }
  } catch {
    // Clipboard blocked — drop into a textarea fallback.
    const ta = document.createElement('textarea');
    ta.value = snippet;
    ta.style.cssText = 'position:fixed;top:10vh;left:5vw;width:90vw;height:30vh;z-index:9999;font-family:monospace;font-size:13px';
    document.body.appendChild(ta);
    ta.select();
    setTimeout(() => {
      const dismiss = (ev) => {
        if (ev.target === ta) return;
        ta.remove();
        document.removeEventListener('click', dismiss, true);
      };
      document.addEventListener('click', dismiss, true);
    }, 50);
  }
});

document.getElementById('capacity-form')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const inp = $('capacity-input');
  const status = $('capacity-status');
  const sn = inp.dataset.deviceSn;
  if (!sn) return;
  const raw = inp.value.trim();
  const capacity_wh = raw ? parseInt(raw, 10) : null;
  status.hidden = false;
  status.textContent = 'Saving…';
  try {
    const r = await fetch('/api/devices/capacity', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ device_sn: sn, capacity_wh }),
    });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      throw new Error(j.detail || ('HTTP ' + r.status));
    }
    status.textContent = 'Saved.';
    setTimeout(() => { status.hidden = true; }, 2500);
  } catch (err) {
    status.textContent = `Save failed: ${err.message || err}`;
  }
});

$('capacity-clear')?.addEventListener('click', async () => {
  const inp = $('capacity-input');
  inp.value = '';
  // Trigger the form's submit logic so the override gets cleared on the
  // server side.
  $('capacity-form').dispatchEvent(new Event('submit', { cancelable: true }));
});

$('cloud-probe-btn')?.addEventListener('click', async () => {
  const dump = $('cloud-probe-dump');
  const btn  = $('cloud-probe-btn');
  if (!dump) return;
  dump.hidden = false;
  btn.textContent = 'Probing…';
  btn.disabled = true;
  dump.textContent = 'Sending probe requests to the Jackery cloud (this may take 5-10 seconds)…';
  try {
    const r = await fetch('/api/debug/cloud_probe');
    const j = await r.json();
    btn.textContent = 'Probe again';
    btn.disabled = false;
    if (j.error) {
      dump.textContent = `Error: ${j.error}`;
      return;
    }
    const results = j.results || {};
    const lines = [];
    for (const [path, resp] of Object.entries(results)) {
      lines.push(`=== ${path} ===`);
      lines.push(JSON.stringify(resp, null, 2));
      lines.push('');
    }
    dump.textContent = lines.join('\n');
  } catch (e) {
    btn.textContent = 'Probe again';
    btn.disabled = false;
    dump.textContent = `Failed: ${e.message || e}`;
  }
});

$('raw-props-toggle')?.addEventListener('click', async () => {
  const dump = $('raw-props-dump');
  const btn  = $('raw-props-toggle');
  if (!dump.hidden) {
    dump.hidden = true;
    btn.textContent = 'Show';
    return;
  }
  dump.hidden = false;
  btn.textContent = 'Refresh';
  dump.textContent = 'Loading…';
  try {
    const r = await fetch('/api/debug/raw_props');
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const j = await r.json();
    if (j.error) {
      dump.textContent = `Error: ${j.error}`;
      return;
    }
    const props = j.props || {};
    const keys = Object.keys(props).sort();
    if (!keys.length) {
      dump.textContent = '(no properties yet — wait for the bridge to poll)';
      return;
    }
    dump.textContent = keys.map(k => `${k.padEnd(12)} = ${JSON.stringify(props[k])}`).join('\n');
  } catch (e) {
    dump.textContent = `Failed: ${e.message || e}`;
  }
});

// ============================================================
// AUTOMATION TAB
// ============================================================
let _savedKasaDevices = [];   // last loaded list of saved devices

async function loadAutomation() {
  // Load creds-status, saved devices, and rules in parallel.
  await Promise.all([loadKasaCreds(), loadSavedKasa(), loadRules()]);
}

async function loadKasaCreds() {
  const status = $('kasa-creds-status');
  const emailIn = $('kasa-creds-email');
  if (!status) return;
  try {
    const r = await fetch('/api/kasa/credentials');
    const j = await r.json();
    if (j.has_credentials) {
      status.textContent = `saved · ${j.email || ''}`;
      if (emailIn && !emailIn.value) emailIn.value = j.email || '';
    } else {
      status.textContent = 'not set';
    }
  } catch (e) {
    status.textContent = 'failed to load';
  }
}

document.getElementById('kasa-creds-form')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const email = $('kasa-creds-email').value.trim();
  const password = $('kasa-creds-password').value;
  const msg = $('kasa-creds-msg');
  msg.hidden = false; msg.textContent = 'Saving…';
  try {
    const r = await fetch('/api/kasa/credentials', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ email, password }),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || j.error || ('HTTP ' + r.status));
    msg.textContent = 'Saved.';
    $('kasa-creds-password').value = '';
    setTimeout(() => { msg.hidden = true; }, 2000);
    loadKasaCreds();
  } catch (err) {
    msg.textContent = 'Failed: ' + (err.message || err);
  }
});

$('kasa-creds-clear')?.addEventListener('click', async () => {
  if (!confirm('Forget saved Kasa credentials?')) return;
  await fetch('/api/kasa/credentials', { method: 'DELETE' });
  $('kasa-creds-password').value = '';
  loadKasaCreds();
});

async function loadSavedKasa() {
  const list = $('kasa-list');
  if (!list) return;
  list.innerHTML = '<div class="auto-empty">Loading devices…</div>';
  try {
    const r = await fetch('/api/kasa/saved?refresh=true');
    const j = await r.json();
    _savedKasaDevices = j.devices || [];
    renderSavedKasa(_savedKasaDevices);
  } catch (e) {
    list.innerHTML = `<div class="auto-empty">Failed to load: ${e.message || e}</div>`;
  }
}

function renderSavedKasa(devices) {
  const list = $('kasa-list');
  if (!devices.length) {
    list.innerHTML = '<div class="auto-empty">No Kasa devices yet. Click "+ Add device" to add one.</div>';
    return;
  }
  const safe = (s) => String(s ?? '').replace(/[<>&"]/g, (c) =>
    ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
  list.innerHTML = devices.map((d) => {
    const stateClass = d.online === false ? 'offline' : (d.is_on === true ? 'on' : (d.is_on === false ? 'off' : 'offline'));
    const stateText = d.online === false ? 'OFFLINE' : (d.is_on === true ? 'ON' : (d.is_on === false ? 'OFF' : '—'));
    const meta = [d.model, d.host].filter(Boolean).map((s, i) => i === 0 ? safe(s) : `<span class="host">${safe(s)}</span>`).join(' · ');
    // Resolve the assigned Jackery (if any) to a human label.
    let assignmentLabel = 'unassigned';
    if (d.jackery_device_sn) {
      const j = (lastDevices || []).find(x => x.device_sn === d.jackery_device_sn);
      assignmentLabel = j?.name || `…${d.jackery_device_sn.slice(-6)}`;
    }
    // Last-seen + error: only render when we actually have something to
    // say. A truncated error keeps the row from blowing up vertically;
    // hover the badge for the full text.
    const lastSeen = d.last_seen_ts
      ? new Date(d.last_seen_ts * 1000).toLocaleString()
      : null;
    const errLine = (d.online === false && d.error)
      ? `<div class="kr-error" title="${safe(d.error)}">⚠ ${safe(String(d.error).slice(0, 100))}${String(d.error).length > 100 ? '…' : ''}${lastSeen ? ` · last seen ${safe(lastSeen)}` : ''}</div>`
      : '';
    return `<div class="kasa-row" data-host="${safe(d.host)}">
      <span class="kr-state ${stateClass}">${stateText}</span>
      <div class="kr-name">
        <div class="kr-alias">${safe(d.alias)}</div>
        <div class="kr-meta">${meta} · <span class="kr-assignment">→ ${safe(assignmentLabel)}</span></div>
        ${errLine}
      </div>
      <div class="kasa-row-actions">
        <button class="btn btn-ghost" data-toggle="${safe(d.host)}" data-onstate="${d.is_on ? '1' : '0'}" type="button">${d.is_on ? 'Turn off' : 'Turn on'}</button>
      </div>
      <div class="kasa-row-actions">
        <button class="btn btn-ghost" data-edit-kasa="${safe(d.host)}" type="button">Edit</button>
        <button class="btn btn-ghost" data-del-kasa="${safe(d.host)}" type="button">Delete</button>
      </div>
    </div>`;
  }).join('');

  list.querySelectorAll('[data-toggle]').forEach((b) => {
    b.addEventListener('click', async () => {
      const host = b.dataset.toggle;
      const turnOn = b.dataset.onstate !== '1';
      b.disabled = true; b.textContent = '…';
      try {
        await fetch('/api/kasa/test', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ host, on: turnOn }),
        });
      } catch (e) {
        alert('Toggle failed: ' + (e.message || e));
      } finally {
        b.disabled = false;
        loadSavedKasa();
      }
    });
  });
  list.querySelectorAll('[data-edit-kasa]').forEach((b) => {
    b.addEventListener('click', () => openKasaEditor(devices.find((d) => d.host === b.dataset.editKasa)));
  });
  list.querySelectorAll('[data-del-kasa]').forEach((b) => {
    b.addEventListener('click', async () => {
      const d = devices.find((dd) => dd.host === b.dataset.delKasa);
      if (!d) return;
      if (!confirm(`Remove "${d.alias}" (${d.host})?\nAny rules using it will start failing.`)) return;
      const r = await fetch('/api/kasa/saved/' + encodeURIComponent(d.host), { method: 'DELETE' });
      const j = await r.json();
      if (j.rules_referencing && j.rules_referencing.length) {
        alert(`Removed. Note: ${j.rules_referencing.length} rule(s) still reference this device and will start logging errors.`);
      }
      loadSavedKasa();
    });
  });
}

function openKasaEditor(device) {
  const ed = $('kasa-editor');
  if (!ed) return;
  ed.hidden = false;
  $('kasa-editor-title').textContent = device ? 'Edit Kasa device' : 'Add Kasa device';
  $('kasa-host').value  = device?.host  || '';
  $('kasa-host').disabled = !!device;   // host is the primary key — locked when editing
  $('kasa-alias').value = device?.alias || '';
  // Populate the Jackery-assignment dropdown from the device picker.
  const sel = $('kasa-jackery-sn');
  if (sel) {
    sel.innerHTML = '<option value="">— unassigned (visible to all) —</option>';
    const jackerys = (lastDevices || []);
    for (const d of jackerys) {
      const opt = document.createElement('option');
      opt.value = d.device_sn || '';
      opt.textContent = `${d.name || d.device_sn} (…${(d.device_sn || '').slice(-6)})`;
      sel.appendChild(opt);
    }
    // Default: existing assignment, else the active Jackery on Add.
    if (device) {
      sel.value = device.jackery_device_sn || '';
    } else {
      sel.value = activeJackeryDevice()?.device_sn || '';
    }
  }
  $('kasa-test-result').hidden = true;
  ed.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function closeKasaEditor() {
  const ed = $('kasa-editor');
  if (ed) ed.hidden = true;
  $('kasa-host').disabled = false;
}

$('kasa-add')?.addEventListener('click', () => openKasaEditor(null));
$('kasa-cancel')?.addEventListener('click', closeKasaEditor);
$('kasa-editor-close')?.addEventListener('click', closeKasaEditor);

$('kasa-test-btn')?.addEventListener('click', async () => {
  const host = $('kasa-host').value.trim();
  const result = $('kasa-test-result');
  result.hidden = false; result.textContent = 'Connecting…';
  if (!host) { result.textContent = 'Enter an IP first.'; return; }
  try {
    const r = await fetch('/api/kasa/status?host=' + encodeURIComponent(host));
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || j.error || ('HTTP ' + r.status));
    result.textContent = `OK · ${j.alias} (${j.model || 'unknown'}) currently ${j.is_on ? 'ON' : 'OFF'}`;
    if (j.alias && !$('kasa-alias').value) $('kasa-alias').value = j.alias;
  } catch (e) {
    result.textContent = 'Failed: ' + (e.message || e);
  }
});

document.getElementById('kasa-form')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const body = {
    host:  $('kasa-host').value.trim(),
    alias: $('kasa-alias').value.trim(),
    // Empty string explicitly unassigns the plug; non-empty assigns it
    // to that Jackery's smart-charge / per-device rule pickers.
    jackery_device_sn: $('kasa-jackery-sn')?.value ?? '',
  };
  if (!body.host) return;
  const result = $('kasa-test-result');
  result.hidden = false; result.textContent = 'Saving…';
  try {
    const r = await fetch('/api/kasa/saved', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || j.error || ('HTTP ' + r.status));
    closeKasaEditor();
    loadSavedKasa();
  } catch (err) {
    result.textContent = 'Save failed: ' + (err.message || err);
  }
});

// Filter mode: 'active' = only rules for the currently-selected Jackery
// device (driven by the top-right Device picker). 'all' = show everything.
let _ruleFilterMode = 'active';
let _allRules = [];

async function loadRules() {
  const list = $('auto-rules');
  if (!list) return;
  list.innerHTML = '<div class="auto-empty">Loading rules…</div>';
  try {
    const r = await fetch('/api/automation/rules');
    const j = await r.json();
    _allRules = j.rules || [];
    renderRulesWithFilter();
  } catch (e) {
    list.innerHTML = `<div class="auto-empty">Failed to load: ${e.message || e}</div>`;
  }
}

function activeJackeryDevice() {
  if (!lastStatus || !lastStatus.cloud) return null;
  const sel = lastStatus.cloud.selected_device_id;
  return (lastStatus.cloud.devices || []).find((d) => d.device_id === sel) || null;
}

function renderRulesWithFilter() {
  const filterName = $('auto-rules-filter-name');
  const toggle = $('auto-rules-filter-toggle');
  const active = activeJackeryDevice();
  let displayed = _allRules;

  if (_ruleFilterMode === 'active' && active) {
    if (filterName) filterName.textContent = active.name || active.device_sn;
    if (toggle) toggle.textContent = 'Show all';
    displayed = _allRules.filter((r) =>
      r.jackery_device_sn === active.device_sn || !r.jackery_device_sn);
  } else {
    if (filterName) filterName.textContent = 'All devices';
    if (toggle) toggle.textContent = active ? `Filter to "${active.name || active.device_sn}"` : 'Filter';
  }

  renderAutomationRules(displayed);
}

$('auto-rules-filter-toggle')?.addEventListener('click', () => {
  _ruleFilterMode = (_ruleFilterMode === 'active') ? 'all' : 'active';
  renderRulesWithFilter();
});

function renderAutomationRules(rules) {
  const list = $('auto-rules');
  if (!rules.length) {
    list.innerHTML = '<div class="auto-empty">No rules yet. Click "+ New rule" to add one.</div>';
    return;
  }
  const safe = (s) => String(s).replace(/[<>&"]/g, (c) =>
    ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
  const opLabel = { '<':'&lt;', '<=':'&le;', '=':'=', '>=':'&ge;', '>':'&gt;' };
  list.innerHTML = rules.map((r) => {
    const fired = r.last_fired
      ? 'last fired ' + new Date(r.last_fired * 1000).toLocaleString()
      : 'never fired';
    const errLine = r.last_error
      ? `<div class="ar-meta err">last error: ${safe(r.last_error)}</div>`
      : '';
    // Make it explicit which Jackery device's SOC drives this rule.
    const jackeryName = r.jackery_device_name || r.jackery_device_sn || '(any device)';
    return `<div class="auto-rule ${r.enabled ? '' : 'disabled'}" data-id="${r.id}">
      <div>
        <div class="ar-title">${safe(r.name)}</div>
        <div class="ar-cond">
          when <span class="device">${safe(jackeryName)}</span>
          battery <span class="op">${opLabel[r.operator] || r.operator}</span>
          <span class="val">${r.value}%</span>,
          turn <span class="action ${r.action}">${r.action.toUpperCase()}</span>
          → <span class="device">${safe(r.kasa_alias || r.kasa_host)}</span>
        </div>
        <div class="ar-meta">${fired}</div>
        ${errLine}
      </div>
      <div class="auto-rule-actions">
        <label class="auto-toggle">
          <input type="checkbox" data-toggle="${r.id}" ${r.enabled ? 'checked' : ''} />
          ${r.enabled ? 'enabled' : 'paused'}
        </label>
        <button class="btn btn-ghost" data-edit="${r.id}" type="button">Edit</button>
        <button class="btn btn-ghost" data-del="${r.id}" type="button">Delete</button>
      </div>
    </div>`;
  }).join('');

  // Wire per-row controls
  list.querySelectorAll('[data-edit]').forEach((b) => {
    b.addEventListener('click', () => openAutomationEditor(rules.find((r) => r.id === b.dataset.edit)));
  });
  list.querySelectorAll('[data-del]').forEach((b) => {
    b.addEventListener('click', async () => {
      const r = rules.find((rr) => rr.id === b.dataset.del);
      if (!r) return;
      if (!confirm(`Delete "${r.name}"?`)) return;
      await fetch('/api/automation/rules/' + encodeURIComponent(r.id), { method: 'DELETE' });
      loadAutomation();
    });
  });
  list.querySelectorAll('[data-toggle]').forEach((b) => {
    b.addEventListener('change', async () => {
      const r = rules.find((rr) => rr.id === b.dataset.toggle);
      if (!r) return;
      r.enabled = b.checked;
      await fetch('/api/automation/rules', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(r),
      });
      loadAutomation();
    });
  });
}

function openAutomationEditor(rule) {
  const ed = $('auto-editor');
  if (!ed) return;
  // Repopulate the Kasa device dropdown from the saved-devices list each
  // time so newly added devices appear without a tab switch.
  const pick = $('auto-kasa-pick');
  const hint = $('auto-kasa-pick-hint');
  if (!_savedKasaDevices.length) {
    pick.innerHTML = '<option value="">(no saved devices)</option>';
    pick.disabled = true;
    if (hint) hint.textContent = 'Add a Kasa device above before creating a rule.';
  } else {
    pick.disabled = false;
    pick.innerHTML = '<option value="">— choose a device —</option>' +
      _savedKasaDevices.map((d) =>
        `<option value="${d.host}">${d.alias} (${d.host})</option>`
      ).join('');
    if (hint) hint.textContent = '';
  }
  // Populate the Jackery device picker from lastStatus.cloud.devices —
  // the WS broadcast keeps that current.
  const jpick = $('auto-jackery');
  const jdevs = (lastStatus && lastStatus.cloud && lastStatus.cloud.devices) || [];
  if (!jdevs.length) {
    jpick.innerHTML = '<option value="">(no Jackery devices yet)</option>';
    jpick.disabled = true;
  } else {
    jpick.disabled = false;
    jpick.innerHTML = '<option value="">— choose a device —</option>' +
      jdevs.map((d) =>
        `<option value="${d.device_sn}">${d.name || d.model_name || d.device_sn}</option>`
      ).join('');
  }
  ed.hidden = false;
  $('auto-editor-title').textContent = rule ? 'Edit rule' : 'New rule';
  $('auto-id').value       = rule?.id || '';
  $('auto-name').value     = rule?.name || '';
  $('auto-operator').value = rule?.operator || '<';
  $('auto-value').value    = rule?.value ?? 20;
  $('auto-kasa-pick').value = rule?.kasa_host || '';
  // Pre-fill the Jackery picker: if editing an existing rule keep its sn,
  // otherwise default to whatever device the topbar dropdown is on so
  // creating a rule from "5000 Plus" view targets the 5000 Plus.
  const activeJackery = activeJackeryDevice();
  $('auto-jackery').value = rule?.jackery_device_sn
    || (activeJackery?.device_sn || '');
  $('auto-enabled').checked = rule ? !!rule.enabled : true;
  // Action radios
  const action = rule?.action || 'off';
  document.querySelectorAll('input[name="auto-action"]').forEach((el) => {
    el.checked = (el.value === action);
  });
  ed.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function closeAutomationEditor() {
  const ed = $('auto-editor');
  if (ed) ed.hidden = true;
}

$('auto-add')?.addEventListener('click', () => openAutomationEditor(null));
$('auto-cancel')?.addEventListener('click', closeAutomationEditor);
$('auto-editor-close')?.addEventListener('click', closeAutomationEditor);

document.getElementById('auto-form')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const action = document.querySelector('input[name="auto-action"]:checked')?.value || 'off';
  const host = $('auto-kasa-pick').value;
  const jackerySn = $('auto-jackery').value;
  if (!host) {
    alert('Pick a saved Kasa device first.');
    return;
  }
  if (!jackerySn) {
    alert('Pick which Jackery device this rule should watch.');
    return;
  }
  // Look up the alias from the saved-devices cache so the rule list shows
  // the friendly name even if the device record changes later.
  const dev = _savedKasaDevices.find((d) => d.host === host);
  const jdev = ((lastStatus && lastStatus.cloud && lastStatus.cloud.devices) || [])
    .find((d) => d.device_sn === jackerySn);
  const body = {
    id: $('auto-id').value || undefined,
    name: $('auto-name').value.trim(),
    operator: $('auto-operator').value,
    value: Number($('auto-value').value),
    action,
    kasa_host: host,
    kasa_alias: dev?.alias || host,
    jackery_device_sn:   jackerySn,
    jackery_device_name: jdev?.name || jdev?.model_name || jackerySn,
    enabled: $('auto-enabled').checked,
    trigger: 'battery_percent',
  };
  const status = $('auto-status');
  status.hidden = false; status.textContent = 'Saving…';
  try {
    const r = await fetch('/api/automation/rules', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || j.error || ('HTTP ' + r.status));
    status.textContent = 'Saved.';
    setTimeout(() => { status.hidden = true; }, 2000);
    closeAutomationEditor();
    loadAutomation();
  } catch (err) {
    status.textContent = 'Save failed: ' + (err.message || err);
  }
});

$('kasa-discover')?.addEventListener('click', async () => {
  const wrap = $('auto-discovered');
  const status = $('auto-status');
  status.hidden = false; status.textContent = 'Discovering…';
  wrap.hidden = false; wrap.innerHTML = 'Searching the LAN…';
  try {
    const r = await fetch('/api/kasa/devices');
    const j = await r.json();
    const devs = j.devices || [];
    if (!devs.length) {
      wrap.innerHTML = `<div>No Kasa devices found via discovery. UDP broadcast often doesn't reach Docker bridge networks. Enter the device IP manually below.</div>`;
    } else {
      wrap.innerHTML = devs.map((d) => `
        <div class="auto-disc-row">
          <span class="alias">${d.alias}</span>
          <span class="host">${d.host}</span>
          <span class="hint">${d.model || ''} · ${d.is_on ? 'on' : 'off'}</span>
          <button class="btn btn-ghost" data-add-host="${d.host}" data-add-alias="${d.alias}" type="button">Add</button>
        </div>`).join('');
      // "Add" saves the discovered device into the registry so it appears
      // in the rule editor's dropdown.
      wrap.querySelectorAll('[data-add-host]').forEach((b) => {
        b.addEventListener('click', async () => {
          b.disabled = true; b.textContent = 'Adding…';
          try {
            await fetch('/api/kasa/saved', {
              method: 'POST',
              headers: { 'content-type': 'application/json' },
              body: JSON.stringify({ host: b.dataset.addHost, alias: b.dataset.addAlias }),
            });
            loadSavedKasa();
          } catch (e) {
            alert('Failed: ' + (e.message || e));
          } finally {
            b.disabled = false; b.textContent = 'Add';
          }
        });
      });
    }
    status.hidden = true;
  } catch (e) {
    wrap.innerHTML = `<div>Discovery failed: ${e.message || e}</div>`;
    status.hidden = true;
  }
});

// ============================================================
// LOGS TAB
// ============================================================
let _logsTimer = null;

async function loadLogs() {
  const list = $('logs-list');
  if (!list) return;
  try {
    const r = await fetch('/api/events?limit=200');
    const j = await r.json();
    renderLogs(j.events || []);
  } catch (e) {
    list.innerHTML = `<div class="logs-empty">Failed to load: ${e.message || e}</div>`;
  }
  // Restart auto-refresh timer based on the checkbox state
  if (_logsTimer) { clearInterval(_logsTimer); _logsTimer = null; }
  if ($('logs-auto')?.checked && activeTab === 'logs') {
    _logsTimer = setInterval(() => {
      if (activeTab !== 'logs') { clearInterval(_logsTimer); _logsTimer = null; return; }
      loadLogs();
    }, 5000);
  }
}

function renderLogs(events) {
  const list = $('logs-list');
  const filterValue = $('logs-filter')?.value || 'all';
  // Filter: 'all', a level (error/warn/info), or a category (auth/poll/mqtt/session)
  const levelOrder = { error: 0, warn: 1, info: 2 };
  const filtered = events.filter((e) => {
    if (filterValue === 'all') return true;
    if (filterValue in levelOrder) {
      // "warn" means warn+error, "info" means everything
      return levelOrder[e.level] <= levelOrder[filterValue];
    }
    return e.category === filterValue;
  });
  if (!filtered.length) {
    list.innerHTML = '<div class="logs-empty">No events yet.</div>';
    return;
  }
  // Newest first.
  filtered.sort((a, b) => b.ts - a.ts);
  list.innerHTML = filtered.map((e) => {
    const ts = new Date(e.ts * 1000).toLocaleTimeString('en-GB', { hour12: false });
    const extras = e.extra ? ' · ' + Object.entries(e.extra).map(([k, v]) =>
      `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`).join(' ') : '';
    const safe = (s) => String(s).replace(/[<>&]/g, (c) => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));
    return `<div class="logs-row lvl-${e.level}">
      <span class="lr-ts">${ts}</span>
      <span class="lr-level">${e.level}</span>
      <span class="lr-cat">${safe(e.category)}</span>
      <span class="lr-msg">${safe(e.message)}${safe(extras)}</span>
    </div>`;
  }).join('');
}

$('logs-refresh')?.addEventListener('click', loadLogs);
$('logs-filter')?.addEventListener('change', () => {
  // Re-render with the same data — we kept the last result on the DOM but
  // it's cheaper to just re-fetch since the buffer is small.
  loadLogs();
});
$('logs-auto')?.addEventListener('change', loadLogs);

// ============================================================
// LOGS → DEBUG PANEL
// ============================================================
// Each button runs an ad-hoc query against the local data store,
// formats the result as a table, and writes it to #debug-output.
// Replaces the console.log / console.table workflow.

function _dbgRender(title, rows, columns) {
  const out = $('debug-output');
  if (!out) return;
  out.hidden = false;
  if (!rows || !rows.length) {
    out.innerHTML = `<div class="dbg-block"><h3>${title}</h3><p class="hint">No data.</p></div>` + out.innerHTML;
    return;
  }
  const cols = columns || Object.keys(rows[0]);
  const head = cols.map(c => `<th>${c}</th>`).join('');
  const body = rows.map(r => `<tr>${cols.map(c => `<td>${_dbgFmt(r[c])}</td>`).join('')}</tr>`).join('');
  const ts = new Date().toLocaleTimeString();
  out.innerHTML = `
    <div class="dbg-block">
      <h3>${title} <small class="hint">(${ts})</small></h3>
      <div class="dbg-table-wrap">
        <table class="dbg-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>
      </div>
    </div>` + out.innerHTML;
}

function _dbgFmt(v) {
  if (v == null) return '—';
  if (typeof v === 'number' && Number.isFinite(v)) {
    return Math.abs(v) < 1 && v !== 0 ? v.toFixed(3) : v.toString();
  }
  if (typeof v === 'object') return `<code>${JSON.stringify(v).slice(0,80)}</code>`;
  return String(v);
}

function _dbgSummary(title, lines) {
  const out = $('debug-output');
  if (!out) return;
  out.hidden = false;
  const ts = new Date().toLocaleTimeString();
  const items = lines.map(l => `<div>${l}</div>`).join('');
  out.innerHTML = `
    <div class="dbg-block">
      <h3>${title} <small class="hint">(${ts})</small></h3>
      <div class="dbg-summary">${items}</div>
    </div>` + out.innerHTML;
}

async function _dbgFetch(url) {
  const status = $('debug-status');
  status.hidden = false;
  status.textContent = `Fetching ${url}…`;
  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    status.hidden = true;
    return await r.json();
  } catch (e) {
    status.textContent = `Failed: ${e.message || e}`;
    return null;
  }
}

$('dbg-forecast-24h')?.addEventListener('click', async () => {
  const j = await _dbgFetch('/api/forecast/accuracy');
  if (!j) return;
  const cutoff = Date.now()/1000 - 24*3600;
  const recent = (j.samples || []).filter(s => s.target >= cutoff && s.actual_soc != null);
  if (!recent.length) {
    _dbgSummary('Forecast vs actual (last 24h)', ['No predictions with actuals in the last 24h.']);
    return;
  }
  const errs = recent.map(s => Math.abs(s.predicted_soc - s.actual_soc));
  const mae = errs.reduce((a,b)=>a+b,0) / errs.length;
  const max = Math.max(...errs);
  const overunder = recent.map(s => s.predicted_soc - s.actual_soc);
  const meanBias = overunder.reduce((a,b)=>a+b,0) / overunder.length;
  _dbgSummary(`Forecast vs actual (last 24h, ${recent.length} samples)`, [
    `<strong>MAE:</strong> ${mae.toFixed(1)}pp`,
    `<strong>Max error:</strong> ${max.toFixed(1)}pp`,
    `<strong>Bias:</strong> ${meanBias > 0 ? '+' : ''}${meanBias.toFixed(1)}pp ${meanBias > 0 ? '(over-predicting)' : '(under-predicting)'}`,
  ]);
  const rows = recent.map(s => ({
    target: new Date(s.target*1000).toLocaleString(),
    lead_h: s.lead_time_h,
    pred: Math.round(s.predicted_soc),
    actual: Math.round(s.actual_soc),
    err_pp: (s.predicted_soc - s.actual_soc).toFixed(1),
  })).sort((a,b) => a.target.localeCompare(b.target));
  _dbgRender('Predictions, sorted by target time', rows);
});

$('dbg-forecast-buckets')?.addEventListener('click', async () => {
  const j = await _dbgFetch('/api/forecast/accuracy');
  if (!j) return;
  const all = j.summary || {};
  const postFix = j.summary_post_fix || {};
  // Render both views side-by-side so it's obvious how much the
  // pre-fix stale rows are dragging the headline numbers down.
  // `summary_post_fix` only counts predictions made AT OR AFTER
  // the most recent forecaster breaking-change deploy.
  const cutoff = j.cutoff_ts
    ? new Date(j.cutoff_ts * 1000).toLocaleString()
    : '—';
  const buckets = ['≤6h', '≤24h', '≤72h', '>72h'];
  const rows = buckets.map(bucket => {
    const a = all[bucket] || { n: 0, mae: null };
    const p = postFix[bucket] || { n: 0, mae: null };
    return {
      lead_time: bucket,
      all_samples: a.n,
      all_mae_pp: a.mae,
      post_fix_samples: p.n,
      post_fix_mae_pp: p.mae,
    };
  });
  _dbgSummary(
    `Forecast accuracy by lead time`,
    [
      `<strong>All-time samples:</strong> ${(j.samples||[]).length}`,
      `<strong>Post-fix cutoff:</strong> ${cutoff} (predictions made before this excluded from post-fix bucket)`,
    ],
  );
  _dbgRender('All vs post-fix', rows);
});

$('dbg-smart-charge')?.addEventListener('click', async () => {
  const j = await _dbgFetch('/api/smart_charge/analytics?days=14');
  if (!j) return;
  const s = j.summary || {};
  const lines = [
    `<strong>Window:</strong> ${j.days || 14} days`,
    `<strong>Decisions:</strong> ${s.n || 0}`,
    `<strong>Target hit rate:</strong> ${s.target_hit_rate != null ? Math.round(s.target_hit_rate * 100) + '%' : '—'}`,
    `<strong>Mean abs error:</strong> ${s.mae_pp != null ? s.mae_pp.toFixed(1) + 'pp' : '—'}`,
  ];
  _dbgSummary('Smart-charge analytics', lines);
  if (j.samples?.length) {
    _dbgRender('Recent decisions',
      j.samples.slice(0, 20).map(d => ({
        when: new Date((d.decided_at || 0) * 1000).toLocaleString(),
        action: d.action,
        mode: d.mode,
        pred_sunrise: d.predicted_sunrise_soc_pct != null ? Math.round(d.predicted_sunrise_soc_pct) : null,
        actual_sunrise: d.actual_sunrise_soc_pct != null ? Math.round(d.actual_sunrise_soc_pct) : null,
        target: d.target_sunrise_soc_pct,
        err_pp: d.prediction_error_pp != null ? d.prediction_error_pp.toFixed(1) : null,
      })));
  }
});

$('dbg-daily-summary')?.addEventListener('click', async () => {
  const j = await _dbgFetch('/api/daily_summary?days=7');
  if (!j || !j.rows?.length) {
    _dbgSummary('Daily summary', ['No rows yet.']);
    return;
  }
  const rows = j.rows.map(r => ({
    date: r.date,
    sunset_pred: r.predicted_sunset_soc_pct != null ? Math.round(r.predicted_sunset_soc_pct) : null,
    sunset_actual: r.actual_sunset_soc_pct != null ? Math.round(r.actual_sunset_soc_pct) : null,
    sunrise_pred: r.predicted_sunrise_soc_pct != null ? Math.round(r.predicted_sunrise_soc_pct) : null,
    sunrise_actual: r.actual_sunrise_soc_pct != null ? Math.round(r.actual_sunrise_soc_pct) : null,
  }));
  _dbgRender(`Daily sunset/sunrise predicted vs actual (${rows.length} days)`, rows);
});

$('dbg-battery-packs')?.addEventListener('click', async () => {
  const j = await _dbgFetch('/api/devices/battery_packs');
  if (!j) return;
  const rows = (j.packs || []).map(p => ({
    order: p.deviceOrder,
    sn_tail: String(p.deviceSn || '').slice(-7),
    soc: p.rb,
    input_w: p.ip,
    output_w: p.op,
    temp_c: p.it === 999 ? '—' : p.it,
    err: p.ec,
  }));
  _dbgRender(
    `Battery packs (${rows.length}, fetched_at: ${new Date((j.fetched_at || 0) * 1000).toLocaleString()})`,
    rows,
  );
});

$('dbg-raw-props')?.addEventListener('click', async () => {
  const j = await _dbgFetch('/api/debug/raw_props');
  if (!j) return;
  const props = j.props || {};
  const rows = Object.keys(props).sort().map(k => ({ key: k, value: props[k] }));
  _dbgRender(`Raw cloud properties (${j.device_sn || ''})`, rows);
});

$('dbg-cloud-probe')?.addEventListener('click', async () => {
  const j = await _dbgFetch('/api/debug/cloud_probe');
  if (!j) return;
  const out = $('debug-output');
  out.hidden = false;
  const ts = new Date().toLocaleTimeString();
  out.innerHTML = `
    <div class="dbg-block">
      <h3>Cloud probe <small class="hint">(${ts})</small></h3>
      <pre class="dbg-pre">${JSON.stringify(j.results || j, null, 2)}</pre>
    </div>` + out.innerHTML;
});

$('dbg-copy-all')?.addEventListener('click', async () => {
  const status = $('debug-status');
  status.hidden = false;
  status.textContent = 'Gathering all debug data…';
  // Run all queries in parallel — each surfaces an `error` field on
  // failure rather than rejecting, so a flaky endpoint doesn't kill
  // the whole bundle.
  const safe = (url) => fetch(url).then(r =>
    r.ok ? r.json() : { error: `HTTP ${r.status}`, url })
    .catch(e => ({ error: String(e), url }));
  const [
    forecastAccuracy,
    smartChargeAnalytics,
    dailySummary,
    batteryPacks,
    rawProps,
    cloudProbe,
    forecast,
    smartChargeStatus,
    smartChargeConfig,
    status_,
  ] = await Promise.all([
    safe('/api/forecast/accuracy'),
    safe('/api/smart_charge/analytics?days=14'),
    safe('/api/daily_summary?days=14'),
    safe('/api/devices/battery_packs'),
    safe('/api/debug/raw_props'),
    safe('/api/debug/cloud_probe'),
    safe('/api/forecast'),
    safe('/api/smart_charge/status'),
    safe('/api/smart_charge/config'),
    safe('/api/status'),
  ]);
  const bundle = {
    captured_at: new Date().toISOString(),
    active_device_sn: status_?.device?.device_sn || null,
    active_device_model: status_?.device?.model_code || null,
    active_device_name: status_?.device?.name || null,
    current_telemetry: status_?.telemetry || null,
    forecast_accuracy: forecastAccuracy,
    smart_charge_analytics: smartChargeAnalytics,
    smart_charge_status: smartChargeStatus,
    smart_charge_config: smartChargeConfig,
    daily_summary: dailySummary,
    battery_packs: batteryPacks,
    raw_cloud_props: rawProps,
    cloud_probe: cloudProbe,
    forecast: forecast,
  };
  const text = JSON.stringify(bundle, null, 2);
  try {
    await navigator.clipboard.writeText(text);
    status.textContent = `Copied ${(text.length / 1024).toFixed(1)} KB to clipboard.`;
    setTimeout(() => { status.hidden = true; }, 4000);
  } catch (e) {
    // Fallback: render in the panel so the user can manually copy.
    status.textContent = 'Clipboard blocked — bundle rendered below for manual copy.';
    const out = $('debug-output');
    out.hidden = false;
    out.innerHTML = `
      <div class="dbg-block">
        <h3>All debug data <small class="hint">(${new Date().toLocaleTimeString()})</small></h3>
        <pre class="dbg-pre" style="max-height:600px">${text.replace(/</g, '&lt;')}</pre>
      </div>` + out.innerHTML;
  }
});

$('dbg-clear')?.addEventListener('click', () => {
  const out = $('debug-output');
  out.innerHTML = '';
  out.hidden = true;
  $('debug-status').hidden = true;
});

// ============================================================
// SETTINGS TAB
// ============================================================
async function loadSettings() {
  const fields = $('settings-fields');
  const status = $('settings-status');
  if (!fields) return;
  status.hidden = true;
  fields.innerHTML = 'Loading…';
  try {
    const r = await fetch('/api/settings');
    const j = await r.json();
    renderSettingsFields(j.settings || []);
  } catch (e) {
    fields.innerHTML = `<div class="login-error">Failed to load: ${e.message || e}</div>`;
  }
}

function renderSettingsFields(specs) {
  const fields = $('settings-fields');
  fields.innerHTML = '';
  for (const s of specs) {
    const row = document.createElement('div');
    row.className = 'settings-row';
    row.innerHTML = `
      <label class="settings-label" for="set-${s.key}">
        <span class="settings-label-text">${s.label}</span>
        <span class="settings-hint">${s.hint}</span>
      </label>
      <div class="settings-control">
        <input id="set-${s.key}" name="${s.key}" type="number"
               min="${s.min}" max="${s.max}" step="1"
               value="${s.value}" required />
        <span class="settings-range">${s.min}–${s.max}</span>
      </div>
    `;
    fields.appendChild(row);
  }
  // Re-apply Anthropic-key gating now that fields exist (the advisor
  // hour input requires a saved key to be editable).
  applyAnthropicGates();
}

$('settings-reload')?.addEventListener('click', loadSettings);

document.getElementById('settings-form')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const status = $('settings-status');
  const inputs = document.querySelectorAll('#settings-fields input');
  const body = {};
  for (const inp of inputs) {
    body[inp.name] = parseInt(inp.value, 10);
  }
  status.hidden = false;
  status.textContent = 'Saving…';
  try {
    const r = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      throw new Error(j.detail || j.error || ('HTTP ' + r.status));
    }
    const j = await r.json();
    status.textContent = 'Saved.';
    // Re-render with the values the server actually accepted (clamped).
    if (j.settings) {
      const inputsByName = Object.fromEntries(
        [...inputs].map(i => [i.name, i])
      );
      for (const [k, v] of Object.entries(j.settings)) {
        if (inputsByName[k]) inputsByName[k].value = v;
      }
    }
    setTimeout(() => { status.hidden = true; }, 2500);
  } catch (err) {
    status.textContent = `Save failed: ${err.message || err}`;
  }
});

// ============================================================
// COST / SAVINGS SETTINGS
// ============================================================
let _costPresets = [];

async function loadCostPlan() {
  try {
    const r = await fetch('/api/cost/plan');
    if (!r.ok) return;
    const j = await r.json();
    _costPresets = j.presets || [];
    renderCostFields(j.plan);
  } catch (e) {
    console.warn('cost plan load failed', e);
  }
}

function renderCostFields(plan) {
  const sel = $('cost-preset');
  if (!sel) return;
  // Build the preset dropdown — every preset, plus "Custom flat rate" for
  // a single user-entered rate, plus "Custom TOU" if the saved plan is a
  // TOU schedule that doesn't match any preset (typical case after a
  // user edits a preset's rates).
  sel.innerHTML = '';
  for (const p of _costPresets) {
    const opt = document.createElement('option');
    opt.value = p.id;
    opt.textContent = p.label;
    sel.appendChild(opt);
  }
  const flatOpt = document.createElement('option');
  flatOpt.value = '__custom_flat__';
  flatOpt.textContent = 'Custom flat rate';
  sel.appendChild(flatOpt);
  const customTouOpt = document.createElement('option');
  customTouOpt.value = '__custom_tou__';
  customTouOpt.textContent = 'Custom TOU';
  sel.appendChild(customTouOpt);

  // Match against presets first; fall through to "Custom TOU" or "Custom
  // flat rate" depending on the saved plan shape.
  const matchPreset = _costPresets.find((p) => deepEqualPlan(p.plan, plan));
  if (matchPreset) {
    sel.value = matchPreset.id;
  } else if (plan?.type === 'tou') {
    sel.value = '__custom_tou__';
  } else {
    sel.value = '__custom_flat__';
  }

  if (plan?.type === 'flat') {
    $('cost-flat-rate').value = plan.rate_per_kwh;
  }
  // applyCostSelection wires the visible rows to the dropdown choice. For
  // a custom TOU plan we want to render the SAVED plan's slots, not the
  // preset's, so handle that branch directly.
  if (sel.value === '__custom_tou__') {
    $('cost-flat-row').hidden = true;
    $('cost-tou-row').hidden = false;
    renderTouEditor(JSON.parse(JSON.stringify(plan)));
  } else {
    applyCostSelection();
  }
}

function deepEqualPlan(a, b) {
  if (!a || !b || a.type !== b.type) return false;
  if (a.type === 'flat') return a.rate_per_kwh === b.rate_per_kwh;
  if (a.type === 'tou') {
    const sa = a.tou_rates || [];
    const sb = b.tou_rates || [];
    if (sa.length !== sb.length) return false;
    for (let i = 0; i < sa.length; i++) {
      if (sa[i].start_hour !== sb[i].start_hour) return false;
      if (sa[i].end_hour !== sb[i].end_hour) return false;
      if (Math.abs(sa[i].rate - sb[i].rate) > 1e-6) return false;
    }
    return true;
  }
  return false;
}

// Tracks the currently-rendered TOU plan so the save handler can recover
// the slot windows + labels. Only the per-slot RATES are user-editable;
// start/end hours are utility-defined and stay locked to the preset.
let _costTouCurrent = null;

// Compact "Jun-Sep" / "Oct-May" / etc. label from a months list.
function _monthsToString(months) {
  if (!months || !months.length) return '';
  const NAMES = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  // If the months form a single contiguous run (mod 12), show "first-last".
  // Otherwise, list them.
  const sorted = [...new Set(months)].sort((a, b) => a - b);
  // Try contiguous detection in normal order
  let contiguous = true;
  for (let i = 1; i < sorted.length; i++) {
    if (sorted[i] !== sorted[i - 1] + 1) { contiguous = false; break; }
  }
  if (contiguous) return `${NAMES[sorted[0] - 1]}–${NAMES[sorted[sorted.length - 1] - 1]}`;
  // Try contiguous wrapping (e.g. winter = 1,2,3,4,5,10,11,12 wraps Oct-May)
  const present = new Set(sorted);
  // Find the start of the longest gap
  for (let start = 1; start <= 12; start++) {
    if (!present.has(start)) continue;
    if (present.has(start - 1 < 1 ? 12 : start - 1)) continue;
    let m = start, count = 0;
    while (present.has(m)) {
      count++;
      m = m === 12 ? 1 : m + 1;
      if (count > 12) break;
    }
    if (count === sorted.length) {
      const end = m === 1 ? 12 : m - 1;
      return `${NAMES[start - 1]}–${NAMES[end - 1]}`;
    }
  }
  return sorted.map(m => NAMES[m - 1]).join(',');
}

function renderTouEditor(plan) {
  _costTouCurrent = plan;
  const wrap = $('cost-tou-slots');
  if (!wrap) return;
  wrap.innerHTML = '';
  for (const [i, slot] of (plan.tou_rates || []).entries()) {
    const row = document.createElement('div');
    row.className = 'cost-tou-slot';
    const start = String(slot.start_hour).padStart(2, '0');
    const end   = String(slot.end_hour).padStart(2, '0');
    const monthsStr = _monthsToString(slot.months);
    const timeText = monthsStr
      ? `${monthsStr} · ${start}:00–${end}:00`
      : `${start}:00–${end}:00`;
    row.innerHTML = `
      <span class="slot-time">${timeText}</span>
      <span><input type="number" class="slot-rate" data-idx="${i}"
             step="0.001" min="0" max="5"
             value="${slot.rate.toFixed(3)}" /> $/kWh</span>
      <span class="slot-label">${slot.label || ''}</span>
    `;
    wrap.appendChild(row);
  }
}

function applyCostSelection() {
  const sel = $('cost-preset');
  if (!sel) return;
  const id = sel.value;
  const flatRow = $('cost-flat-row');
  const touRow  = $('cost-tou-row');
  if (id === '__custom_flat__') {
    flatRow.hidden = false;
    touRow.hidden = true;
    _costTouCurrent = null;
    return;
  }
  const preset = _costPresets.find((p) => p.id === id);
  if (!preset) return;
  if (preset.plan.type === 'flat') {
    flatRow.hidden = false;
    touRow.hidden = true;
    _costTouCurrent = null;
    $('cost-flat-rate').value = preset.plan.rate_per_kwh;
  } else if (preset.plan.type === 'tou') {
    flatRow.hidden = true;
    touRow.hidden = false;
    // Deep-copy so the user editing rates doesn't mutate _costPresets.
    renderTouEditor(JSON.parse(JSON.stringify(preset.plan)));
  }
}

$('cost-preset')?.addEventListener('change', applyCostSelection);

document.getElementById('cost-form')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const status = $('cost-status');
  const sel = $('cost-preset');
  const id = sel.value;
  let plan;
  if (id === '__custom_flat__') {
    plan = {
      type: 'flat',
      rate_per_kwh: parseFloat($('cost-flat-rate').value) || 0,
      currency: 'USD',
    };
  } else if (_costTouCurrent) {
    // TOU: walk the editable rate inputs and rebuild the plan from
    // _costTouCurrent (which holds the current windows + labels).
    const inputs = document.querySelectorAll('#cost-tou-slots input.slot-rate');
    const touRates = (_costTouCurrent.tou_rates || []).map((slot, i) => {
      const inp = inputs[i];
      const editedRate = inp ? parseFloat(inp.value) : slot.rate;
      const out = {
        start_hour: slot.start_hour,
        end_hour: slot.end_hour,
        rate: Number.isFinite(editedRate) ? editedRate : slot.rate,
        label: slot.label || '',
      };
      // Preserve the seasonal `months` filter so the saved plan keeps
      // its per-season grouping.
      if (slot.months && slot.months.length) {
        out.months = [...slot.months];
      }
      return out;
    });
    plan = {
      type: 'tou',
      currency: _costTouCurrent.currency || 'USD',
      tou_rates: touRates,
    };
  } else {
    const preset = _costPresets.find((p) => p.id === id);
    if (!preset) return;
    plan = preset.plan;
  }
  status.hidden = false;
  status.textContent = 'Saving…';
  try {
    const r = await fetch('/api/cost/plan', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(plan),
    });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      throw new Error(j.detail || ('HTTP ' + r.status));
    }
    const j = await r.json();
    if (j.plan) renderCostFields(j.plan);  // re-anchor on the saved plan
    status.textContent = 'Saved.';
    setTimeout(() => { status.hidden = true; }, 2500);
  } catch (err) {
    status.textContent = `Save failed: ${err.message || err}`;
  }
});

// ============================================================
// SMART CHARGE
// ============================================================
// Load config + Kasa device list + recent decisions into the form.
// Called on Settings tab activation; re-runs are cheap.
async function loadSmartCharge() {
  // Render only when the form actually exists in the DOM (Automation tab).
  if (!$('sc-form')) return;
  const dev = activeJackeryDevice();
  const deviceSn = dev?.device_sn || '';
  const lbl = $('sc-device-label');
  if (lbl) lbl.textContent = dev?.name ? `· ${dev.name}` : '';
  if (!deviceSn) {
    // Without a selected device we can't load per-device config.
    const status = $('sc-status');
    if (status) {
      status.hidden = false;
      status.textContent = 'No active device — pick one to configure smart charge.';
    }
    return;
  }
  try {
    const q = `?device_sn=${encodeURIComponent(deviceSn)}`;
    const kasaQ = `?jackery_sn=${encodeURIComponent(deviceSn)}`;
    const [cfgRes, kasaRes, statusRes, anaRes] = await Promise.all([
      fetch(`/api/smart_charge/config${q}`),
      // Only show Kasa plugs assigned to THIS Jackery (or legacy unassigned).
      fetch(`/api/kasa/saved${kasaQ}`),
      fetch(`/api/smart_charge/status${q}`),
      fetch(`/api/smart_charge/analytics?days=14&device_sn=${encodeURIComponent(deviceSn)}`),
    ]);
    const cfg = cfgRes.ok ? (await cfgRes.json()).config || {} : {};
    const kasa = kasaRes.ok ? await kasaRes.json() : { devices: [] };
    const status = statusRes.ok ? await statusRes.json() : {};
    const ana = anaRes.ok ? await anaRes.json() : { summary: {} };

    // Populate Kasa picker. Show alias + last 4 of host so dupes don't
    // collide. Preserve current selection if the host still exists.
    const sel = $('sc-kasa-host');
    const currentHost = cfg.kasa_device_host || '';
    sel.innerHTML = '<option value="">— none —</option>';
    for (const d of (kasa.devices || [])) {
      const opt = document.createElement('option');
      opt.value = d.host;
      const label = d.alias || d.host;
      const tail = d.host ? ` (${d.host.slice(-7)})` : '';
      opt.textContent = `${label}${tail}`;
      if (d.host === currentHost) opt.selected = true;
      sel.appendChild(opt);
    }
    if (currentHost && !Array.from(sel.options).some(o => o.value === currentHost)) {
      // Saved host no longer in registry — show it anyway so the user
      // doesn't silently lose the binding.
      const opt = document.createElement('option');
      opt.value = currentHost;
      opt.textContent = `${currentHost} (not in registry)`;
      opt.selected = true;
      sel.appendChild(opt);
    }

    // Hydrate the form fields. Defaults match smart_charge.DEFAULT_CONFIG.
    $('sc-mode').value = cfg.mode || 'off';
    $('sc-target-soc').value = cfg.target_sunrise_soc_pct ?? 25;
    // Prefer a saved value; if unsaved AND the server has observed a
    // charging rate from this device's own samples, prefill that
    // instead of the population fallback.
    const observedW = status?.observed_max_charge_w ?? null;
    const observedN = status?.observed_max_charge_n ?? 0;
    const inp = $('sc-max-charge-w');
    if (cfg.max_charge_w != null) {
      inp.value = cfg.max_charge_w;
    } else if (observedW) {
      inp.value = observedW;
    } else {
      inp.value = 800;  // matches DEFAULT_CONFIG.max_charge_w
    }
    // Render the hint dynamically — show what THIS device actually
    // pulled if we've seen enough charging samples; otherwise a
    // generic "we'll learn this once you charge" message.
    const hint = $('sc-max-charge-hint');
    if (hint) {
      if (observedW && observedN >= 6) {
        hint.innerHTML =
          `Observed AC charging rate for this device: <strong>~${observedW} W</strong> ` +
          `(95th percentile across ${observedN} samples ≥ 100W). ` +
          `Set this to whatever your unit actually pulls — the prefill is what the dashboard observed, ` +
          `not the marketing max.`;
      } else {
        hint.innerHTML =
          `AC charging rate this device pulls from the wall while the smart-charge plug is on. ` +
          `The dashboard learns this from observed charging events on this device ` +
          `(${observedN}/6 samples so far) — the hint will update once we've seen a few minutes of charging.`;
      }
    }
    $('sc-max-on-min').value = cfg.max_on_duration_minutes ?? 480;
    $('sc-claude-toggle').checked = !!cfg.claude_enabled;

    // Refresh the API-key gate every time the panel loads — the user
    // may have just saved or cleared the key on Settings.
    try {
      const r = await fetch('/api/anthropic/key');
      _anthropicHasKey = r.ok ? !!(await r.json()).has_key : false;
    } catch { _anthropicHasKey = false; }
    applyClaudeToggleGate();

    renderSmartChargeHistory(status.history || []);
    renderSmartChargeAnalytics(ana);
  } catch (e) {
    console.warn('smart_charge load failed', e);
  }
}

function renderSmartChargeAnalytics(j) {
  const el = $('sc-analytics');
  if (!el) return;
  const s = j?.summary || {};
  if (!s.n) {
    el.hidden = true;
    return;
  }
  const hitRate = s.target_hit_rate != null
    ? `${Math.round(s.target_hit_rate * 100)}%` : '—';
  const mae = s.mae_pp != null ? `${s.mae_pp.toFixed(1)} pp` : '—';
  el.innerHTML = `
    <div class="sc-ana-grid">
      <div><span class="sc-lbl">Samples (${j.days || 14}d)</span><span>${s.n}</span></div>
      <div><span class="sc-lbl">Target hit rate</span><span>${hitRate}</span></div>
      <div><span class="sc-lbl">Mean abs error</span><span>${mae}</span></div>
    </div>`;
  el.hidden = false;
}

// Compact decision timestamp. The full toLocaleString form
// ("4/30/2026, 11:54:27 AM") wraps awkwardly in the 140px column and
// the year/seconds add no value when scanning a list. Show:
//   - Same day:  "11:54 AM"
//   - Yesterday: "Yesterday 11:54 AM"
//   - Earlier:   "Apr 30, 11:54 AM"  (year only when different year)
function _formatDecisionTime(epochS) {
  const d = new Date((epochS || 0) * 1000);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const yesterday = new Date(now); yesterday.setDate(now.getDate() - 1);
  const isYesterday = d.toDateString() === yesterday.toDateString();
  const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  if (sameDay) return time;
  if (isYesterday) return `Yesterday ${time}`;
  const sameYear = d.getFullYear() === now.getFullYear();
  const date = d.toLocaleDateString([], sameYear
    ? { month: 'short', day: 'numeric' }
    : { month: 'short', day: 'numeric', year: 'numeric' });
  return `${date}, ${time}`;
}

function renderSmartChargeHistory(rows) {
  const block = $('sc-history-block');
  const list = $('sc-history');
  if (!block || !list) return;
  if (!rows.length) {
    block.hidden = true;
    return;
  }
  block.hidden = false;
  // Cap at 50 rows in the DOM — older ones live in the DB and the
  // analytics block above the list summarizes them. With the new
  // scrolling container this still lets the user review a full day's
  // worth of decisions without the panel taking over the page.
  list.innerHTML = rows.slice(0, 50).map((r) => {
    const when = _formatDecisionTime(r.decided_at);
    const fullWhen = new Date((r.decided_at || 0) * 1000).toLocaleString();
    const pred = r.predicted_sunrise_soc_pct != null
      ? `${Math.round(r.predicted_sunrise_soc_pct)}%` : '—';
    const actual = r.actual_sunrise_soc_pct != null
      ? `${Math.round(r.actual_sunrise_soc_pct)}%` : '—';
    const target = r.target_sunrise_soc_pct != null
      ? `${Math.round(r.target_sunrise_soc_pct)}%` : '—';
    const actionCls = r.action === 'on' ? 'sc-act-on'
                    : r.action === 'off' ? 'sc-act-off'
                    : 'sc-act-skip';
    const safe = (s) => String(s || '').replace(/[<>&"]/g, (c) =>
      ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
    const narrationLine = r.narration
      ? `<div class="sc-narration">💬 ${safe(r.narration)}</div>`
      : '';
    return `
      <div class="sc-row">
        <span class="sc-when" title="${fullWhen}">${when}</span>
        <span class="sc-action ${actionCls}">${(r.action || '?').toUpperCase()}</span>
        <span class="sc-mode">[${r.mode || '?'}]</span>
        <span class="sc-pred">predicted ${pred} → actual ${actual} (target ${target})</span>
        <span class="sc-reason" title="${safe(r.reason || '')}">${safe(r.reason || '')}</span>
        <button class="btn btn-ghost btn-small sc-detail-btn"
                data-decided-at="${r.decided_at}" type="button">Details ▾</button>
      </div>${narrationLine}
      <div class="sc-detail-panel" data-detail-for="${r.decided_at}" hidden></div>`;
  }).join('');
  // Wire the Details toggles. Each click hits the decision_details
  // endpoint and renders into the matching panel.
  list.querySelectorAll('[data-decided-at]').forEach((btn) => {
    btn.addEventListener('click', () => toggleDecisionDetails(btn));
  });
}

async function toggleDecisionDetails(btn) {
  const decidedAt = btn.dataset.decidedAt;
  const panel = document.querySelector(`.sc-detail-panel[data-detail-for="${decidedAt}"]`);
  if (!panel) return;
  if (!panel.hidden) {
    panel.hidden = true;
    btn.textContent = 'Details ▾';
    return;
  }
  btn.textContent = 'Details ▴';
  panel.hidden = false;
  panel.innerHTML = '<div class="hint" style="padding:8px 12px">Loading…</div>';
  try {
    const dev = activeJackeryDevice();
    const params = `?decided_at=${encodeURIComponent(decidedAt)}` +
      (dev?.device_sn ? `&device_sn=${encodeURIComponent(dev.device_sn)}` : '');
    const r = await fetch(`/api/smart_charge/decision_details${params}`);
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      throw new Error(j.detail || `HTTP ${r.status}`);
    }
    const j = await r.json();
    panel.innerHTML = renderDecisionDetailsHtml(j);
    panel.querySelector('[data-action="copy-detail"]')?.addEventListener('click', () => {
      copyDecisionDetails(j);
    });
  } catch (e) {
    panel.innerHTML = `<div class="hint" style="padding:8px 12px; color:#ef4444">Failed: ${e.message || e}</div>`;
  }
}

function renderDecisionDetailsHtml(j) {
  const safe = (s) => String(s == null ? '' : s).replace(/[<>&"]/g, (c) =>
    ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
  const fmtTs = (ts) => ts ? new Date(ts * 1000).toLocaleString() : '—';
  const fmtTime = (ts) => ts ? new Date(ts * 1000).toLocaleTimeString() : '—';
  const d = j.decision || {};
  const fields = [
    ['decided_at', fmtTs(d.decided_at)],
    ['mode', d.mode],
    ['action', d.action],
    ['executed', d.executed ? 'yes' : 'no'],
    ['reason', d.reason],
    ['current_soc_pct', d.current_soc_pct != null ? `${d.current_soc_pct}%` : '—'],
    ['predicted_sunrise_soc_pct', d.predicted_sunrise_soc_pct != null ? `${d.predicted_sunrise_soc_pct}%` : '—'],
    ['actual_sunrise_soc_pct', d.actual_sunrise_soc_pct != null ? `${d.actual_sunrise_soc_pct}%` : '—'],
    ['target_sunrise_soc_pct', d.target_sunrise_soc_pct != null ? `${d.target_sunrise_soc_pct}%` : '—'],
    ['deficit_kwh', d.deficit_kwh != null ? `${d.deficit_kwh.toFixed(2)} kWh` : '—'],
    ['charge_window', d.window_start && d.window_end ? `${fmtTime(d.window_start)} → ${fmtTime(d.window_end)}` : '—'],
    ['sunrise_ts', fmtTs(d.sunrise_ts)],
    ['cheapest_rate', d.cheapest_rate != null ? `$${d.cheapest_rate.toFixed(3)}/kWh` : '—'],
  ];
  const fieldsHtml = fields.map(([k, v]) => `
    <div class="dd-field"><span class="dd-key">${safe(k)}</span><span class="dd-val">${safe(v)}</span></div>`).join('');

  const params = (j.resolved_params || []).map(p => `
    <div class="dd-field"><span class="dd-key">${safe(p.label || p.key)}</span>
      <span class="dd-val">${p.value == null ? '—' : (
        p.unit === 'ratio' ? Number(p.value).toFixed(3) : `${Math.round(Number(p.value))} ${safe(p.unit)}`
      )} <small class="hint">[${safe(p.source)}${p.n_samples ? `, n=${p.n_samples}` : ''}]</small></span></div>`).join('');

  const fcRows = (j.forecast_trace || []).map(f => `
    <tr><td>${fmtTime(f.target)}</td><td>${Math.round(f.predicted_soc)}%</td></tr>`).join('');
  const fcMadeAt = j.forecast_made_at
    ? ` <small class="hint">(snapshot taken ${fmtTs(j.forecast_made_at)})</small>` : '';
  const fcTable = fcRows
    ? `<table class="dd-table"><thead><tr><th>target</th><th>predicted SOC</th></tr></thead><tbody>${fcRows}</tbody></table>${fcMadeAt}`
    : '<div class="hint">No forecast trace stored at this decision time.</div>';

  const wRows = (j.weather || []).map(w => `
    <tr><td>${fmtTime(w.ts)}</td><td>${w.ghi_w_m2 || 0}</td><td>${w.cloud_cover_pct || 0}%</td></tr>`).join('');
  const wTable = wRows
    ? `<table class="dd-table"><thead><tr><th>hour</th><th>GHI W/m²</th><th>cloud</th></tr></thead><tbody>${wRows}</tbody></table>`
    : '<div class="hint">No weather observations in window.</div>';

  const sRows = (j.samples_trace || []).map(s => `
    <tr><td>${fmtTime(s.ts)}</td><td>${s.soc != null ? `${s.soc}%` : '—'}</td>
        <td>${s.input_w || 0}W</td><td>${s.output_w || 0}W</td><td>${s.solar_w || 0}W</td></tr>`).join('');
  const sTable = sRows
    ? `<table class="dd-table"><thead><tr><th>hour</th><th>SOC</th><th>in</th><th>out</th><th>solar</th></tr></thead><tbody>${sRows}</tbody></table>`
    : '<div class="hint">No samples after this decision yet.</div>';

  return `
    <div class="dd-wrap">
      <div class="dd-actions">
        <button class="btn btn-primary btn-small" data-action="copy-detail" type="button">Copy all (paste-ready)</button>
      </div>
      <div class="dd-section"><h4>Decision row</h4><div class="dd-grid">${fieldsHtml}</div></div>
      <div class="dd-section"><h4>Resolved device parameters (current)</h4><div class="dd-grid">${params || '<div class="hint">none</div>'}</div></div>
      <div class="dd-section"><h4>Forecast trace at decision time</h4>${fcTable}</div>
      <div class="dd-section"><h4>Weather inputs (next 24h)</h4>${wTable}</div>
      <div class="dd-section"><h4>Actual SOC trajectory after decision</h4>${sTable}</div>
    </div>`;
}

async function copyDecisionDetails(j) {
  // Build a paste-ready plain-text dump. Same shape as the AI insights
  // Copy-all button so the chat assistant has a consistent format to
  // parse.
  const fmtTs = (ts) => ts ? new Date(ts * 1000).toISOString() : '—';
  const lines = [];
  const d = j.decision || {};
  lines.push(`# Smart-charge decision details — exported ${new Date().toISOString()}`);
  lines.push(`# device_sn=${j.device_sn || '?'}`);
  lines.push('');
  lines.push('## Decision row');
  for (const [k, v] of Object.entries(d)) {
    let val = v;
    if (typeof val === 'number' && k.endsWith('_at') || k === 'sunrise_ts'
        || k === 'window_start' || k === 'window_end') {
      val = `${v} (${fmtTs(v)})`;
    }
    lines.push(`- ${k}: ${val ?? '—'}`);
  }
  lines.push('');
  lines.push('## Resolved device parameters (current values)');
  for (const p of (j.resolved_params || [])) {
    lines.push(`- ${p.key}: ${p.value} ${p.unit || ''} [source=${p.source}${p.n_samples ? `, n=${p.n_samples}` : ''}]`);
  }
  lines.push('');
  lines.push(`## Forecast trace${j.forecast_made_at ? ` (snapshot taken ${fmtTs(j.forecast_made_at)})` : ''}`);
  for (const f of (j.forecast_trace || [])) {
    lines.push(`- ${fmtTs(f.target)}  predicted_soc=${f.predicted_soc}`);
  }
  if (!(j.forecast_trace || []).length) lines.push('  (none)');
  lines.push('');
  lines.push('## Weather inputs');
  for (const w of (j.weather || [])) {
    lines.push(`- ${fmtTs(w.ts)}  ghi=${w.ghi_w_m2 || 0} W/m²  cloud=${w.cloud_cover_pct || 0}%`);
  }
  if (!(j.weather || []).length) lines.push('  (none)');
  lines.push('');
  lines.push('## Actual SOC trajectory after decision');
  for (const s of (j.samples_trace || [])) {
    lines.push(`- ${fmtTs(s.ts)}  SOC=${s.soc ?? '—'}%  in=${s.input_w || 0}W  out=${s.output_w || 0}W  solar=${s.solar_w || 0}W`);
  }
  if (!(j.samples_trace || []).length) lines.push('  (none)');
  const text = lines.join('\n');
  try {
    await navigator.clipboard.writeText(text);
    const status = $('alg-status') || $('sc-status');
    if (status) {
      status.hidden = false;
      status.textContent = `Copied ${text.length} chars to clipboard.`;
      setTimeout(() => { status.hidden = true; }, 2500);
    }
  } catch {
    // Fallback: textarea overlay.
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;top:8vh;left:5vw;width:90vw;height:70vh;z-index:9999;font-family:monospace;font-size:12px';
    document.body.appendChild(ta);
    ta.select();
    setTimeout(() => {
      const dismiss = (ev) => {
        if (ev.target === ta) return;
        ta.remove();
        document.removeEventListener('click', dismiss, true);
      };
      document.addEventListener('click', dismiss, true);
    }, 50);
  }
}

function renderSmartChargePlan(plan, narration) {
  const el = $('sc-current-plan');
  if (!el) return;
  if (!plan) { el.hidden = true; return; }
  const fmtHour = (ts) => new Date(ts * 1000).toLocaleTimeString([], {
    hour: '2-digit', minute: '2-digit',
  });
  // Discontinuous schedule: render the planned hours grouped into
  // contiguous segments so a 4-hour split (e.g. 18-21 + 04-05) shows
  // as two distinct ON blocks. Falls back to legacy window_start/end
  // on older decision rows that don't carry planned_hours.
  let scheduleStr = '—';
  if (Array.isArray(plan.planned_hours) && plan.planned_hours.length) {
    const hrs = [...plan.planned_hours].sort((a, b) => a - b);
    const segments = [];
    let segStart = hrs[0];
    let segEnd = hrs[0] + 3600;
    for (let i = 1; i < hrs.length; i++) {
      if (hrs[i] === segEnd) {
        segEnd = hrs[i] + 3600;
      } else {
        segments.push([segStart, segEnd]);
        segStart = hrs[i];
        segEnd = hrs[i] + 3600;
      }
    }
    segments.push([segStart, segEnd]);
    scheduleStr = segments
      .map(([s, e]) => `${fmtHour(s)}–${fmtHour(e)}`)
      .join(', ');
  } else if (plan.window_start && plan.window_end) {
    scheduleStr = `${fmtHour(plan.window_start)}–${fmtHour(plan.window_end)}`;
  }
  const sunrise = plan.sunrise_ts ? fmtHour(plan.sunrise_ts) : '—';
  const rate = plan.cheapest_rate != null
    ? `$${plan.cheapest_rate.toFixed(3)}/kWh` : '—';
  const actionCls = plan.action === 'on' ? 'sc-act-on'
                  : plan.action === 'off' ? 'sc-act-off'
                  : 'sc-act-skip';
  const safe = (s) => String(s || '').replace(/[<>&"]/g, (c) =>
    ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
  const narrationLine = narration
    ? `<div class="sc-narration" style="margin-top:10px; padding-left:0">💬 ${safe(narration)}</div>`
    : '';
  // Counterfactual lives next to display predicted so the user can see
  // when smart-charge is doing real work vs coasting on solar.
  const baseline = plan.baseline_predicted_sunrise_soc_pct;
  const baselineLine = baseline != null
    ? `<div><span class="sc-lbl">No-AC sunrise SOC</span><span>${Math.round(baseline)}%</span></div>`
    : '';
  const extensionBadge = plan.extension_active
    ? '<span class="sc-mode" title="Past planned window, holding ON until target hit">EXTENSION</span>'
    : '';
  el.innerHTML = `
    <div class="sc-plan-header">
      <span class="sc-action ${actionCls}">${(plan.action || '?').toUpperCase()}</span>
      <span class="sc-mode">[${plan.mode || '?'}]</span>${extensionBadge}
      <span class="sc-reason">${plan.reason || ''}</span>
    </div>
    <div class="sc-plan-grid">
      <div><span class="sc-lbl">Current SOC</span><span>${plan.current_soc_pct != null ? Math.round(plan.current_soc_pct) + '%' : '—'}</span></div>
      <div><span class="sc-lbl">Predicted at sunrise</span><span>${plan.predicted_sunrise_soc_pct != null ? Math.round(plan.predicted_sunrise_soc_pct) + '%' : '—'}</span></div>
      ${baselineLine}
      <div><span class="sc-lbl">Target</span><span>${Math.round(plan.target_sunrise_soc_pct)}%</span></div>
      <div><span class="sc-lbl">Deficit</span><span>${plan.deficit_kwh != null ? plan.deficit_kwh.toFixed(2) + ' kWh' : '—'}</span></div>
      <div><span class="sc-lbl">Charge schedule</span><span>${scheduleStr}</span></div>
      <div><span class="sc-lbl">Sunrise</span><span>${sunrise}</span></div>
      <div><span class="sc-lbl">Cheapest rate</span><span>${rate}</span></div>
    </div>${narrationLine}`;
  el.hidden = false;
}

document.getElementById('sc-form')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const status = $('sc-status');
  const deviceSn = activeJackeryDevice()?.device_sn;
  if (!deviceSn) {
    status.hidden = false;
    status.textContent = 'No active device — cannot save.';
    return;
  }
  status.hidden = false;
  status.textContent = 'Saving…';
  const cfg = {
    mode: $('sc-mode').value,
    kasa_device_host: $('sc-kasa-host').value || null,
    target_sunrise_soc_pct: parseInt($('sc-target-soc').value, 10) || 25,
    max_charge_w: parseInt($('sc-max-charge-w').value, 10) || 800,
    max_on_duration_minutes: parseInt($('sc-max-on-min').value, 10) || 480,
    claude_enabled: $('sc-claude-toggle').checked,
  };
  try {
    const r = await fetch(`/api/smart_charge/config?device_sn=${encodeURIComponent(deviceSn)}`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(cfg),
    });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      throw new Error(j.detail || `HTTP ${r.status}`);
    }
    status.textContent = 'Saved.';
    setTimeout(() => { status.hidden = true; }, 2500);
  } catch (err) {
    status.textContent = `Save failed: ${err.message || err}`;
  }
});

document.getElementById('sc-evaluate')?.addEventListener('click', async () => {
  const status = $('sc-status');
  const deviceSn = activeJackeryDevice()?.device_sn;
  status.hidden = false;
  status.textContent = 'Evaluating…';
  try {
    const url = deviceSn
      ? `/api/smart_charge/evaluate_now?device_sn=${encodeURIComponent(deviceSn)}`
      : '/api/smart_charge/evaluate_now';
    const r = await fetch(url, { method: 'POST' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const j = await r.json();
    renderSmartChargePlan(j.plan, j.narration);
    status.textContent = j.narration
      ? 'Plan computed + narrated.'
      : 'Plan computed (no execution).';
    setTimeout(() => { status.hidden = true; }, 3500);
  } catch (err) {
    status.textContent = `Evaluate failed: ${err.message || err}`;
  }
});

document.getElementById('sc-backtest')?.addEventListener('click', async () => {
  const status = $('sc-status');
  const deviceSn = activeJackeryDevice()?.device_sn;
  if (!deviceSn) {
    status.hidden = false;
    status.textContent = 'No active device.';
    return;
  }
  const days = parseInt($('sc-backtest-days').value, 10) || 7;
  const targetRaw = $('sc-backtest-target').value;
  const params = new URLSearchParams({ device_sn: deviceSn, days: String(days) });
  if (targetRaw) params.set('target_override', targetRaw);
  status.hidden = false;
  status.textContent = `Replaying ${days}d of decisions…`;
  try {
    const r = await fetch(`/api/smart_charge/backtest?${params}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const j = await r.json();
    renderBacktestResult(j);
    status.textContent = `Replayed ${j.summary?.n || 0} decisions, ${j.summary?.n_flipped || 0} would flip.`;
    setTimeout(() => { status.hidden = true; }, 4500);
  } catch (err) {
    status.textContent = `Backtest failed: ${err.message || err}`;
  }
});

function renderBacktestResult(j) {
  const el = $('sc-backtest-result');
  if (!el) return;
  if (!j || !j.summary) { el.hidden = true; return; }
  const s = j.summary;
  const safe = (x) => String(x ?? '').replace(/[<>&"]/g, (c) =>
    ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
  const fmtTs = (ts) => new Date(ts * 1000).toLocaleString([], {
    month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  });
  // Flip pairs as a compact key:value list.
  const flipPairsStr = Object.entries(s.flip_pairs || {})
    .map(([k, v]) => `${k}: ${v}`).join(', ') || '—';
  // Show first 30 flipped rows + first 10 errors so the user can
  // spot-check; full results are in the JSON response if they want
  // more.
  const flipped = (j.results || []).filter(r => r.would_flip).slice(0, 30);
  const errors = (j.results || []).filter(r => r.error).slice(0, 10);
  const flipRowsHtml = flipped.map(r => `
    <tr>
      <td>${fmtTs(r.ts)}</td>
      <td>${safe(r.old_action)}</td>
      <td><b>${safe(r.new_action)}</b></td>
      <td>${r.soc != null ? Math.round(r.soc) + '%' : '—'}</td>
      <td>${r.target}%</td>
      <td>${r.new_baseline_sunrise != null ? Math.round(r.new_baseline_sunrise) + '%' : '—'}</td>
      <td>${(r.planned_hours || []).length}</td>
      <td>${r.extension_active ? 'ext' : ''}</td>
      <td style="font-size:11px; color:#9aa0a6">${safe(r.new_reason)}</td>
    </tr>
  `).join('');
  const errRowsHtml = errors.map(r => `
    <tr><td>${fmtTs(r.ts)}</td><td colspan="8" style="color:#e57373">${safe(r.error)}</td></tr>
  `).join('');
  const targetNote = j.target_override != null
    ? ` <span style="color:#fbbc04">(target override: ${j.target_override}%)</span>`
    : '';
  el.innerHTML = `
    <h3 style="margin:0 0 8px; font-size:14px">Backtest — last ${j.days}d${targetNote}</h3>
    <div style="font-size:13px; line-height:1.7">
      <div><b>${s.n}</b> decisions replayed · <b>${s.n_flipped}</b> would flip · <b>${s.n_extension}</b> in extension · <b>${s.n_error}</b> errors</div>
      <div>old: ${Object.entries(s.by_action_old || {}).map(([k,v])=>`${k}=${v}`).join(', ')}</div>
      <div>new: ${Object.entries(s.by_action_new || {}).map(([k,v])=>`${k}=${v}`).join(', ')}</div>
      <div>flips: ${flipPairsStr}</div>
      <div style="margin-top:6px; color:#9aa0a6; font-size:12px">capacity ${j.capacity_wh} Wh · max charge ${j.max_charge_w} W · tz ${j.tz_offset_seconds}s</div>
    </div>
    ${flipped.length ? `
      <table style="width:100%; margin-top:10px; font-size:12px; border-collapse:collapse">
        <thead><tr style="text-align:left; color:#9aa0a6">
          <th>when</th><th>old</th><th>new</th><th>soc</th><th>target</th>
          <th>baseline</th><th>plan h</th><th></th><th>reason</th>
        </tr></thead>
        <tbody>${flipRowsHtml}</tbody>
      </table>
    ` : '<div style="margin-top:8px; color:#81c784">No flips — current code matches recorded decisions.</div>'}
    ${errors.length ? `<table style="width:100%; margin-top:10px; font-size:12px"><tbody>${errRowsHtml}</tbody></table>` : ''}
  `;
  el.hidden = false;
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
$('logout')?.addEventListener('click', async () => {
  if (!confirm('Sign out of the Jackery Monitor?')) return;
  try { await fetch('/api/auth/logout', { method: 'POST' }); }
  catch (_e) {}
  window.location.replace('/login');
});

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
  const prevDeviceSn = activeJackeryDevice()?.device_sn;
  lastStatus = s;
  // Stash for fetchBatteryPacks() — it derives the main unit's standalone
  // SOC from the combined SOC the dashboard is showing.
  window._lastStatus = s.telemetry || null;
  // If the active Jackery device changed and we're on the Automation tab,
  // re-render the rules list so the "Showing rules for: X" filter follows.
  const newDeviceSn = activeJackeryDevice()?.device_sn;
  if (activeTab === 'automation' && prevDeviceSn !== newDeviceSn) {
    if (_allRules.length) renderRulesWithFilter();
    // Smart-charge config + AI insights are per-device — reload both.
    loadSmartCharge();
    loadAlgorithmAdvisor();
  }
  // Same idea for the Forecast tab: forecast is per-device (battery
  // capacity, solar regression, load profile all differ), so re-fetch on
  // device switch. The EOD badge on the battery card is also per-device.
  if (prevDeviceSn !== newDeviceSn && newDeviceSn) {
    if (activeTab === 'forecast') {
      forecastCache = null;
      fetchForecast();
    }
    fetchEodForecast();
    // Pack list is per-device — drop the previous device's cache so we
    // don't briefly show the old packs while the new fetch is in flight.
    // The next WS tick will populate cachedPacks from s.battery_packs.
    window._cachedPacks = [];
    window._cachedPackDeviceSn = null;
    window._cachedPacksMainSoc = null;
    window._cachedPacksError = null;
    window._cachedNoPacks = false;
    window._systemSoc = null;
    window._mainSoc = null;
    renderBatteryPacks();
    // AC-charge button is per-device too — hide it eagerly so the
    // user doesn't see the old device's plug state for up to 30s
    // until the next periodic refresh fires. refreshAcChargeButton()
    // re-shows it with the correct state if the new device has a
    // plug configured.
    const acBtn = $('ac-charge-toggle');
    if (acBtn) acBtn.hidden = true;
    refreshAcChargeButton().catch(() => {});
  }

  // Pack data piggy-backs on the WS payload — the bridge has it pushed
  // from MQTT in real time, so this updates per-pack rows at the same
  // cadence as the SOC card (no separate HTTP polling needed).
  if (Array.isArray(s.battery_packs)) {
    window._cachedPacks = s.battery_packs;
    window._cachedPacksMainSoc = s.telemetry?.main_soc_pct ?? null;
    window._cachedPacksError = null;
    window._cachedNoPacks = s.battery_packs.length === 0;
  }

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
    // Prefer the server-computed system SOC when packs are attached,
    // so the SOC card lands on the right number on the very first
    // paint (no main → system flash). Falls back to the raw main %
    // for single-unit devices where system_soc_pct is absent.
    const headlineSoc = t.system_soc_pct != null
      ? t.system_soc_pct
      : t.battery_percent;
    animateNumber($('battery-pct'), headlineSoc);
    $('battery-bar-fill').style.width = `${Math.max(0, Math.min(100, headlineSoc))}%`;
    // Single source of truth for the temp segment in the SOC card meta:
    // hide it whenever the telemetry tells us packs are present (the
    // host unit's temp moves to the Main row in the pack card). Show
    // it on single-unit devices where system_soc_pct is absent.
    const tempGroup = $('battery-temp-group');
    if (tempGroup) tempGroup.hidden = t.system_soc_pct != null;
    // EOD pill follows live SOC drift so it doesn't go stale between refreshes.
    maybeRefitEodOnDrift(t.battery_percent);
    // Re-render the pack card with the live main % — pack values lag
    // (server polls every 5 min) but the main % updates every tick, so
    // this keeps the Main row + system SOC overlay in sync without
    // waiting for the next pack fetch.
    renderBatteryPacks();
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
    // Charging — show ETA to full so the label is never ambiguous
    if (ttFull > 0) {
      timeLabel = `${fmt(ttFull, 1)} h until full`;
    } else {
      const wh = ((100 - soc) / 100) * PACK_KWH * 1000;
      const eta = wh / netW;
      timeLabel = eta > 0 && isFinite(eta) ? `${fmt(eta, 1)} h until full` : 'Charging…';
    }
  } else {
    // Discharging — show ETA to empty
    if (ttEmpty > 0) {
      timeLabel = `${fmt(ttEmpty, 1)} h until empty`;
    } else {
      const wh = (soc / 100) * PACK_KWH * 1000;
      const eta = wh / Math.abs(netW);
      timeLabel = eta > 0 && isFinite(eta) ? `${fmt(eta, 1)} h until empty` : 'Discharging…';
    }
  }
  $('battery-time').textContent = timeLabel;
  if (t.battery_temp_c != null) $('battery-temp').textContent = formatTemp(t.battery_temp_c);

  // Animated power flow diagram (replaces the old two-column W readout).
  renderPowerFlow(t);

  // Battery card mood: charging / discharging / low / idle. Drives the soft
  // glow + bar color via CSS classes.
  const batCard = $('battery-card');
  if (batCard) {
    const inW = Number(t.input_power_w ?? 0);
    const outW = Number(t.output_power_w ?? 0);
    const net = inW - outW;
    batCard.classList.toggle('charging',    net >  25);
    batCard.classList.toggle('discharging', net < -25);
    batCard.classList.toggle('low',         (t.battery_percent ?? 100) <= 20);
  }

  // Today KPIs from energy aggregator
  if (s.energy?.today) {
    $('today-out-kwh').textContent = fmtKwh(s.energy.today.output_wh);
    $('today-in-kwh').textContent  = fmtKwh(s.energy.today.input_wh);
  }
  // Cost savings row in the TODAY card. Server only populates this when
  // the cost plan loads cleanly; keep the row hidden otherwise.
  if (s.energy) {
    renderSavingsRow('today',
      s.energy.today_savings,
      s.energy.cost_plan?.currency || 'USD');
  }

  // Output state. If a toggle is pending and the device hasn't propagated
  // yet, hold the expected value and keep the .pending tag so the user
  // sees the change "stick" instead of flapping back to the old state.
  const now = Date.now();
  for (const port of ['ac', 'dc', 'usb', 'car']) {
    const v = t[`${port}_on`];
    const sw = document.querySelector(`.switch[data-port="${port}"]`);
    const lbl = $(`sw-${port}`);
    if (!sw || !lbl) continue;
    const pending = _pendingToggle[port];
    if (pending && pending.until > now) {
      if (v === pending.expected) {
        // Telemetry has caught up — apply normally and clear pending.
        delete _pendingToggle[port];
        sw.classList.remove('pending');
        sw.classList.toggle('on', v);
        lbl.textContent = v ? 'ON' : 'OFF';
      } else {
        // Still propagating — hold the optimistic value, mark as pending.
        sw.classList.add('pending');
        sw.classList.toggle('on', pending.expected);
        lbl.textContent = pending.expected ? 'ON' : 'OFF';
      }
      continue;
    }
    // No (or expired) pending toggle — render the actual telemetry.
    if (pending) { delete _pendingToggle[port]; sw.classList.remove('pending'); }
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
  _lastCloudMeta = s.cloud || null;
  renderPausePill();
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

// ============================================================
// AC CHARGING — manual override of the smart-charge plug.
// Renders only when the user has assigned a Kasa plug to this
// device's smart-charge config. Inlined as the 5th button in the
// Outputs row; auto-hides when no plug is configured. When
// smart-charge mode is 'active' the button gets a yellow border
// and a tooltip explaining the loop will re-decide on the next
// 5-min tick (Q4-A: warn but don't block).
// ============================================================
let _acChargeBusy = false;
let _acChargeLastDeviceSn = null;

async function refreshAcChargeButton() {
  const btn = $('ac-charge-toggle');
  if (!btn) return;
  const deviceSn = activeJackeryDevice()?.device_sn;
  if (!deviceSn) { btn.hidden = true; return; }
  let cfg = null;
  try {
    const r = await fetch(`/api/smart_charge/config?device_sn=${encodeURIComponent(deviceSn)}`);
    if (r.ok) cfg = (await r.json()).config;
  } catch (e) { /* ignore */ }
  const host = cfg?.kasa_device_host;
  if (!host) { btn.hidden = true; return; }
  btn.hidden = false;
  const mode = cfg.mode || 'off';
  if (mode === 'active') {
    btn.classList.add('warn');
    btn.title = `Plug ${host} · smart-charge ${mode}\nWarning: your manual toggle may be reverted within 5 min.`;
  } else {
    btn.classList.remove('warn');
    btn.title = `Plug ${host} · smart-charge ${mode}`;
  }
  // Read plug state. Failures = unknown (—).
  const stateEl = $('ac-charge-state');
  if (!_acChargeBusy) {
    try {
      const r = await fetch(`/api/kasa/status?host=${encodeURIComponent(host)}`);
      if (r.ok) {
        const j = await r.json();
        const isOn = !!j.is_on;
        btn.classList.toggle('on', isOn);
        if (stateEl) stateEl.textContent = isOn ? 'ON' : 'OFF';
      } else {
        btn.classList.remove('on');
        if (stateEl) stateEl.textContent = '—';
      }
    } catch (e) {
      btn.classList.remove('on');
      if (stateEl) stateEl.textContent = '—';
    }
  }
}

document.getElementById('ac-charge-toggle')?.addEventListener('click', async () => {
  if (_acChargeBusy) return;
  const deviceSn = activeJackeryDevice()?.device_sn;
  if (!deviceSn) return;
  let host = null;
  try {
    const r = await fetch(`/api/smart_charge/config?device_sn=${encodeURIComponent(deviceSn)}`);
    if (r.ok) host = (await r.json()).config?.kasa_device_host;
  } catch (e) { return; }
  if (!host) return;
  const btn = $('ac-charge-toggle');
  const stateEl = $('ac-charge-state');
  const wantOn = !btn?.classList.contains('on');
  _acChargeBusy = true;
  if (btn) btn.classList.add('pending');
  if (stateEl) stateEl.textContent = wantOn ? 'ON…' : 'OFF…';
  try {
    const r = await fetch('/api/kasa/test', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ host, on: wantOn }),
    });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      throw new Error(j.detail || `HTTP ${r.status}`);
    }
    // Optimistic update + clear any stale error decoration left over
    // from a prior failed click. refreshAcChargeButton fires on the
    // next 30s tick to confirm the actual plug state.
    if (btn) {
      btn.classList.toggle('on', wantOn);
      btn.classList.remove('pending', 'warn');
    }
    if (stateEl) stateEl.textContent = wantOn ? 'ON' : 'OFF';
    _acChargeBusy = false;
    refreshAcChargeButton().catch(() => {}); // re-sync title + state
  } catch (e) {
    // Backend already retried 3x (kasa_client._with_retry) before
    // bubbling up here, so by the time we see this the plug is
    // genuinely unreachable or returning an auth error. Two fixes
    // for the "stuck on err / refresh required" pain:
    //   1. Re-sync to actual plug state so the button reflects truth
    //      (may have succeeded server-side even if the response was
    //      lost) — user can immediately click again to retry.
    //   2. Surface the error as a tooltip + yellow border, NOT as a
    //      replacement button label. State stays readable.
    _acChargeBusy = false;
    if (btn) btn.classList.remove('pending');
    await refreshAcChargeButton().catch(() => {});
    if (btn) {
      btn.classList.add('warn');
      btn.title = `Last toggle failed: ${e.message || e}\nClick to retry. Hover for details.`;
    }
  }
});

// Periodic refresh — plug state changes infrequently so 30s is plenty.
setInterval(refreshAcChargeButton, 30_000);
// Initial render — fire once at module load AND once when applyStatus
// resolves an active device, so the button shows up without waiting 30s.
refreshAcChargeButton().catch(() => {});

// ============================================================
// POWER FLOW DIAGRAM (Tesla-app style)
// Each flow has three SVG pieces: a solid translucent CHANNEL,
// a hidden geometry PATH, and a DOT group with animateMotion that
// follows the path. When watts < 5: channel goes faint (idle), dot
// hidden. When watts ≥ 5: channel brightens, dot becomes visible
// and travels with dur set inversely to wattage. animateMotion's
// dur is updated via setAttribute on the inner <animateMotion>
// element — Chrome/Safari pick up the new duration without a
// restart hack.
// ============================================================

// Threshold — anything below ~5W is inverter rounding noise, not flow.
const FLOW_IDLE_W = 5;

function renderPowerFlow(t) {
  if (!t) return;
  const solarW = Math.max(0, Math.round(Number(t.solar_input_w ?? 0)));
  const gridW  = Math.max(0, Math.round(Number(t.ac_input_w    ?? 0)));
  const loadW  = Math.max(0, Math.round(Number(t.output_power_w ?? 0)));
  setFlow('solar', solarW);
  setFlow('grid',  gridW);
  setFlow('load',  loadW);

  // Battery node — color encodes direction. Net = sources - load.
  // Charging when sources outpace load, discharging the other way.
  const batteryNode = $('flow-node-battery');
  if (batteryNode) {
    const net = (solarW + gridW) - loadW;
    batteryNode.classList.toggle('charging',    net >  25);
    batteryNode.classList.toggle('discharging', net < -25);
  }

  // Battery SOC text. Use the same headline SOC as the main battery
  // card (system_soc_pct when available, else main %) so the two
  // numbers always agree.
  const headlineSoc = t.system_soc_pct != null
    ? t.system_soc_pct
    : t.battery_percent;
  const socEl = $('flow-soc-pct');
  if (socEl) {
    socEl.textContent = headlineSoc != null
      ? `${Math.round(headlineSoc)}%`
      : '—';
  }

  // AC voltage / Hz hint in the card eyebrow.
  const meta = $('flow-ac-meta');
  if (meta) {
    const parts = [];
    if (t.ac_output_v != null) parts.push(`${Math.round(t.ac_output_v)} V`);
    if (t.ac_output_v_l1 && t.ac_output_v_l1 > 0) {
      parts.push(`L1 ${Math.round(t.ac_output_v_l1)} V`);
    }
    if (t.ac_output_hz != null) parts.push(`${Math.round(t.ac_output_hz)} Hz`);
    meta.textContent = parts.join(' · ') || '—';
  }
}

function setFlow(kind, watts) {
  const channel = document.getElementById(`flow-line-${kind}`);
  const node    = document.getElementById(`flow-node-${kind}`);
  const dotGrp  = document.getElementById(`flow-dot-${kind}`);
  const wEl     = document.getElementById(`flow-${kind}-w`);
  if (wEl) wEl.textContent = watts;

  const active = watts >= FLOW_IDLE_W;
  if (channel) channel.classList.toggle('active', active);
  if (node)    node.classList.toggle('active', active);
  if (dotGrp)  dotGrp.classList.toggle('active', active);

  if (dotGrp) {
    const am = dotGrp.querySelector('animateMotion');
    if (am) {
      if (active) {
        // Speed mapping: faster dot = more power, but capped so
        // 2 kW doesn't look frantic. 50 W → 4.5 s slow drift,
        // 500 W → 2.7 s, 2 kW+ → 1.2 s. Smooth log-ish curve.
        const dur = Math.max(1.2, Math.min(4.5, 4500 / Math.max(50, watts)));
        am.setAttribute('dur', `${dur.toFixed(2)}s`);
      } else {
        // Stop wasting cycles on a hidden dot.
        am.setAttribute('dur', '999s');
      }
    }
  }
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

  // Cost / savings rows. Server returns today_savings + lifetime_savings
  // alongside the kWh totals when the cost plan loads cleanly. If either
  // is absent, hide the corresponding row.
  const currency = e.cost_plan?.currency || 'USD';
  renderSavingsRow('today',    e.today_savings,    currency);
  renderSavingsRow('lifetime', e.lifetime_savings, currency);
}

function fmtMoney(amount, currency = 'USD') {
  if (amount == null || Number.isNaN(amount)) return '—';
  try {
    return new Intl.NumberFormat(undefined,
      { style: 'currency', currency, maximumFractionDigits: 2 }).format(amount);
  } catch {
    return `$${Number(amount).toFixed(2)}`;
  }
}

function renderSavingsRow(prefix, savings, currency) {
  const row = $(`${prefix}-savings-row`);
  if (!row) return;
  if (!savings) { row.hidden = true; return; }
  const setText = (id, txt) => { const el = $(id); if (el) el.textContent = txt; };
  const idPrefix = prefix === 'today' ? 'today' : 'life';
  setText(`${idPrefix}-solar-savings`, `+${fmtMoney(savings.solar_savings, currency)} solar`);
  setText(`${idPrefix}-grid-cost`,     `−${fmtMoney(savings.grid_cost, currency)} grid`);
  const net = savings.net_savings ?? 0;
  setText(`${idPrefix}-net-savings`, `${net >= 0 ? '+' : ''}${fmtMoney(net, currency)} net`);
  row.hidden = false;
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

// Build a smooth Catmull-Rom path through `pts` (array of [x,y]) and trace
// it onto ctx. Tension 0.5 = classic Catmull-Rom, gentle curves, no
// overshoot. Skips null entries cleanly (lifts pen + restarts).
function _smoothPath(ctx, pts) {
  let i = 0, started = false;
  while (i < pts.length) {
    if (!pts[i]) { started = false; i++; continue; }
    const p0 = pts[i - 1] && pts[i - 1] || pts[i];
    const p1 = pts[i];
    const p2 = pts[i + 1] || p1;
    const p3 = pts[i + 2] || p2;
    if (!started) { ctx.moveTo(p1[0], p1[1]); started = true; i++; continue; }
    const cp1x = p0[0] + (p2[0] - p0[0]) / 6;
    const cp1y = p0[1] + (p2[1] - p0[1]) / 6;
    const cp2x = p2[0] - (p3[0] - p1[0]) / 6;
    const cp2y = p2[1] - (p3[1] - p1[1]) / 6;
    ctx.bezierCurveTo(cp1x, cp1y, cp2x, cp2y, p2[0], p2[1]);
    i++;
  }
}

// Stroke a smooth line through a series of values. `xs(i)`/`ys(v)` map
// data-space to canvas-space; null/NaN values lift the pen.
function drawSmoothLine(ctx, points, xs, ys, color, lineWidth = 2.5) {
  const pts = points.map((v, i) =>
    (v == null || Number.isNaN(v)) ? null : [xs(i), ys(v)]);
  ctx.strokeStyle = color;
  ctx.lineWidth = lineWidth;
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  ctx.beginPath();
  _smoothPath(ctx, pts);
  ctx.stroke();
}

// Same shape as drawSmoothLine, but fills the area below the curve down to
// `baseY` with a vertical gradient (color at top, transparent at bottom).
function drawAreaFill(ctx, points, xs, ys, baseY, color, alphaTop = 0.25) {
  const pts = points.map((v, i) =>
    (v == null || Number.isNaN(v)) ? null : [xs(i), ys(v)]);
  // Walk segments — we may have null gaps; fill each contiguous run
  // separately so a gap doesn't yield a polygon spanning the whole gap.
  let runStart = -1;
  for (let i = 0; i <= pts.length; i++) {
    const here = pts[i];
    if (here && runStart < 0) runStart = i;
    if ((!here || i === pts.length) && runStart >= 0) {
      const run = pts.slice(runStart, i);
      if (run.length >= 1) {
        const minY = Math.min(...run.map((p) => p[1]));
        const grad = ctx.createLinearGradient(0, minY, 0, baseY);
        grad.addColorStop(0, _withAlpha(color, alphaTop));
        grad.addColorStop(1, _withAlpha(color, 0));
        ctx.fillStyle = grad;
        ctx.beginPath();
        ctx.moveTo(run[0][0], baseY);
        ctx.lineTo(run[0][0], run[0][1]);
        _smoothPath(ctx, run);
        ctx.lineTo(run[run.length - 1][0], baseY);
        ctx.closePath();
        ctx.fill();
      }
      runStart = -1;
    }
  }
}

function _withAlpha(color, a) {
  // accept "#RRGGBB" or "#RGB"
  if (color[0] === '#') {
    let hex = color.slice(1);
    if (hex.length === 3) hex = hex.split('').map((c) => c + c).join('');
    const r = parseInt(hex.slice(0, 2), 16);
    const g = parseInt(hex.slice(2, 4), 16);
    const b = parseInt(hex.slice(4, 6), 16);
    return `rgba(${r},${g},${b},${a})`;
  }
  return color;  // best-effort passthrough
}

// Backwards-compat wrapper used by the energy chart's overlay line. Same
// signature as the old drawSeries; just routes through the new smooth path.
function drawSeries(ctx, points, color, padL, padR, padT, padB, w, h, minY, maxY) {
  if (!points.length) return;
  const xs = (i) => padL + (i / Math.max(1, points.length - 1)) * (w - padL - padR);
  const ys = (v) => h - padB - ((v - minY) / Math.max(1e-6, maxY - minY)) * (h - padT - padB);
  drawSmoothLine(ctx, points, xs, ys, color);
}

// Hover tooltip — created on demand, shared by all charts. Returns a setter
// (call with {x, y, html} to position+show, or null to hide).
let _tooltipEl = null;
function _ensureTooltip() {
  if (_tooltipEl) return _tooltipEl;
  const t = document.createElement('div');
  t.className = 'chart-tooltip';
  t.hidden = true;
  document.body.appendChild(t);
  _tooltipEl = t;
  return t;
}
function chartTooltip(target) {
  const el = _ensureTooltip();
  if (!target) { el.hidden = true; return; }
  el.hidden = false;
  el.innerHTML = target.html;
  // Position relative to viewport, but offset above the cursor.
  const left = Math.min(window.innerWidth - el.offsetWidth - 8, target.x + 12);
  const top  = Math.max(8, target.y - el.offsetHeight - 12);
  el.style.left = left + 'px';
  el.style.top  = top + 'px';
}

function drawLiveChart(s) {
  const canvas = $('chart-live');
  if (!canvas) return;
  const { ctx, w, h } = setCanvasSize(canvas);
  ctx.clearRect(0, 0, w, h);
  const padL = 44, padR = 44, padT = 14, padB = 28;

  const hist = (s?.history) || [];
  if (!hist.length) {
    ctx.fillStyle = '#6b7280'; ctx.font = '12px Inter';
    ctx.fillText('Waiting for data…', padL + 8, padT + 16);
    return;
  }
  const out = hist.map(p => p.output_power_w);
  const inp = hist.map(p => p.input_power_w);
  const bat = hist.map(p => p.battery_percent);
  const maxW = Math.max(50, ...out, ...inp);

  const xs = (i) => padL + (i / Math.max(1, hist.length - 1)) * (w - padL - padR);
  const yWatts = (v) => (h - padB) - (v / Math.max(1e-6, maxW)) * (h - padT - padB);
  const yPct   = (v) => (h - padB) - (v / 100) * (h - padT - padB);
  const baseY  = h - padB;

  // Subtle horizontal gridlines (4)
  ctx.strokeStyle = 'rgba(35,42,51,.7)';
  ctx.lineWidth = 1;
  for (let i = 1; i <= 4; i++) {
    const y = padT + ((h - padT - padB) * i) / 5;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
  }
  drawAxes(ctx, w, h, padL, padR, padT, padB);

  // Left Y-axis labels (Watts)
  ctx.fillStyle = '#6b7280'; ctx.font = '11px Inter';
  ctx.textAlign = 'right';
  for (let i = 0; i <= 4; i++) {
    const v = (maxW * (4 - i)) / 4;
    const y = padT + ((h - padT - padB) * i) / 4;
    ctx.fillText(`${Math.round(v)}`, padL - 6, y + 3);
  }
  // Right Y-axis labels (Battery %)
  ctx.textAlign = 'left';
  ctx.fillStyle = 'rgba(251,191,36,.65)';
  for (const v of [0, 50, 100]) {
    ctx.fillText(`${v}%`, w - padR + 4, yPct(v) + 3);
  }
  ctx.textAlign = 'start';

  // X-axis time ticks — first sample timestamp ... last (clamped to "now").
  if (hist.length >= 2) {
    const first = hist[0].ts || 0, last = hist[hist.length - 1].ts || 0;
    const fmtMs = (ms) => new Date(ms * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    ctx.fillStyle = '#6b7280';
    ctx.textAlign = 'center';
    const ticks = 5;
    for (let i = 0; i <= ticks; i++) {
      const ts = first + (last - first) * (i / ticks);
      ctx.fillText(fmtMs(ts), xs((hist.length - 1) * (i / ticks)), h - 8);
    }
    ctx.textAlign = 'start';
  }

  if (_seriesVisible.live.input) {
    drawAreaFill(ctx, inp, xs, yWatts, baseY, SERIES_COLORS.input, .22);
    drawSmoothLine(ctx, inp, xs, yWatts, SERIES_COLORS.input, 2.5);
  }
  if (_seriesVisible.live.output) {
    drawAreaFill(ctx, out, xs, yWatts, baseY, SERIES_COLORS.output, .25);
    drawSmoothLine(ctx, out, xs, yWatts, SERIES_COLORS.output, 2.5);
  }
  // Battery on its own right axis, dashed to make the different scale obvious.
  if (_seriesVisible.live.battery) {
    ctx.save();
    ctx.setLineDash([4, 4]);
    drawSmoothLine(ctx, bat, xs, yPct, SERIES_COLORS.battery, 2);
    ctx.restore();
  }

  // Hover state stored on the canvas element
  _attachChartHover(canvas, hist, (i, evt) => {
    const p = hist[i];
    const ts = p.ts ? new Date(p.ts * 1000).toLocaleTimeString() : '';
    return `<div class="cht-ts">${ts}</div>
            <div class="cht-row"><i style="background:${SERIES_COLORS.output}"></i> Output <b>${fmt(p.output_power_w)}</b> W</div>
            <div class="cht-row"><i style="background:${SERIES_COLORS.input}"></i> Input <b>${fmt(p.input_power_w)}</b> W</div>
            <div class="cht-row"><i style="background:${SERIES_COLORS.battery}"></i> Battery <b>${fmt(p.battery_percent)}</b> %</div>`;
  }, () => ({ xs, padL, padR, w, h, baseY: h - padB, padT, padB }));
}

// Generic chart-hover binder. Stores the per-render data + geometry +
// tooltip-builder on the canvas element so the (one-time) listener always
// reads the freshest closures, not stale ones from when it was bound.
function _attachChartHover(canvas, hist, htmlFn, geomFn) {
  // Refresh per-render state every call.
  canvas._chartData = hist;
  canvas._chartHtml = htmlFn;
  canvas._chartGeom = geomFn();
  if (canvas._hoverBound) return;
  canvas._hoverBound = true;
  canvas.style.cursor = 'crosshair';
  const onMove = (e) => {
    const data = canvas._chartData;
    const geom = canvas._chartGeom;
    const html = canvas._chartHtml;
    if (!data?.length || !geom || !html) return;
    const rect = canvas.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const t = (x - geom.padL) / (geom.w - geom.padL - geom.padR);
    const idx = Math.max(0, Math.min(data.length - 1, Math.round(t * (data.length - 1))));
    // Redraw the chart, then overlay the crosshair.
    if (canvas._redraw) canvas._redraw();
    const ctx = canvas.getContext('2d');
    const xPos = geom.xs(idx);
    ctx.strokeStyle = 'rgba(255,255,255,.18)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(xPos, geom.padT); ctx.lineTo(xPos, geom.baseY); ctx.stroke();
    chartTooltip({ x: e.clientX, y: e.clientY, html: html(idx, e) });
  };
  const onLeave = () => chartTooltip(null);
  canvas.addEventListener('mousemove', onMove);
  canvas.addEventListener('mouseleave', onLeave);
  canvas._redraw = () => drawLiveChart(lastStatus);
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

  // Bars: consumed (left of slot) + charged (right of slot), interleaved.
  const n = hist.length;
  const totalW = w - padL - padR;
  const slot = totalW / n;
  const barW = Math.max(1, Math.min(slot * 0.4, 16));
  for (let i = 0; i < n; i++) {
    const xCenter = padL + slot * (i + 0.5);
    const yBase = h - padB;
    if (_seriesVisible.energy.output) {
      const yOut = yBase - (out[i] / maxWh) * (h - padT - padB);
      ctx.fillStyle = SERIES_COLORS.output;
      ctx.fillRect(xCenter - barW - 1, yOut, barW, yBase - yOut);
    }
    if (_seriesVisible.energy.input) {
      const yIn = yBase - (inp[i] / maxWh) * (h - padT - padB);
      ctx.fillStyle = SERIES_COLORS.input;
      ctx.fillRect(xCenter + 1, yIn, barW, yBase - yIn);
    }
  }

  if (_seriesVisible.energy.battery && bat.some(v => v != null)) {
    ctx.strokeStyle = SERIES_COLORS.battery;
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
  _chartRedraw[activeTab]?.();
});

// ============================================================
// FORECAST TAB
// ============================================================
async function fetchForecast() {
  const needsConfig = $('forecast-needs-config');
  const content     = $('forecast-content');
  const stats       = $('forecast-stats');
  const heading     = needsConfig.querySelector('h2');
  const body        = needsConfig.querySelector('p');
  const showNeedsConfig = (h, p) => {
    needsConfig.hidden = false;
    content.hidden = true;
    stats.hidden = true;
    if (h) heading.textContent = h;
    if (p) body.textContent = p;
  };
  try {
    const r = await fetch('/api/forecast');
    if (!r.ok) { showNeedsConfig(); return; }
    const j = await r.json();
    if (!j.configured) {
      showNeedsConfig('Allow location to enable forecasts',
        'The forecaster needs your approximate location to know which weather to fetch. Click below to share it.');
      return;
    }
    if (j.error) { showNeedsConfig(j.error, body.textContent); return; }
    if (j.ready === false) {
      const r = j.readiness || {};
      const haveH = r.have_hours ?? 0, needH = r.needed_hours ?? 24;
      const haveW = r.have_idle_windows ?? 0, needW = r.needed_idle_windows ?? 5;
      showNeedsConfig(
        'Forecaster is calibrating',
        `Need ${needH}h of history (${haveH}h captured) and ` +
        `${needW} clean discharge windows (${haveW} so far). ` +
        `Once enough data accumulates, the forecast will appear here ` +
        `automatically — no action required.`
      );
      return;
    }
    needsConfig.hidden = true;
    content.hidden = false;
    stats.hidden = false;
    forecastCache = j;
    const setText = (id, v, d = 0) => { const el = $(id); if (el) el.textContent = fmt(v, d); };
    const dev = activeJackeryDevice();
    const title = $('forecast-title');
    if (title) {
      title.textContent = dev?.name
        ? `${dev.name} — next 5 days`
        : 'State of charge — next 5 days';
    }
    setText('forecast-capacity', j.capacity_wh);
    setText('forecast-coeff',    j.solar_coefficient, 2);
    setText('forecast-avg-load', j.overall_load_w);
    const sub = $('forecast-coeff-sub');
    if (sub) {
      const minFit = 2;
      if (j.solar_coefficient === 0) {
        sub.textContent = 'no solar production detected on this device';
      } else if (j.fit_samples >= minFit) {
        sub.textContent = `learned from ${j.fit_samples} hourly samples`;
      } else {
        const need = Math.max(1, minFit - j.fit_samples);
        sub.textContent = `default — need ${need} more daylight hours of data to fit`;
      }
    }
    drawForecastChart(j);
  } catch (e) { console.warn('forecast fetch failed', e); }
}

// Run once on app boot: if the server has no location yet AND the user
// hasn't previously denied the prompt in this browser, ask. After any
// denial, set a localStorage flag so we don't pester them on every reload.
// The Forecast tab still has a manual "Use my location" button for retry.
async function maybePromptLocationOnBoot() {
  if (localStorage.getItem('jackery-location-denied') === '1') return;
  try {
    const r = await fetch('/api/location');
    if (!r.ok) return;
    const j = await r.json();
    if (j.latitude != null && j.longitude != null) return; // already set
  } catch { return; }
  const result = await requestAndSaveGeolocation();
  if (result.denied) {
    localStorage.setItem('jackery-location-denied', '1');
  } else if (result.ok) {
    // Location just saved — populate the EOD badge right away rather than
    // waiting for the hourly tick.
    fetchEodForecast();
  }
}

// Trigger the browser geolocation prompt and POST the result to the server.
// Returns {ok, denied}. `denied` is only true when the user explicitly
// rejected the permission (code 1) — transient errors don't count.
async function requestAndSaveGeolocation() {
  const needsConfig = $('forecast-needs-config');
  const heading     = needsConfig?.querySelector('h2');
  const body        = needsConfig?.querySelector('p');
  const setMessage  = (h, p) => {
    if (heading) heading.textContent = h;
    if (body)    body.textContent    = p;
  };

  if (!('geolocation' in navigator)) {
    setMessage('Location is needed to forecast',
      'Your browser does not support geolocation. There is no other way to enable forecasts on this build.');
    return { ok: false, denied: false };
  }
  if (!window.isSecureContext) {
    setMessage('Location is needed to forecast',
      'Browser geolocation only works over HTTPS. Open the dashboard via your HTTPS (Cloudflare) URL and try again.');
    return { ok: false, denied: false };
  }

  const coords = await new Promise((resolve) => {
    navigator.geolocation.getCurrentPosition(
      (pos) => resolve({ ok: true, lat: pos.coords.latitude, lon: pos.coords.longitude }),
      (err) => resolve({ ok: false, err }),
      { timeout: 15000, maximumAge: 24 * 3600 * 1000 },
    );
  });

  if (!coords.ok) {
    const code = coords.err?.code;
    const msg = code === 1
      ? 'Location is needed to forecast. You denied the permission — re-allow it in your browser site settings, or click below to try again.'
      : code === 2
      ? 'Could not determine your location. Try again in a moment.'
      : code === 3
      ? 'Location request timed out. Click below to try again.'
      : 'Could not get your location. Click below to try again.';
    setMessage('Location is needed to forecast', msg);
    return { ok: false, denied: code === 1 };
  }

  try {
    const r = await fetch('/api/location', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ latitude: coords.lat, longitude: coords.lon }),
    });
    if (!r.ok) {
      setMessage('Could not save location', `${r.status} ${r.statusText}`);
      return { ok: false, denied: false };
    }
    return { ok: true, denied: false };
  } catch (e) {
    setMessage('Could not save location', e.message || String(e));
    return { ok: false, denied: false };
  }
}

// "Use my location" button — manual retry. Clears the denied flag so the
// user explicitly opting in unsticks any earlier dismissal.
document.addEventListener('click', async (e) => {
  if (e.target?.id !== 'forecast-geo-btn') return;
  localStorage.removeItem('jackery-location-denied');
  const result = await requestAndSaveGeolocation();
  if (result.ok) {
    fetchForecast();
    fetchEodForecast();
  }
});

// EOD-pill: predicted SOC at the next sun phase boundary.
//   • During daytime (solar > 0 right now)  → "At sunset" — the SOC at
//     the last daylight hour today (just before solar drops to 0).
//   • During nighttime (solar = 0 right now) → "At sunrise" — the SOC at
//     the last dark hour tonight (just before solar returns).
// Refit every 30 min via setInterval, OR opportunistically when the live
// telemetry shows the actual SOC has drifted >1pp from the value the
// last forecast was anchored on. Drift refits are rate-limited to 5 min
// minimum spacing so we don't hammer /api/forecast on every WS tick.
let _eodAnchorSOC = null;
let _eodLastFetchAt = 0;
const EOD_DRIFT_THRESHOLD_PCT = 1.0;
const EOD_MIN_REFRESH_INTERVAL_MS = 5 * 60_000;

async function fetchEodForecast() {
  const el = $('eod-forecast');
  if (!el) return;
  _eodLastFetchAt = Date.now();
  try {
    const r = await fetch('/api/forecast');
    if (!r.ok) { el.hidden = true; return; }
    const j = await r.json();
    if (!j.configured || j.error || !Array.isArray(j.forecast) || !j.forecast.length) {
      el.hidden = true;
      return;
    }
    const fc = j.forecast;
    // The first forecast hour represents "now-ish"; its solar tells us
    // whether we're currently in daylight or darkness.
    const isDayNow = (fc[0]?.solar_w || 0) > 0;
    let i = 0;
    let label;
    if (isDayNow) {
      // Walk forward to the first night hour. fc[i-1] is the last
      // daylight hour = SOC at sunset.
      while (i < fc.length && (fc[i].solar_w || 0) > 0) i++;
      label = 'At sunset';
    } else {
      // Walk forward to the next daylight hour. fc[i-1] is the last
      // dark hour = SOC at sunrise (overnight low).
      while (i < fc.length && (fc[i].solar_w || 0) <= 0) i++;
      label = 'At sunrise';
    }
    if (i === 0 || i >= fc.length) {
      el.hidden = true;
      return;
    }
    const best = fc[i - 1];
    if (best.predicted_soc == null) {
      el.hidden = true;
      return;
    }
    const labelEl = el.querySelector('.eod-label');
    if (labelEl) labelEl.textContent = label;

    const pct = $('eod-pct');
    const trend = $('eod-trend');
    pct.textContent = Math.round(best.predicted_soc);
    const start = j.starting_soc_pct ?? best.predicted_soc;
    _eodAnchorSOC = start;
    const delta = best.predicted_soc - start;
    trend.classList.remove('up', 'down');
    if (Math.abs(delta) < 1) {
      trend.textContent = '→';
    } else if (delta > 0) {
      trend.textContent = '↗';
      trend.classList.add('up');
    } else {
      trend.textContent = '↘';
      trend.classList.add('down');
    }
    const threshold = j.low_battery_threshold || 20;
    el.classList.toggle('low', best.predicted_soc < threshold);
    el.hidden = false;
  } catch (e) {
    console.warn('EOD forecast fetch failed:', e);
  }
}

// ---- Per-expansion-battery list ----
// Fetches /api/devices/battery_packs and renders one row per attached
// pack plus a "Main" row showing the host unit's actual SOC (the value
// the cloud reports as `battery_percent` on /v1/device/property).
//
// The State of Charge card shows the *combined* system SOC, computed
// here and stashed on window for applyStatus to pick up:
//   system_pct = (main_pct × main_wh + Σ pack_pct × pack_wh) / total_wh
// 5000 Plus expansion packs are 5040 Wh — same as the main unit.
// (The smaller 2042 Wh packs are for the older 1500/2000 series.)
const PACK_NOMINAL_WH = 5040;       // 5000 Plus Battery Pack 5040
const MAIN_DEFAULT_WH = 5040;       // 5000 Plus internal

function computeSystemSoc(mainPct, packs, mainWh) {
  if (mainPct == null || !packs.length || !mainWh) return null;
  const totalWh = mainWh + packs.length * PACK_NOMINAL_WH;
  let stored = mainPct * mainWh / 100;
  for (const p of packs) {
    if (p.rb != null) stored += p.rb * PACK_NOMINAL_WH / 100;
  }
  const pct = (stored / totalWh) * 100;
  if (!Number.isFinite(pct)) return null;
  return Math.max(0, Math.min(100, pct));
}

function packRow({ idx, soc, flow, flowClass, temp, label, snTitle, isMain }) {
  const cls = isMain ? 'pack-row pack-row-main' : 'pack-row';
  const socTxt = soc != null ? Math.round(soc) : '—';
  return `
    <div class="${cls}">
      <span class="pack-idx">${idx}</span>
      <span class="pack-bar"><span class="pack-bar-fill" style="width:${Math.max(0, Math.min(100, soc || 0))}%"></span></span>
      <span class="pack-soc">${socTxt}<small>%</small></span>
      <span class="pack-flow ${flowClass}">${flow}</span>
      <span class="pack-temp">${temp}</span>
      <span class="pack-sn" title="${snTitle}">${label}</span>
    </div>`;
}

async function fetchBatteryPacks() {
  const card = $('battery-packs-card');
  if (!card) return;
  try {
    const r = await fetch('/api/devices/battery_packs');
    const j = r.ok ? await r.json() : { error: `HTTP ${r.status}` };
    console.debug('battery_packs response', j);
    window._cachedPacks = Array.isArray(j.packs) ? j.packs : [];
    window._cachedPackDeviceSn = j.device_sn || null;
    window._cachedPacksMainSoc = j.main_soc_pct ?? null;
    window._cachedPacksError = j.error || null;
    window._cachedNoPacks = j.no_packs === true;
    renderBatteryPacks();
  } catch (e) {
    console.warn('battery packs fetch failed:', e);
    window._cachedPacksError = String(e);
    renderBatteryPacks();
  }
}

// Pure renderer for the Battery packs card. Uses whatever's in cache
// plus the live WS SOC if available. Idempotent and re-runnable any
// time inputs change — wire it from applyStatus() so the Main row
// paints as soon as the first WS tick lands instead of waiting for
// the next 30s pack-fetch interval.
function renderBatteryPacks() {
  const card = $('battery-packs-card');
  const list = $('battery-packs-list');
  const summary = $('battery-packs-summary');
  if (!card || !list) return;

  const packs = window._cachedPacks || [];
  const err = window._cachedPacksError;
  const isNoPackDevice = window._cachedNoPacks === true;

  // Device with no expansion packs (e.g. HomePower 3000): hide the
  // card cleanly and clear any system-SOC overlay so the SOC card
  // shows the main reading directly. Temp segment visibility is
  // managed centrally in applyStatus().
  if (isNoPackDevice && !packs.length && !err) {
    window._systemSoc = null;
    window._mainSoc = null;
    card.hidden = true;
    return;
  }

  if (!packs.length) {
    if (err) {
      summary.textContent = `error: ${err}`;
      list.innerHTML = '';
      card.hidden = false;
    } else {
      card.hidden = true;
    }
    return;
  }

  const mainPct =
    window._lastStatus?.battery_percent ??
    window._cachedPacksMainSoc ??
    null;
  const mainTempC = window._lastStatus?.battery_temp_c ?? null;
  const mainWh = window._capacityOverrideWh || MAIN_DEFAULT_WH;
  const systemSoc = computeSystemSoc(mainPct, packs, mainWh);
  window._mainWh = mainWh;
  window._systemSoc = systemSoc;
  window._mainSoc = mainPct;
  applySystemSocOverlay();


  const totalIn = packs.reduce((s, p) => s + (p.ip || 0), 0);
  const avgPack = packs.reduce((s, p) => s + (p.rb || 0), 0) / packs.length;
  const sysTxt = systemSoc != null ? ` · system ${Math.round(systemSoc)}%` : '';
  summary.textContent = `${packs.length} packs · avg ${Math.round(avgPack)}%${sysTxt} · ${totalIn}W in`;

  const rows = [];
  if (mainPct != null) {
    rows.push(packRow({
      idx: '★',
      soc: mainPct,
      flow: '',
      flowClass: 'flow-idle',
      temp: formatPackTempHtml(mainTempC, 'main'),
      label: 'Main',
      snTitle: 'Host unit (cloud-reported SOC)',
      isMain: true,
    }));
  }
  packs.forEach((p, i) => {
    const sn = String(p.deviceSn || '');
    const ip = p.ip != null ? Math.round(p.ip) : 0;
    const op = p.op != null ? Math.round(p.op) : 0;
    // deviceOrder is the cloud's authoritative pack ordering; some
    // payload paths (older WS broadcasts, the bridge cache when it
    // hasn't seen a fresh /v1/device/property yet) omit it. Fall
    // back to the array index so the user sees 1..N instead of every
    // row labeled "1".
    const order = (typeof p.deviceOrder === 'number') ? p.deviceOrder : i;
    rows.push(packRow({
      idx: order + 1,
      soc: p.rb,
      flow: ip > 0 ? `+${ip}W` : (op > 0 ? `−${op}W` : 'idle'),
      flowClass: ip > 0 ? 'flow-in' : (op > 0 ? 'flow-out' : 'flow-idle'),
      temp: formatPackTempHtml(p.it, sn.slice(-6) || sn),
      label: `…${sn.slice(-6)}`,
      snTitle: sn,
      isMain: false,
    }));
  });
  list.innerHTML = rows.join('');
  card.hidden = false;
}

// Battery packs collapse toggle. Default is collapsed; user
// preference is sticky across reloads via localStorage. The detail
// rows aren't decision-critical at glance — the header summary
// ("5 packs · avg 75% · system 76% · 237W in") tells the user
// whether anything's wrong. Open the card only when investigating.
const PACKS_COLLAPSE_KEY = 'jackery-battery-packs-collapsed';

function applyBatteryPacksCollapseState() {
  const card = document.getElementById('battery-packs-card');
  const hdr  = document.getElementById('battery-packs-toggle');
  if (!card || !hdr) return;
  const saved = localStorage.getItem(PACKS_COLLAPSE_KEY);
  // Default to collapsed when the key has never been set.
  const collapsed = saved == null ? true : (saved === '1');
  card.classList.toggle('collapsed', collapsed);
  hdr.setAttribute('aria-expanded', String(!collapsed));
}

function toggleBatteryPacksCollapsed() {
  const card = document.getElementById('battery-packs-card');
  const hdr  = document.getElementById('battery-packs-toggle');
  if (!card || !hdr) return;
  const nowCollapsed = !card.classList.contains('collapsed');
  card.classList.toggle('collapsed', nowCollapsed);
  hdr.setAttribute('aria-expanded', String(!nowCollapsed));
  localStorage.setItem(PACKS_COLLAPSE_KEY, nowCollapsed ? '1' : '0');
}

document.getElementById('battery-packs-toggle')?.addEventListener('click', toggleBatteryPacksCollapsed);
document.getElementById('battery-packs-toggle')?.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault();
    toggleBatteryPacksCollapsed();
  }
});
// Apply persisted state on load. (Body of the function tolerates
// the elements being absent so order-of-script-load doesn't matter.)
applyBatteryPacksCollapseState();

// Apply the cached system SOC to the SOC card's big number + bar.
// applyStatus() runs on every WS tick and writes the main-only SOC; we
// re-write here so the system value wins whenever packs are present.
// Idempotent — safe to call repeatedly.
function applySystemSocOverlay() {
  const sys = window._systemSoc;
  if (sys == null) return;
  const pctEl = $('battery-pct');
  const barEl = $('battery-bar-fill');
  if (pctEl) pctEl.textContent = Math.round(sys);
  if (barEl) barEl.style.width = `${sys}%`;
}

// Called from applyStatus() on every WS telemetry tick. If the actual
// SOC has drifted enough from the last forecast's anchor, refit so the
// pill stays meaningful between scheduled refreshes.
function maybeRefitEodOnDrift(currentSocPct) {
  if (_eodAnchorSOC == null || currentSocPct == null) return;
  if (Date.now() - _eodLastFetchAt < EOD_MIN_REFRESH_INTERVAL_MS) return;
  if (Math.abs(currentSocPct - _eodAnchorSOC) < EOD_DRIFT_THRESHOLD_PCT) return;
  fetchEodForecast();
}

function drawForecastChart(j) {
  const canvas = $('chart-forecast');
  if (!canvas) return;
  const { ctx, w, h } = setCanvasSize(canvas);
  ctx.clearRect(0, 0, w, h);
  const padL = 42, padR = 50, padT = 14, padB = 28;
  drawAxes(ctx, w, h, padL, padR, padT, padB);

  const fc = j?.forecast || [];
  if (!fc.length) {
    ctx.fillStyle = '#6b7280'; ctx.font = '12px Inter';
    ctx.fillText('No forecast yet — keep the monitor running and check back.', padL + 8, padT + 16);
    return;
  }

  const totalW = w - padL - padR;
  const innerH = h - padT - padB;
  const n = fc.length;
  const xAt = (i) => padL + (i / (n - 1)) * totalW;
  const ts0 = fc[0].ts, ts1 = fc[fc.length - 1].ts;

  // Threshold band — shade where predicted SOC dips below user's low-batt %.
  const threshold = j.low_battery_threshold || 20;
  const yThreshold = padT + innerH * (1 - threshold / 100);
  ctx.fillStyle = 'rgba(248, 113, 113, 0.08)';
  ctx.fillRect(padL, yThreshold, totalW, h - padB - yThreshold);
  ctx.strokeStyle = 'rgba(248, 113, 113, 0.4)';
  ctx.setLineDash([4, 4]);
  ctx.beginPath();
  ctx.moveTo(padL, yThreshold); ctx.lineTo(w - padR, yThreshold);
  ctx.stroke();
  ctx.setLineDash([]);

  const vis = _seriesVisible.forecast;
  // Right-axis grid: power scale based on whichever power series are
  // currently visible. If both are hidden, fall back to 1 so the SOC-only
  // view still draws cleanly.
  const powerSamples = [1];
  if (vis.solar) powerSamples.push(...fc.map(p => p.solar_w || 0));
  if (vis.load)  powerSamples.push(...fc.map(p => p.load_w || 0));
  const maxPower = Math.max(...powerSamples);
  const niceMax = Math.max(100, Math.ceil(maxPower / 100) * 100);

  // Left-axis: SOC % grid + labels
  ctx.strokeStyle = '#1c2128'; ctx.lineWidth = 1;
  ctx.fillStyle = '#6b7280'; ctx.font = '11px Inter';
  ctx.textAlign = 'right';
  for (let i = 0; i <= 4; i++) {
    const y = padT + (innerH * i) / 4;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
    ctx.fillText(`${100 - i * 25}%`, padL - 6, y + 3);
    ctx.fillText(`${Math.round(niceMax * (4 - i) / 4)}W`, w - padR + 36, y + 3);
  }
  ctx.textAlign = 'start';

  // X-axis labels
  const fmtTs = (ts) => {
    const d = new Date(ts * 1000);
    const span = ts1 - ts0;
    if (span > 24 * 3600) return d.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  };
  ctx.fillStyle = '#6b7280';
  const ticks = 5;
  for (let i = 0; i <= ticks; i++) {
    const idx = Math.floor((n - 1) * (i / ticks));
    ctx.fillText(fmtTs(fc[idx].ts), xAt(idx) - 18, h - 8);
  }

  if (vis.solar) {
    ctx.strokeStyle = SERIES_COLORS.input;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    fc.forEach((p, i) => {
      const x = xAt(i);
      const y = padT + innerH * (1 - (p.solar_w || 0) / niceMax);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }

  if (vis.load) {
    // Load is dashed so it visually reads as "demand" not "production".
    ctx.strokeStyle = SERIES_COLORS.output;
    ctx.lineWidth = 1.5;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    fc.forEach((p, i) => {
      const x = xAt(i);
      const y = padT + innerH * (1 - (p.load_w || 0) / niceMax);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.setLineDash([]);
  }

  if (vis.soc) {
    ctx.strokeStyle = SERIES_COLORS.battery;
    ctx.lineWidth = 2.5;
    ctx.beginPath();
    fc.forEach((p, i) => {
      const x = xAt(i);
      const y = padT + innerH * (1 - (p.predicted_soc || 0) / 100);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }
}

// ============================================================
// BOOT
// ============================================================
// ============================================================
// SCREEN WAKE LOCK ("Keep screen on" toggle for iPad kiosks)
// ============================================================
const KEEP_AWAKE_KEY = 'jackery-keep-awake';
let _wakeLock = null;

function isKeepAwakeOn() {
  return localStorage.getItem(KEEP_AWAKE_KEY) === '1';
}

async function requestWakeLock() {
  if (!('wakeLock' in navigator)) {
    return { ok: false, reason: 'not-supported' };
  }
  try {
    _wakeLock = await navigator.wakeLock.request('screen');
    _wakeLock.addEventListener('release', () => { _wakeLock = null; });
    return { ok: true };
  } catch (e) {
    return { ok: false, reason: e?.message || String(e) };
  }
}

async function releaseWakeLock() {
  if (_wakeLock) {
    try { await _wakeLock.release(); } catch {}
    _wakeLock = null;
  }
}

// Browsers release the wake lock when the tab becomes hidden. Re-acquire
// when it comes back into view, so the iPad mounted-on-wall use case
// keeps working after notifications, app switches, etc.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && isKeepAwakeOn()) {
    requestWakeLock();
  }
});

function setKeepAwakeStatus(text) {
  const el = $('keep-awake-status');
  if (el) el.textContent = text;
}

function initKeepAwakeToggle() {
  const t = $('keep-awake-toggle');
  if (!t) return;
  t.checked = isKeepAwakeOn();
  if (!('wakeLock' in navigator)) {
    setKeepAwakeStatus('— not supported on this browser (needs iOS 16.4+)');
  } else if (t.checked) {
    setKeepAwakeStatus(_wakeLock ? '— active' : '— pending');
  } else {
    setKeepAwakeStatus('');
  }
  // Reflect saved temp-unit preference on each tab open.
  syncTempUnitPills();
}

// ============================================================
// TEMPERATURE UNIT (°C / °F) — per-browser display preference
// ============================================================
// Stored in localStorage so each device/browser can have its own.
// All telemetry is captured + persisted on the server in Celsius;
// this preference only affects DISPLAY. Toggle re-renders the live
// view immediately so the user sees the change.

const TEMP_UNIT_KEY = 'jackery-temp-unit';

function getTempUnit() {
  return localStorage.getItem(TEMP_UNIT_KEY) === 'F' ? 'F' : 'C';
}

function setTempUnit(unit) {
  localStorage.setItem(TEMP_UNIT_KEY, unit === 'F' ? 'F' : 'C');
  syncTempUnitPills();
  // Re-render anything that shows a temperature so the change is
  // immediate without waiting for the next poll/MQTT push.
  if (window._lastStatus) {
    try { applyStatus(window._lastStatus); } catch { /* ignore */ }
  }
  try { renderBatteryPacks(); } catch { /* ignore */ }
}

function syncTempUnitPills() {
  const unit = getTempUnit();
  const c = $('temp-unit-c'), f = $('temp-unit-f');
  if (c) { c.classList.toggle('on', unit === 'C'); c.setAttribute('aria-checked', String(unit === 'C')); }
  if (f) { f.classList.toggle('on', unit === 'F'); f.setAttribute('aria-checked', String(unit === 'F')); }
}

// Format a Celsius value according to the user's display preference.
// `decimals` defaults to 0 (whole degrees) since the device's BMS
// only reports integer °C anyway.
function formatTemp(celsius, decimals = 0) {
  if (celsius == null || Number.isNaN(Number(celsius))) return '—';
  const c = Number(celsius);
  if (getTempUnit() === 'F') {
    const f = c * 9 / 5 + 32;
    return `${f.toFixed(decimals)}°F`;
  }
  return `${c.toFixed(decimals)}°C`;
}

// Plausible Li-ion operating range. Anything outside is almost
// certainly a fault code or unit-confused register from the BMS — the
// AI advisor's 2026-05-01 review caught two packs reporting 135°C
// while others read 76-79°C, which is thermodynamically impossible
// without thermal runaway (and the BMS would have already disconnected
// the pack at that point). We display these as "⚠?" with a tooltip
// rather than the raw number, so they don't mask real thermal issues.
const PACK_TEMP_PLAUSIBLE_C = { min: -20, max: 80 };

// Track which (pack_sn, value) combos we've already warned about so
// the console doesn't spam every render tick.
const _packTempWarned = new Set();

// Returns an HTML snippet for the pack-temp cell. Out-of-range values
// render as a "?" with a tooltip + console warning; in-range values
// pass through formatTemp so the C/F preference applies.
function formatPackTempHtml(celsius, packSn) {
  if (celsius == null || celsius === 999 || Number.isNaN(Number(celsius))) {
    return '';  // unknown / not-applicable sentinel — empty cell
  }
  const c = Number(celsius);
  if (c < PACK_TEMP_PLAUSIBLE_C.min || c > PACK_TEMP_PLAUSIBLE_C.max) {
    const key = `${packSn || 'main'}:${c}`;
    if (!_packTempWarned.has(key)) {
      _packTempWarned.add(key);
      console.warn(
        `pack temp out of range — pack ${packSn || 'main'}: ${c}°C ` +
        `(plausible band ${PACK_TEMP_PLAUSIBLE_C.min}–${PACK_TEMP_PLAUSIBLE_C.max}°C). ` +
        `Likely a BMS fault code or units mismatch on this pack's firmware.`
      );
    }
    const safe = String(c).replace(/[<>&"]/g, (ch) =>
      ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[ch]));
    const title = `Reported value ${safe}°C is outside the plausible Li-ion ` +
                  `operating range (${PACK_TEMP_PLAUSIBLE_C.min} to ${PACK_TEMP_PLAUSIBLE_C.max}°C). ` +
                  `Probably a BMS fault code or sensor glitch on this pack — ` +
                  `not a real temperature reading.`;
    return `<span class="pack-temp-fault" title="${title}">⚠?</span>`;
  }
  return formatTemp(c);
}

document.addEventListener('click', (e) => {
  const id = e.target?.id;
  if (id === 'temp-unit-c') setTempUnit('C');
  else if (id === 'temp-unit-f') setTempUnit('F');
});

document.addEventListener('change', async (e) => {
  if (e.target?.id !== 'keep-awake-toggle') return;
  const on = e.target.checked;
  localStorage.setItem(KEEP_AWAKE_KEY, on ? '1' : '0');
  if (on) {
    const res = await requestWakeLock();
    setKeepAwakeStatus(res.ok
      ? '— active (screen will stay on while this tab is visible)'
      : (res.reason === 'not-supported'
          ? '— not supported on this browser (needs iOS 16.4+)'
          : `— failed: ${res.reason}`));
  } else {
    await releaseWakeLock();
    setKeepAwakeStatus('');
  }
});

// ============================================================
// AI INSIGHTS — daily Claude advisor review
// ============================================================
// Shows pending suggestions on the Automation tab. Each suggestion
// has Apply / Dismiss buttons; nothing auto-applies. Anomalies show
// without an Apply button (purely informational).

async function loadAlgorithmAdvisor() {
  const wrap = $('alg-suggestions');
  if (!wrap) return;
  const dev = activeJackeryDevice();
  const deviceSn = dev?.device_sn;
  const params = deviceSn ? `?device_sn=${encodeURIComponent(deviceSn)}` : '';
  try {
    const r = await fetch(`/api/algorithm/suggestions${params}&status=pending`.replace(/^&/, '?'));
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const j = await r.json();
    renderAlgorithmSuggestions(j.suggestions || []);
  } catch (e) {
    wrap.innerHTML = `<div class="hint">Failed to load suggestions: ${String(e.message || e)}</div>`;
  }
}

function renderAlgorithmSuggestions(rows) {
  const wrap = $('alg-suggestions');
  const summaryEl = $('alg-summary');
  const badge = $('alg-pending-badge');
  if (!wrap) return;
  // Latest pending of kind=config has its summary surfaced; we
  // currently store it implicitly in the DB as the most recent
  // suggestion's reasoning. For now, show the count + any anomaly hints.
  const config = rows.filter(r => r.kind === 'config');
  const anomalies = rows.filter(r => r.kind === 'anomaly');

  if (badge) {
    if (rows.length) {
      badge.hidden = false;
      badge.textContent = String(rows.length);
    } else {
      badge.hidden = true;
    }
  }
  if (summaryEl) summaryEl.hidden = true;  // populated by review_now response

  if (!rows.length) {
    wrap.innerHTML = '<div class="hint">No pending suggestions. ' +
      'Click "Run review now" to ask Claude for a fresh analysis.</div>';
    return;
  }

  const safe = (s) => String(s || '').replace(/[<>&"]/g, (c) =>
    ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));

  const renderConfig = (s) => {
    const conf = s.confidence || 'medium';
    const confCls = `alg-conf alg-conf-${conf}`;
    return `
      <div class="alg-card" data-id="${s.id}">
        <div class="alg-card-head">
          <span class="alg-target">${safe(s.target)}</span>
          <span class="${confCls}">${conf}</span>
          <span class="alg-when">${new Date((s.created_at || 0) * 1000).toLocaleString()}</span>
        </div>
        <div class="alg-change-line">
          <span class="alg-num-old">${safe(s.current_value)}</span>
          <span class="alg-arrow">→</span>
          <span class="alg-num-new">${safe(s.proposed_value)}</span>
        </div>
        <div class="alg-reasoning">${safe(s.reasoning || '')}</div>
        <div class="alg-actions">
          <button class="btn btn-primary" data-alg-apply="${s.id}" type="button">Apply</button>
          <button class="btn btn-ghost" data-alg-dismiss="${s.id}" type="button">Dismiss</button>
        </div>
      </div>`;
  };
  const renderAnomaly = (s) => {
    const sev = s.severity || 'info';
    return `
      <div class="alg-card alg-anomaly alg-sev-${sev}" data-id="${s.id}">
        <div class="alg-card-head">
          <span class="alg-target">⚠ Anomaly</span>
          <span class="alg-sev-tag">${sev}</span>
          <span class="alg-when">${new Date((s.created_at || 0) * 1000).toLocaleString()}</span>
        </div>
        <div class="alg-reasoning">${safe(s.reasoning || '')}</div>
        <div class="alg-actions">
          <button class="btn btn-ghost" data-alg-dismiss="${s.id}" type="button">Acknowledge</button>
        </div>
      </div>`;
  };

  // Top action bar: Copy-all (always when there are rows) + Ack-all
  // (only when 2+ anomalies, to keep one-off anomalies from cluttering).
  const showAck = anomalies.length >= 2;
  const topBar = `
    <div class="alg-ack-all">
      <span>${rows.length} pending — ${config.length} config, ${anomalies.length} anomalies</span>
      <span style="display:flex; gap:8px">
        <button class="btn btn-ghost" data-alg-copy-all type="button">Copy all</button>
        ${showAck ? '<button class="btn btn-ghost" data-alg-ack-all type="button">Acknowledge all</button>' : ''}
      </span>
    </div>`;

  wrap.innerHTML = [
    topBar,
    ...config.map(renderConfig),
    ...anomalies.map(renderAnomaly),
  ].join('');

  wrap.querySelectorAll('[data-alg-apply]').forEach((btn) => {
    btn.addEventListener('click', () => applyAlgSuggestion(btn.dataset.algApply));
  });
  wrap.querySelectorAll('[data-alg-dismiss]').forEach((btn) => {
    btn.addEventListener('click', () => dismissAlgSuggestion(btn.dataset.algDismiss));
  });
  wrap.querySelector('[data-alg-ack-all]')?.addEventListener('click', () => {
    ackAllAnomalies(anomalies.map(a => a.id));
  });
  wrap.querySelector('[data-alg-copy-all]')?.addEventListener('click', () => {
    copyAllInsights(config, anomalies);
  });
}

async function copyAllInsights(config, anomalies) {
  const fmtDate = (ts) => new Date((ts || 0) * 1000).toISOString();
  const lines = [];
  lines.push(`# AI advisor insights — exported ${new Date().toISOString()}`);
  lines.push(`# ${config.length} config suggestion(s), ${anomalies.length} anomaly/anomalies`);
  lines.push('');
  if (config.length) {
    lines.push('## Config suggestions');
    for (const s of config) {
      lines.push(`- ${s.target}: ${s.current_value} → ${s.proposed_value} (confidence: ${s.confidence || 'medium'}, ${fmtDate(s.created_at)})`);
      if (s.reasoning) lines.push(`  reasoning: ${s.reasoning}`);
    }
    lines.push('');
  }
  if (anomalies.length) {
    lines.push('## Anomalies');
    for (const a of anomalies) {
      lines.push(`- [${(a.severity || 'info').toUpperCase()}] ${fmtDate(a.created_at)}`);
      if (a.reasoning) lines.push(`  ${a.reasoning}`);
    }
  }
  const text = lines.join('\n');
  const status = $('alg-status');
  try {
    await navigator.clipboard.writeText(text);
    if (status) {
      status.hidden = false;
      status.textContent = `Copied ${text.length} chars to clipboard.`;
      setTimeout(() => { status.hidden = true; }, 2500);
    }
  } catch (e) {
    // Clipboard API blocked (insecure context, permissions, etc.) — fall
    // back to a textarea-prompt the user can manually copy from.
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.cssText = 'position:fixed;top:10vh;left:5vw;width:90vw;height:60vh;z-index:9999;font-family:monospace;font-size:12px';
    document.body.appendChild(ta);
    ta.select();
    if (status) {
      status.hidden = false;
      status.textContent = 'Clipboard blocked — select-all + copy from the textarea, then click anywhere to dismiss.';
    }
    const dismiss = (ev) => {
      if (ev.target === ta) return;
      ta.remove();
      document.removeEventListener('click', dismiss, true);
      if (status) status.hidden = true;
    };
    setTimeout(() => document.addEventListener('click', dismiss, true), 50);
  }
}

async function ackAllAnomalies(ids) {
  if (!ids?.length) return;
  if (!confirm(`Acknowledge all ${ids.length} anomalies?`)) return;
  const status = $('alg-status');
  if (status) {
    status.hidden = false;
    status.textContent = `Acknowledging ${ids.length}…`;
  }
  try {
    await Promise.all(ids.map(id =>
      fetch(`/api/algorithm/suggestions/${id}/dismiss`, { method: 'POST' })
        .catch(e => console.warn('dismiss failed', id, e))
    ));
    if (status) {
      status.textContent = 'Acknowledged.';
      setTimeout(() => { status.hidden = true; }, 2000);
    }
    loadAlgorithmAdvisor();
  } catch (e) {
    if (status) status.textContent = `Failed: ${e.message || e}`;
  }
}

async function applyAlgSuggestion(id) {
  const status = $('alg-status');
  status.hidden = false;
  status.textContent = 'Applying…';
  try {
    const r = await fetch(`/api/algorithm/suggestions/${id}/apply`, { method: 'POST' });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
    status.textContent = 'Applied.';
    setTimeout(() => { status.hidden = true; }, 2500);
    loadAlgorithmAdvisor();
    // If the change touched smart_charge config, refresh that panel too.
    loadSmartCharge();
  } catch (e) {
    status.textContent = `Apply failed: ${e.message || e}`;
  }
}

async function dismissAlgSuggestion(id) {
  try {
    await fetch(`/api/algorithm/suggestions/${id}/dismiss`, { method: 'POST' });
    loadAlgorithmAdvisor();
  } catch (e) {
    console.warn('dismiss failed', e);
  }
}

// Track the current poll loop so a re-click or device switch can cancel
// it instead of stacking polls.
let _advisorPollTimer = null;

document.getElementById('alg-review-now')?.addEventListener('click', async () => {
  const status = $('alg-status');
  const summaryEl = $('alg-summary');
  const btn = $('alg-review-now');
  status.hidden = false;
  status.textContent = 'Starting Claude review (Opus + extended thinking, 60-180s)…';
  if (btn) btn.disabled = true;
  try {
    const dev = activeJackeryDevice();
    const params = dev?.device_sn
      ? `?device_sn=${encodeURIComponent(dev.device_sn)}` : '';
    // Fire-and-forget: server returns 202 immediately, we poll for the
    // result. Cloudflare 524 is what happens when we tried to wait for
    // the full review on a single HTTP request.
    const r = await fetch(`/api/algorithm/review_now${params}`, { method: 'POST' });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
    if (j.already_running) {
      status.textContent = 'A review is already running for this device — polling for results…';
    }
    pollAdvisorJob(dev?.device_sn);
  } catch (e) {
    status.textContent = `Review failed to start: ${e.message || e}`;
    if (btn) btn.disabled = false;
  }
});

function pollAdvisorJob(deviceSn) {
  // Clear any previous loop so re-clicks don't stack.
  if (_advisorPollTimer) { clearTimeout(_advisorPollTimer); _advisorPollTimer = null; }
  const params = deviceSn ? `?device_sn=${encodeURIComponent(deviceSn)}` : '';
  const status = $('alg-status');
  const summaryEl = $('alg-summary');
  const btn = $('alg-review-now');

  const tick = async () => {
    try {
      const r = await fetch(`/api/algorithm/review_status${params}`);
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
      if (j.status === 'running') {
        const elapsed = j.elapsed_s != null ? `${Math.round(j.elapsed_s)}s elapsed` : '';
        status.textContent = `Claude is reviewing… ${elapsed}`;
        _advisorPollTimer = setTimeout(tick, 4000);
        return;
      }
      if (j.status === 'done') {
        const result = j.result || {};
        if (summaryEl && result.summary) {
          summaryEl.textContent = result.summary;
          summaryEl.hidden = false;
        }
        const newCount = (result.new_suggestion_ids || []).length;
        status.textContent = `Review complete: ${newCount} new ` +
          `(${result.turns || 0} turns, ${result.tool_calls || 0} DB queries).`;
        setTimeout(() => { status.hidden = true; }, 6000);
        loadAlgorithmAdvisor();
        // Light the tab dot if user is on another tab — they'll see new
        // items waiting without needing to wait for the 3min periodic.
        refreshAutomationDot();
        if (btn) btn.disabled = false;
        return;
      }
      if (j.status === 'error') {
        status.textContent = `Review failed: ${j.error || 'unknown error'}`;
        if (btn) btn.disabled = false;
        return;
      }
      // status === 'idle' — shouldn't happen right after kicking, but
      // re-poll once just in case the task hasn't latched yet.
      _advisorPollTimer = setTimeout(tick, 1500);
    } catch (e) {
      status.textContent = `Status poll failed: ${e.message || e}`;
      if (btn) btn.disabled = false;
    }
  };
  // First poll quickly so the status text doesn't sit on "Starting…" for
  // multiple seconds; subsequent polls back off to 4s.
  _advisorPollTimer = setTimeout(tick, 800);
}

// Resume a poll if the user lands on Automation while a review is in
// flight (e.g. they kicked it, switched tabs, came back).
async function resumeAdvisorPollIfRunning() {
  const dev = activeJackeryDevice();
  if (!dev?.device_sn) return;
  try {
    const r = await fetch(`/api/algorithm/review_status?device_sn=${encodeURIComponent(dev.device_sn)}`);
    const j = await r.json().catch(() => ({}));
    if (r.ok && j.status === 'running') pollAdvisorJob(dev.device_sn);
  } catch { /* silent — not critical */ }
}

document.getElementById('alg-show-context')?.addEventListener('click', async () => {
  const wrap = $('alg-changes');
  if (!wrap) return;
  if (!wrap.hidden && wrap.dataset.mode === 'context') {
    wrap.hidden = true;
    return;
  }
  wrap.dataset.mode = 'context';
  wrap.innerHTML = '<div class="hint">Loading…</div>';
  wrap.hidden = false;
  try {
    const dev = activeJackeryDevice();
    const params = dev?.device_sn
      ? `?device_sn=${encodeURIComponent(dev.device_sn)}` : '';
    const r = await fetch(`/api/algorithm/preview${params}`);
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
    const safe = (s) => String(s || '').replace(/[<>&"]/g, (c) =>
      ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
    wrap.innerHTML = `
      <h3 style="margin:0 0 4px;font-size:14px">Context sent to Claude</h3>
      <p class="hint" style="margin:0 0 8px">
        Model: <code>${safe(j.model)}</code> · thinking budget: <code>${j.thinking_budget}</code> tokens.
        This is the exact text Claude reads as the user message — minus
        the system prompt and tool schema.
      </p>
      <pre class="dbg-pre" style="max-height:520px">${safe(j.rendered)}</pre>`;
  } catch (e) {
    wrap.innerHTML = `<div class="hint">Failed: ${e.message || e}</div>`;
  }
});

document.getElementById('alg-show-history')?.addEventListener('click', async () => {
  const wrap = $('alg-changes');
  if (!wrap) return;
  if (!wrap.hidden && wrap.dataset.mode === 'history') {
    wrap.hidden = true;
    return;
  }
  wrap.dataset.mode = 'history';
  try {
    const dev = activeJackeryDevice();
    const params = dev?.device_sn
      ? `?device_sn=${encodeURIComponent(dev.device_sn)}` : '';
    const r = await fetch(`/api/algorithm/changes${params}`);
    const j = await r.json();
    const rows = j.changes || [];
    if (!rows.length) {
      wrap.innerHTML = '<div class="hint">No applied changes yet.</div>';
    } else {
      const safe = (s) => String(s || '').replace(/[<>&"]/g, (c) =>
        ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
      wrap.innerHTML = '<h3 style="margin:0 0 8px;font-size:14px">Applied changes</h3>' +
        rows.map(c => `
          <div class="alg-change-row">
            <span class="alg-when">${new Date((c.applied_at || 0) * 1000).toLocaleString()}</span>
            <span class="alg-target">${safe(c.target)}</span>
            <span class="alg-num-old">${safe(c.old_value)}</span>
            <span class="alg-arrow">→</span>
            <span class="alg-num-new">${safe(c.new_value)}</span>
            <span class="alg-reasoning">${safe(c.reasoning || '')}</span>
          </div>`).join('');
    }
    wrap.hidden = false;
  } catch (e) {
    wrap.innerHTML = `<div class="hint">Failed: ${e.message || e}</div>`;
    wrap.hidden = false;
  }
});

// Reload AI insights on device switch (per-device suggestions).
function _reloadAdvisorOnDeviceSwitch() {
  if (activeTab === 'automation') loadAlgorithmAdvisor();
}

// ============================================================
// ANTHROPIC API KEY (Settings → enables Claude narration)
// ============================================================
// Status banner + save-with-server-validation form. We never receive
// the saved key back from the server; the status endpoint just tells
// us whether one is present, so the form stays empty after save.

let _anthropicHasKey = false;

async function loadAnthropicKeyStatus() {
  const line = $('ak-state-line');
  const status = $('ak-status');
  if (!line) return;
  try {
    const r = await fetch('/api/anthropic/key');
    const j = r.ok ? await r.json() : { has_key: false };
    _anthropicHasKey = !!j.has_key;
    if (j.has_key) {
      line.innerHTML = j.source === 'env'
        ? '✅ <strong>Configured via env var</strong> on the bridge — UI changes here will only affect the saved-on-disk key.'
        : '✅ <strong>API key saved.</strong> The Claude narration toggle on the Automation tab is unlocked.';
    } else {
      line.innerHTML = 'No key saved. Enter one below to unlock the Claude narration toggle on the Automation tab.';
    }
    if (status) status.hidden = true;
  } catch (e) {
    console.warn('anthropic key status fetch failed', e);
  }
  // Also re-evaluate the smart-charge claude toggle disabled state, in
  // case the user saved a key while sitting on Settings then jumped to
  // Automation without a full reload.
  applyClaudeToggleGate();
}

function applyAnthropicGates() {
  // Anything that depends on a saved + verified Anthropic API key gets
  // gated here. Re-runnable on every render or status change.
  const reason = 'Save an Anthropic API key on the Settings page first.';

  const t = $('sc-claude-toggle');
  if (t) {
    if (_anthropicHasKey) {
      t.disabled = false;
      t.title = '';
    } else {
      t.disabled = true;
      if (t.checked) t.checked = false;
      t.title = reason;
    }
  }

  // Advisor daily-review hour lives in the generic settings form, so it
  // only exists after renderSettingsFields() has run.
  const hour = $('set-advisor_trigger_hour');
  if (hour) {
    const row = hour.closest('.settings-row');
    if (_anthropicHasKey) {
      hour.disabled = false;
      hour.title = '';
      if (row) row.classList.remove('gated');
    } else {
      hour.disabled = true;
      hour.title = reason;
      if (row) row.classList.add('gated');
    }
  }
}

// Back-compat shim: older call sites can keep the original name.
const applyClaudeToggleGate = applyAnthropicGates;

document.getElementById('ak-form')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const status = $('ak-status');
  const input = $('ak-input');
  const key = input.value.trim();
  if (!key) return;
  status.hidden = false;
  status.textContent = 'Validating with Anthropic…';
  try {
    const r = await fetch('/api/anthropic/key', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ api_key: key }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || j.error || `HTTP ${r.status}`);
    status.textContent = 'Saved. Test call succeeded.';
    input.value = '';
    setTimeout(() => { status.hidden = true; }, 3000);
    loadAnthropicKeyStatus();
    // Newly-saved key → re-fetch the model list so the dropdown
    // switches from fallback to the live Anthropic list.
    loadAnthropicModelPickers();
  } catch (err) {
    status.textContent = String(err.message || err);
  }
});

document.getElementById('ak-clear')?.addEventListener('click', async () => {
  const status = $('ak-status');
  if (!confirm('Forget the saved Anthropic API key? Claude narration will stop until a new key is saved.')) return;
  status.hidden = false;
  status.textContent = 'Clearing…';
  try {
    const r = await fetch('/api/anthropic/key', { method: 'DELETE' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    status.textContent = 'Forgotten.';
    setTimeout(() => { status.hidden = true; }, 2500);
    loadAnthropicKeyStatus();
  } catch (err) {
    status.textContent = String(err.message || err);
  }
});

// ============================================================
// ANTHROPIC MODEL + EFFORT PICKERS — populated from
// /api/anthropic/models (live list when an API key is saved,
// static fallback otherwise), persisted via /api/anthropic/prefs.
// Refreshes every time the Settings tab opens.
//
// 1M-context handling: models flagged supports_1m by the server
// get a synthetic "(1M context)" entry with value `${id}@1m`. On
// save, the suffix is stripped and `advisor_1m_context: true` is
// sent alongside the bare model id. Picking a non-@1m entry sets
// it false. This way "future Opus" / "future Sonnet 1M" works
// without a code change — just bump JACKERY_1M_MODEL_PATTERNS on
// the server.
// ============================================================
const ANTHROPIC_1M_SUFFIX = '@1m';

async function loadAnthropicModelPickers() {
  const advisorSel = $('ak-advisor-model');
  const narratorSel = $('ak-narrator-model');
  const effortSel = $('ak-advisor-effort');
  const statusEl = $('ak-models-status');
  if (!advisorSel || !narratorSel || !effortSel) return;
  if (statusEl) statusEl.textContent = 'Loading model list…';
  try {
    // Parallel: fetch the available list + the user's current selection.
    const [modelsR, prefsR] = await Promise.all([
      fetch('/api/anthropic/models'),
      fetch('/api/anthropic/prefs'),
    ]);
    const modelsJ = modelsR.ok ? await modelsR.json() : { models: [], source: 'fetch_failed' };
    const prefsJ = prefsR.ok ? await prefsR.json() : {};
    const models = modelsJ.models || [];
    const prefs = {
      advisor_model: prefsJ.advisor_model || '',
      advisor_1m_context: !!prefsJ.advisor_1m_context,
      advisor_thinking_effort: prefsJ.advisor_thinking_effort || 'high',
      narrator_model: prefsJ.narrator_model || '',
    };
    // Compute the dropdown value that represents the current advisor
    // selection (model + 1m flag) — if 1m is on, pick the @1m
    // variant; else pick the bare model.
    const advisorCurrent = prefs.advisor_model
      ? (prefs.advisor_1m_context
          ? `${prefs.advisor_model}${ANTHROPIC_1M_SUFFIX}`
          : prefs.advisor_model)
      : '';

    const populate = (sel, current, opts = {}) => {
      const { synthesize1m = false } = opts;
      sel.innerHTML = '';
      const seen = new Set();
      const addOpt = (id, label) => {
        if (seen.has(id)) return;
        seen.add(id);
        const o = document.createElement('option');
        o.value = id;
        o.textContent = label || id;
        if (id === current) o.selected = true;
        sel.appendChild(o);
      };
      for (const m of models) {
        addOpt(m.id, m.display_name);
        // Synthesize a 1M variant on dropdowns that should offer it
        // (advisor only). The server tells us which models support 1M
        // via the `supports_1m` flag — we don't hardcode "opus" here
        // so future families pick up the variant via the env-var
        // pattern config without a frontend change.
        if (synthesize1m && m.supports_1m) {
          const id1m = `${m.id}${ANTHROPIC_1M_SUFFIX}`;
          addOpt(id1m, `${m.display_name || m.id} (1M context)`);
        }
      }
      if (current && !seen.has(current)) {
        addOpt(current, `${current} (saved)`);
      }
      sel.disabled = false;
    };
    populate(advisorSel, advisorCurrent, { synthesize1m: true });
    populate(narratorSel, prefs.narrator_model);
    // Effort dropdown is static (Anthropic API enum); just select
    // the persisted value.
    for (const opt of effortSel.options) {
      opt.selected = opt.value === prefs.advisor_thinking_effort;
    }
    if (statusEl) {
      const sourceLabel = ({
        live: '✅ Live list from Anthropic.',
        cache: '✅ Live list (cached).',
        fallback_no_key: '⚠ No API key saved — showing well-known model aliases as a fallback.',
        fallback_no_sdk: '⚠ Anthropic SDK not in this image — showing fallback list.',
        fallback_fetch_failed: `⚠ Couldn't reach Anthropic to refresh the list — showing fallback. ${modelsJ.error || ''}`,
        fallback_empty: '⚠ Anthropic returned an empty list — showing fallback.',
      })[modelsJ.source] || `Source: ${modelsJ.source || 'unknown'}.`;
      statusEl.textContent = sourceLabel;
    }
  } catch (e) {
    if (statusEl) statusEl.textContent = `Failed to load model list: ${e.message || e}`;
  }
}

async function saveAnthropicPref(body, label) {
  const statusEl = $('ak-models-status');
  if (statusEl) statusEl.textContent = `Saving ${label}…`;
  try {
    const r = await fetch('/api/anthropic/prefs', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      throw new Error(j.detail || `HTTP ${r.status}`);
    }
    if (statusEl) {
      statusEl.textContent = `Saved. ${label} applies on the next advisor/narrator call.`;
      setTimeout(() => { statusEl.textContent = ''; }, 3500);
    }
  } catch (e) {
    if (statusEl) statusEl.textContent = `Save failed: ${e.message || e}`;
  }
}

document.getElementById('ak-advisor-model')?.addEventListener('change', (e) => {
  // Parse the synthetic @1m suffix into a (model, 1m_context) tuple
  // before persisting. Picking the bare model entry explicitly turns
  // 1m off — important so a previous "@1m" save doesn't keep the
  // beta header on after the user switches to a non-1M model.
  const raw = e.target.value;
  const has1m = raw.endsWith(ANTHROPIC_1M_SUFFIX);
  const model = has1m ? raw.slice(0, -ANTHROPIC_1M_SUFFIX.length) : raw;
  saveAnthropicPref(
    { advisor_model: model, advisor_1m_context: has1m },
    `advisor model${has1m ? ' (1M)' : ''}`,
  );
});
document.getElementById('ak-narrator-model')?.addEventListener('change', (e) => {
  saveAnthropicPref({ narrator_model: e.target.value }, 'narrator model');
});
document.getElementById('ak-advisor-effort')?.addEventListener('change', (e) => {
  saveAnthropicPref({ advisor_thinking_effort: e.target.value }, 'thinking effort');
});

// ============================================================
// HERO CARD REORDER (drag-and-drop, persisted per-browser)
// ============================================================
const HERO_ORDER_KEY = 'jackery-hero-order';

function applyHeroOrder() {
  const grid = $('hero-grid');
  if (!grid) return;
  let saved;
  try { saved = JSON.parse(localStorage.getItem(HERO_ORDER_KEY) || '[]'); }
  catch { return; }
  if (!Array.isArray(saved) || !saved.length) return;
  // Reorder DOM children to match saved order. Unknown card IDs are
  // skipped; new cards we haven't seen before fall to the end.
  for (const id of saved) {
    const el = grid.querySelector(`[data-card="${id}"]`);
    if (el) grid.appendChild(el);
  }
}

function initHeroSortable() {
  const grid = $('hero-grid');
  if (!grid || typeof Sortable === 'undefined') return;
  Sortable.create(grid, {
    handle: '.grab-handle',  // drag only via the handle so card content stays interactive
    animation: 180,
    ghostClass: 'sortable-ghost',
    dragClass: 'sortable-drag',
    delay: 100,              // long-press delay on touch
    delayOnTouchOnly: true,  // mouse drags fire instantly; touch needs a brief hold
    touchStartThreshold: 5,
    onEnd: () => {
      const ids = [...grid.querySelectorAll('[data-card]')].map(c => c.dataset.card);
      try { localStorage.setItem(HERO_ORDER_KEY, JSON.stringify(ids)); } catch {}
    },
  });
}

(async function boot() {
  // Restore card order BEFORE first paint so the user doesn't see a flash
  // of the default arrangement.
  applyHeroOrder();

  // Re-acquire wake lock on boot if the user previously enabled it.
  // Silent failure on unsupported browsers — toggle UI surfaces the
  // not-supported message when the user opens Settings.
  if (isKeepAwakeOn()) {
    requestWakeLock();
  }

  const ok = await checkAuth();
  // Always start the WS — server returns whatever it has, even if cloud is logging in
  connectWs();
  initHeroSortable();

  // Pre-load history so the Live chart and Energy tab have data immediately
  // (otherwise the user has to switch tabs once before history populates).
  fetchEnergyHistory();
  fetchEnergyAllDevices();

  // Once-per-app-load geolocation prompt for the forecast feature. Skipped
  // if location is already saved or the user previously denied.
  if (ok) maybePromptLocationOnBoot();

  // EOD forecast badge on the battery card: populate once on boot, then
  // refit hourly (weather forecast itself only updates ~hourly).
  if (ok) fetchEodForecast();
  setInterval(fetchEodForecast, 30 * 60_000);

  // Per-expansion-battery list. Server caches packs (refreshed every 5min
  // by the poll loop), so a 30s UI refresh just keeps the rendered values
  // in sync with the cache without hammering the cloud.
  if (ok) fetchBatteryPacks();
  setInterval(fetchBatteryPacks, 30_000);

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

  // Forecast: weather updates hourly, fit + simulation are cheap, refresh
  // every 5 min while the tab is visible. fetchForecast is a no-op if the
  // result hasn't changed visibly.
  setInterval(() => { if (activeTab === 'forecast') fetchForecast(); }, 5 * 60_000);

  // AI advisor pending-items badge on the Automation tab. Check on load
  // + every 3 min so the daily 8am review (or a manual run that
  // completed in another browser) lights up the dot promptly.
  refreshAutomationDot();
  setInterval(refreshAutomationDot, 3 * 60_000);

  // If auth was missing the user is in the modal — when they sign in successfully
  // the modal hides and the WS snapshot will populate the UI.
  setInterval(checkAuth, 30000);
})();
