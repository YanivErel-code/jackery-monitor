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
  if (name === 'settings') { loadSettings(); }
  if (name === 'logs')     { loadLogs(); }
  if (name === 'automation') { loadAutomation(); }
}

// ============================================================
// AUTOMATION TAB
// ============================================================
let _savedKasaDevices = [];   // last loaded list of saved devices

async function loadAutomation() {
  // Load both saved devices and rules in parallel; both rerender on completion.
  await Promise.all([loadSavedKasa(), loadRules()]);
}

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
    return `<div class="kasa-row" data-host="${safe(d.host)}">
      <span class="kr-state ${stateClass}">${stateText}</span>
      <div class="kr-name">
        <div class="kr-alias">${safe(d.alias)}</div>
        <div class="kr-meta">${meta}</div>
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

async function loadRules() {
  const list = $('auto-rules');
  if (!list) return;
  list.innerHTML = '<div class="auto-empty">Loading rules…</div>';
  try {
    const r = await fetch('/api/automation/rules');
    const j = await r.json();
    renderAutomationRules(j.rules || []);
  } catch (e) {
    list.innerHTML = `<div class="auto-empty">Failed to load: ${e.message || e}</div>`;
  }
}

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
    return `<div class="auto-rule ${r.enabled ? '' : 'disabled'}" data-id="${r.id}">
      <div>
        <div class="ar-title">${safe(r.name)}</div>
        <div class="ar-cond">
          when battery <span class="op">${opLabel[r.operator] || r.operator}</span>
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
  // Repopulate the device dropdown from the saved-devices list each time
  // so newly added devices appear without a tab switch.
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
  ed.hidden = false;
  $('auto-editor-title').textContent = rule ? 'Edit rule' : 'New rule';
  $('auto-id').value       = rule?.id || '';
  $('auto-name').value     = rule?.name || '';
  $('auto-operator').value = rule?.operator || '<';
  $('auto-value').value    = rule?.value ?? 20;
  $('auto-kasa-pick').value = rule?.kasa_host || '';
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
  if (!host) {
    alert('Pick a saved Kasa device first.');
    return;
  }
  // Look up the alias from the saved-devices cache so the rule list shows
  // the friendly name even if the device record changes later.
  const dev = _savedKasaDevices.find((d) => d.host === host);
  const body = {
    id: $('auto-id').value || undefined,
    name: $('auto-name').value.trim(),
    operator: $('auto-operator').value,
    value: Number($('auto-value').value),
    action,
    kasa_host: host,
    kasa_alias: dev?.alias || host,
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
    animateNumber($('battery-pct'), t.battery_percent);
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
  if (t.battery_temp_c != null) $('battery-temp').textContent = `${fmt(t.battery_temp_c, 0)} °C`;

  animateNumber($('output-w'), t.output_power_w);
  animateNumber($('input-w'),  t.input_power_w);
  animateNumber($('input-grid-w'),  t.ac_input_w ?? 0);
  animateNumber($('input-solar-w'), t.solar_input_w ?? 0);

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
  if (t.ac_output_v != null) $('ac-out-v').textContent  = fmt(t.ac_output_v, 0);
  if (t.ac_output_hz != null)        $('ac-out-hz').textContent = fmt(t.ac_output_hz, 0);

  // Today KPIs from energy aggregator
  if (s.energy?.today) {
    $('today-out-kwh').textContent = fmtKwh(s.energy.today.output_wh);
    $('today-in-kwh').textContent  = fmtKwh(s.energy.today.input_wh);
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

  // Areas (output, input) under their lines — gradient transparent toward bottom.
  drawAreaFill(ctx, inp, xs, yWatts, baseY, '#38bdf8', .22);
  drawAreaFill(ctx, out, xs, yWatts, baseY, '#4ade80', .25);
  // Lines on top
  drawSmoothLine(ctx, inp, xs, yWatts, '#38bdf8', 2.5);
  drawSmoothLine(ctx, out, xs, yWatts, '#4ade80', 2.5);
  // Battery on its own (right) axis — dashed so it's clearly a different scale.
  ctx.save();
  ctx.setLineDash([4, 4]);
  drawSmoothLine(ctx, bat, xs, yPct, '#fbbf24', 2);
  ctx.restore();

  // Hover state stored on the canvas element
  _attachChartHover(canvas, hist, (i, evt) => {
    const p = hist[i];
    const ts = p.ts ? new Date(p.ts * 1000).toLocaleTimeString() : '';
    return `<div class="cht-ts">${ts}</div>
            <div class="cht-row"><i style="background:#4ade80"></i> Output <b>${fmt(p.output_power_w)}</b> W</div>
            <div class="cht-row"><i style="background:#38bdf8"></i> Input <b>${fmt(p.input_power_w)}</b> W</div>
            <div class="cht-row"><i style="background:#fbbf24"></i> Battery <b>${fmt(p.battery_percent)}</b> %</div>`;
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
