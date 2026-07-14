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

// Canonical HTML escape. Use this whenever interpolating untrusted data
// (API responses, error messages, cloud-probed values) into innerHTML.
// Several sections also define a local `safe = ...` shim — those are
// kept for now to avoid a 100+ site rename, but they behave identically.
function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

// ---------- state ----------
let lastStatus = null;
let lastDevices = [];
let energyRangeHours = 6;     // current Energy tab range selection
let energyHistoryCache = null; // last fetched series for the energy tab
let energyDailyCache = null;   // last /api/energy/daily payload (365d, per device)
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
    // If the AC button is in watchdog-error state, route the click to
    // the dismiss endpoint instead of toggling. The watchdog will start
    // a fresh retry sequence on the next AC=OFF observation.
    if (port === 'ac' && btn.classList.contains('wd-err')) {
      const viewSnDismiss = activeJackeryDevice()?.device_sn || null;
      try {
        const r = await fetch('/api/inverter_watchdog/dismiss' +
          (viewSnDismiss ? `?device_sn=${encodeURIComponent(viewSnDismiss)}` : ''),
          { method: 'POST' });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        btn.classList.remove('wd-err');
        btn.title = '';
      } catch (e) {
        alert(`Failed to dismiss watchdog error: ${e.message || e}`);
      }
      return;
    }
    const lbl = $(`sw-${port}`);
    const currentState = (lbl?.textContent || '').trim();
    if (currentState !== 'ON' && currentState !== 'OFF') {
      return;  // unknown state — don't risk a blind toggle
    }
    // Route the toggle to the device the user is *viewing*, not the one
    // the bridge happens to be polling. Without this, a user with
    // per-browser view set to a non-active device would silently toggle
    // the wrong Jackery.
    const viewSn = activeJackeryDevice()?.device_sn || null;
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
        body: JSON.stringify({ port, on: turnOn, device_sn: viewSn }),
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
// URL hash is the single source of truth for the active tab — refresh
// keeps state automatically (hash persists in the URL), shared links
// land on the right tab, and explicit nav to `/` always goes to Live.
// We deliberately do NOT layer localStorage on top: that fights the
// URL on explicit navigations (clearing the hash from the address bar
// would be over-ridden by the saved value, snapping back).
const VALID_TABS = new Set([
  'live', 'energy', 'forecast', 'device', 'automation', 'logs', 'settings',
]);

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => switchTab(tab.dataset.tab));
});

function switchTab(name, opts = {}) {
  if (!VALID_TABS.has(name)) return;
  activeTab = name;
  // Reflect the tab in the URL hash. Live is the default — keep the
  // URL clean (no hash) on that one. fromHash skips the rewrite when
  // we're already responding to a hashchange we didn't trigger.
  if (!opts.fromHash) {
    const desired = name === 'live' ? '' : '#' + name;
    if (location.hash !== desired) {
      try {
        if (desired) {
          history.pushState(null, '', desired);
        } else {
          // Strip hash without reload — fresh start at the bare URL.
          history.pushState(null, '', location.pathname + location.search);
        }
      } catch (_) { /* old browser or restricted context */ }
    }
  }
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('on', t.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach(p => p.toggleAttribute('hidden', p.id !== `tab-${name}`));
  if (name === 'live')     { drawLiveChart(lastStatus); }
  if (name === 'energy')   { fetchEnergyHistory(); fetchEnergyAllDevices(); fetchEnergyDaily(); }
  if (name === 'forecast') { fetchForecast(); }
  if (name === 'settings') { loadSettings(); loadCostPlan(); initKeepAwakeToggle(); loadAnthropicKeyStatus(); loadAnthropicModelPickers(); loadAIProvider(); loadOpenAIKeyStatus(); loadOpenAIModelPickers(); loadBackupAll(); restoreSettingsSubtab(); }
  if (name === 'logs')     { loadLogs(); }
  if (name === 'automation') {
    restoreAutomationSubtab();
    loadAutomation(); loadSmartCharge(); loadSolarCharge(); loadAlgorithmAdvisor(); resumeAdvisorPollIfRunning();
    // User is now looking — clear the "new insights" dot.
    setAutomationDot(false);
  }
  if (name === 'device')   { loadDeviceCapacity(); loadDeviceParams(); }
}

// Boot path: pull the tab from the URL hash. Defer the actual switch
// until the next microtask so all the `let`/`const` declarations later
// in this file have been initialized — calling switchTab synchronously
// here would hit TDZ errors on side-effect functions that reference
// module-level vars (e.g. loadAnthropicKeyStatus -> _anthropicHasKey).
function _initialTab() {
  const hashTab = location.hash.slice(1);
  return (hashTab && VALID_TABS.has(hashTab)) ? hashTab : 'live';
}
queueMicrotask(() => {
  const tab = _initialTab();
  if (tab !== 'live') switchTab(tab);
});

// Back/forward buttons cycle through tab history; manual hash edits in
// the address bar route to the corresponding tab. The hashchange event
// fires for our own pushState too, but the activeTab guard short-
// circuits the re-entry.
window.addEventListener('hashchange', () => {
  const target = location.hash.slice(1) || 'live';
  if (VALID_TABS.has(target) && target !== activeTab) {
    switchTab(target, { fromHash: true });
  }
});

// ---- Settings sub-tabs ---------------------------------------------------
// The Settings tab grew enough to need internal grouping (general /
// intelligence / rates / backup). Each card is wrapped in a
// <div data-settings-group="..."> in index.html; this toggles which
// group is visible. Selection persists per-browser via localStorage so
// a refresh / reload lands you back on the same view.
const SETTINGS_SUBTAB_KEY = 'jackery-settings-subtab';
const SETTINGS_SUBTABS = ['general', 'intelligence', 'rates', 'backup'];

function selectSettingsSubtab(name) {
  if (!SETTINGS_SUBTABS.includes(name)) name = 'general';
  document.querySelectorAll('.settings-subtab').forEach(btn => {
    btn.classList.toggle('on', btn.dataset.settingsTab === name);
    btn.setAttribute('aria-selected', btn.dataset.settingsTab === name ? 'true' : 'false');
  });
  document.querySelectorAll('[data-settings-group]').forEach(group => {
    group.toggleAttribute('hidden', group.dataset.settingsGroup !== name);
  });
  try { localStorage.setItem(SETTINGS_SUBTAB_KEY, name); } catch (_) {}
}

function restoreSettingsSubtab() {
  let saved = null;
  try { saved = localStorage.getItem(SETTINGS_SUBTAB_KEY); } catch (_) {}
  selectSettingsSubtab(saved || 'general');
  // Refresh status chips that don't auto-update via existing loaders —
  // most chips (#ak-status, #backup-creds-status, #backup-last-line,
  // #cost-status) get populated by their own loaders that already run
  // when Settings opens. Display is the odd one out — it's pure
  // localStorage.
  _refreshSettingsDisplayChip();
}

document.querySelectorAll('.settings-subtab').forEach((btn) => {
  btn.addEventListener('click', () => selectSettingsSubtab(btn.dataset.settingsTab));
});

// ---- Collapsed history list -----------------------------------------------
// Renders `rows` into `listEl` showing only the first row by default with
// a "Show N more" toggle that reveals/hides the rest. The toggle is
// delegated below so it works for both Smart-charge and Excess-diversion
// history without per-render rebinding.
function renderCollapsedHistory(listEl, rows, rowRenderer) {
  if (!listEl) return;
  if (!rows.length) {
    listEl.innerHTML = '';
    return;
  }
  const remaining = rows.length - 1;
  if (remaining === 0) {
    listEl.innerHTML = rowRenderer(rows[0]);
    return;
  }
  listEl.innerHTML = `
    ${rowRenderer(rows[0])}
    <button class="history-toggle" type="button" data-count="${remaining}">
      Show ${remaining} more ▼
    </button>
    <div class="history-rest" hidden>
      ${rows.slice(1).map(rowRenderer).join('')}
    </div>
  `;
}

document.body.addEventListener('click', (e) => {
  const btn = e.target.closest('.history-toggle');
  if (!btn) return;
  const rest = btn.parentElement?.querySelector('.history-rest');
  if (!rest) return;
  if (rest.hidden) {
    rest.hidden = false;
    btn.textContent = 'Hide ▲';
  } else {
    rest.hidden = true;
    btn.textContent = `Show ${btn.dataset.count} more ▼`;
  }
});

// ---- Automation sub-tabs: same pattern as Settings ------------------------
// Four groups (controllers / devices / rules / insights). Each card under
// #tab-automation carries data-automation-group; this handler hides the
// non-matching ones and tracks the choice in localStorage so a reload
// or tab switch lands the user back where they left off.
const AUTOMATION_SUBTAB_KEY = 'jackery-automation-subtab';
const AUTOMATION_SUBTABS = ['controllers', 'devices', 'rules', 'insights'];

function selectAutomationSubtab(name) {
  if (!AUTOMATION_SUBTABS.includes(name)) name = 'controllers';
  const pane = document.getElementById('tab-automation');
  if (pane) pane.dataset.activeGroup = name;
  document.querySelectorAll('.automation-subtab').forEach((btn) => {
    btn.classList.toggle('on', btn.dataset.automationTab === name);
    btn.setAttribute('aria-selected', btn.dataset.automationTab === name ? 'true' : 'false');
  });
  document.querySelectorAll('#tab-automation [data-automation-group]').forEach((el) => {
    el.toggleAttribute('hidden', el.dataset.automationGroup !== name);
  });
  try { localStorage.setItem(AUTOMATION_SUBTAB_KEY, name); } catch (_) {}
}

function restoreAutomationSubtab() {
  let saved = null;
  try { saved = localStorage.getItem(AUTOMATION_SUBTAB_KEY); } catch (_) {}
  selectAutomationSubtab(saved || 'controllers');
}

document.querySelectorAll('.automation-subtab').forEach((btn) => {
  btn.addEventListener('click', () => selectAutomationSubtab(btn.dataset.automationTab));
});

// ---- Settings cards: collapsible with persisted state ---------------------
// Each card under [data-settings-group] carries a stable
// data-settings-collapse-key. Click the header to toggle, and we
// remember the state per-key in localStorage so a refresh doesn't
// re-collapse what the user just expanded. The status chip in each
// header (e.g. backup-creds-status, backup-last-line, ak-status,
// cost-status) stays visible when collapsed — that's the whole
// point: zero-click reads of "did last night's backup succeed?",
// "is Anthropic connected?", "what TOU plan am I on?".
const SETTINGS_COLLAPSE_KEY = (cardKey) => `jackery-settings-collapse:${cardKey}`;

function _persistCollapseState(cardKey, collapsed) {
  try { localStorage.setItem(SETTINGS_COLLAPSE_KEY(cardKey), collapsed ? '1' : '0'); }
  catch (_) {}
}

function _restoreCollapseStates() {
  document.querySelectorAll('[data-settings-collapse-key]').forEach((card) => {
    const key = card.dataset.settingsCollapseKey;
    let saved = null;
    try { saved = localStorage.getItem(SETTINGS_COLLAPSE_KEY(key)); }
    catch (_) {}
    if (saved === '1') card.classList.add('collapsed');
    else if (saved === '0') card.classList.remove('collapsed');
    // else: leave whatever the HTML set (default state per card).
  });
}
_restoreCollapseStates();

document.querySelectorAll('[data-settings-collapse-key] .collapsible-header')
  .forEach((header) => {
    header.addEventListener('click', (e) => {
      // Don't hijack clicks on form controls inside the header (none today,
      // but defensive — keeps space + tab keypresses from doing weird stuff).
      if (e.target.closest('input, select, button, a, textarea')) return;
      const card = header.closest('[data-settings-collapse-key]');
      if (!card) return;
      const nowCollapsed = !card.classList.contains('collapsed');
      card.classList.toggle('collapsed', nowCollapsed);
      _persistCollapseState(card.dataset.settingsCollapseKey, nowCollapsed);
    });
  });

// ---- Settings global search ---------------------------------------------
// Flat index built lazily from the DOM the first time the user types.
// Every .settings-row inside every collapsible card becomes a search
// target — labels + hints are already on the page in human-readable
// form, no parallel schema needed. Card titles themselves are also
// indexed so "backup" jumps to that group's first card.
let _settingsSearchIndex = null;
const _SETTINGS_GROUP_LABELS = {
  general: 'General',
  intelligence: 'Intelligence',
  rates: 'Rates',
  backup: 'Backup',
};

function _buildSettingsSearchIndex() {
  const idx = [];
  document.querySelectorAll('[data-settings-group]').forEach((group) => {
    const groupKey = group.dataset.settingsGroup;
    group.querySelectorAll('[data-settings-collapse-key]').forEach((card) => {
      const cardKey = card.dataset.settingsCollapseKey;
      const cardTitle = card.querySelector('h2')?.textContent.trim() || cardKey;
      // Card itself: lets a query like "anthropic" or "backup" match
      // even if the user doesn't remember a specific row label.
      idx.push({
        group: groupKey, cardKey, cardTitle,
        label: cardTitle, hint: '',
        el: card,
      });
      // Each labelled row.
      card.querySelectorAll('.settings-row').forEach((row) => {
        const labelEl = row.querySelector('.settings-label-text');
        const hintEl = row.querySelector('.settings-hint, .hint');
        const label = (labelEl?.textContent || '').trim();
        if (!label) return;  // skip rows with no label
        idx.push({
          group: groupKey, cardKey, cardTitle, label,
          hint: (hintEl?.textContent || '').trim(),
          el: row,
        });
      });
    });
  });
  return idx;
}

function _searchSettingsIndex(query) {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  if (!_settingsSearchIndex) _settingsSearchIndex = _buildSettingsSearchIndex();
  const out = [];
  for (const entry of _settingsSearchIndex) {
    const labelHit = entry.label.toLowerCase().includes(q);
    const hintHit = entry.hint.toLowerCase().includes(q);
    if (!labelHit && !hintHit) continue;
    // Score: label hits beat hint-only hits; earlier matches beat later.
    const labelPos = labelHit ? entry.label.toLowerCase().indexOf(q) : 999;
    out.push({ ...entry, _score: (labelHit ? 0 : 1000) + labelPos });
    if (out.length >= 64) break;
  }
  out.sort((a, b) => a._score - b._score);
  return out.slice(0, 8);
}

function _highlightQuery(text, query) {
  const escaped = escapeHtml(text);
  if (!query) return escaped;
  const re = new RegExp(`(${query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'gi');
  return escaped.replace(re, '<mark class="settings-search-hl">$1</mark>');
}

function _renderSettingsSearchResults(results, query) {
  const ul = $('settings-search-results');
  if (!ul) return;
  if (!results.length) {
    ul.innerHTML = `<li class="settings-search-empty">No matches for “${escapeHtml(query)}”</li>`;
    ul.hidden = false;
    return;
  }
  ul.innerHTML = results.map((r, i) => `
    <li class="settings-search-result${i === 0 ? ' on' : ''}" data-idx="${i}" role="option">
      <div class="settings-search-crumb">${escapeHtml(_SETTINGS_GROUP_LABELS[r.group] || r.group)} → ${escapeHtml(r.cardTitle)}</div>
      <div class="settings-search-label">${_highlightQuery(r.label, query)}</div>
      ${r.hint ? `<div class="settings-search-hint">${_highlightQuery(r.hint, query)}</div>` : ''}
    </li>
  `).join('');
  ul.hidden = false;
  ul.querySelectorAll('.settings-search-result').forEach((li) => {
    li.addEventListener('click', () => {
      const idx = Number(li.dataset.idx);
      if (results[idx]) _settingsSearchJumpTo(results[idx]);
    });
  });
}

function _settingsSearchJumpTo(entry) {
  // Switch to the matching sub-tab.
  selectSettingsSubtab(entry.group);
  // Expand the card if currently collapsed.
  const card = document.querySelector(
    `[data-settings-collapse-key="${entry.cardKey}"]`);
  if (card && card.classList.contains('collapsed')) {
    card.classList.remove('collapsed');
    _persistCollapseState(entry.cardKey, false);
  }
  // Scroll the matching element into view + pulse-highlight.
  if (entry.el) {
    entry.el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    entry.el.classList.remove('settings-search-jumped');
    // eslint-disable-next-line no-unused-expressions
    void entry.el.offsetWidth;  // force reflow so the animation restarts
    entry.el.classList.add('settings-search-jumped');
  }
  // Reset the search UI.
  const ul = $('settings-search-results');
  if (ul) ul.hidden = true;
  const input = $('settings-search');
  if (input) input.value = '';
}

(function _wireSettingsSearch() {
  const input = $('settings-search');
  if (!input) return;
  let timer = null;
  let lastResults = [];
  input.addEventListener('input', () => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      const q = input.value;
      if (!q.trim()) {
        $('settings-search-results').hidden = true;
        lastResults = [];
        return;
      }
      lastResults = _searchSettingsIndex(q);
      _renderSettingsSearchResults(lastResults, q);
    }, 100);
  });
  input.addEventListener('keydown', (e) => {
    const ul = $('settings-search-results');
    if (!ul) return;
    if (e.key === 'Escape') {
      input.value = '';
      ul.hidden = true;
      input.blur();
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const onEl = ul.querySelector('.settings-search-result.on');
      if (onEl) onEl.click();
    } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      const items = [...ul.querySelectorAll('.settings-search-result')];
      if (!items.length) return;
      e.preventDefault();
      const cur = items.findIndex(li => li.classList.contains('on'));
      let next = e.key === 'ArrowDown' ? cur + 1 : cur - 1;
      if (next < 0) next = items.length - 1;
      if (next >= items.length) next = 0;
      items.forEach((li, i) => li.classList.toggle('on', i === next));
      items[next].scrollIntoView({ block: 'nearest' });
    }
  });
  // Click outside the search wrap closes the dropdown.
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.settings-search-wrap')) {
      const ul = $('settings-search-results');
      if (ul) ul.hidden = true;
    }
  });
  // Re-build the index on next search if the DOM changed (e.g. cost
  // plan rendered new TOU rows). Cheap to invalidate; ~30 entries.
  document.addEventListener('jackery-settings-dom-changed', () => {
    _settingsSearchIndex = null;
  });
})();

// "/" focuses the search input when Settings is the active tab.
// Skip when the user is already typing somewhere else, otherwise it'd
// hijack literal "/" keystrokes in number inputs / Anthropic API key
// fields / etc.
document.addEventListener('keydown', (e) => {
  if (e.key !== '/' || activeTab !== 'settings') return;
  const ae = document.activeElement;
  if (ae && ae.matches('input, select, textarea, button')) return;
  e.preventDefault();
  $('settings-search')?.focus();
});

// Populate the Display card's collapsed-header chip so the user can see
// the current temp unit + keep-awake state at a glance without expanding.
function _refreshSettingsDisplayChip() {
  const el = $('settings-display-status');
  if (!el) return;
  let unit = 'C';
  try { unit = (typeof getTempUnit === 'function') ? getTempUnit() : 'C'; }
  catch (_) {}
  let keepAwake = false;
  try { keepAwake = localStorage.getItem(KEEP_AWAKE_KEY) === '1'; }
  catch (_) {}
  const parts = [`°${unit}`];
  if (keepAwake) parts.push('keep awake');
  el.textContent = parts.join(' · ');
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
  // Also reset the title so it doesn't keep a stale "1 device offline"
  // hover hint from before the user switched in.
  if (activeTab === 'automation') {
    setAutomationDot(false);
    const dot = $('tab-automation-dot');
    if (dot) dot.title = 'Automation';
    return;
  }
  try {
    const dev = activeJackeryDevice();
    const params = dev?.device_sn
      ? `?device_sn=${encodeURIComponent(dev.device_sn)}&status=pending`
      : '?status=pending';
    const [insightsR, kasaHealthR] = await Promise.all([
      fetch(`/api/algorithm/suggestions${params}`),
      fetch('/api/kasa/health'),
    ]);
    let actionableInsights = 0;
    let kasaOffline = 0;
    if (insightsR.ok) {
      const j = await insightsR.json();
      // Only count items the user can actually act on. INFO-severity
      // anomalies are observations the advisor explicitly flags as
      // "no action needed" — they belong in the list when the user
      // opens the tab, but they shouldn't trigger the dot. Counting
      // them was burying the actionable items behind a stack of pure
      // observations and made the dot meaningless.
      actionableInsights = (j.suggestions || []).filter((s) =>
        s.kind === 'config'
        || (s.kind === 'anomaly' && s.severity === 'warn')
      ).length;
    }
    if (kasaHealthR.ok) {
      const k = await kasaHealthR.json();
      kasaOffline = k.offline_count || 0;
    }
    // Re-check activeTab after the network round-trip — boot races
    // (queueMicrotask switchTab fires AFTER the IIFE that kicked off
    // the first refresh) and mid-flight tab switches would otherwise
    // light the dot back up immediately after switchTab cleared it,
    // and it'd take a full 3-min poll cycle to clear.
    if (activeTab === 'automation') {
      setAutomationDot(false);
      const dot = $('tab-automation-dot');
      if (dot) dot.title = 'Automation';
      return;
    }
    const dot = $('tab-automation-dot');
    setAutomationDot(actionableInsights > 0 || kasaOffline > 0);
    if (dot) {
      // Dynamic title so hovering reveals what actually needs attention.
      const reasons = [];
      if (actionableInsights > 0) {
        reasons.push(`${actionableInsights} AI insight(s) need attention`);
      }
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
    list.innerHTML = `<div class="hint">Failed to load: ${escapeHtml(e.message || e)}</div>`;
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
    panel.innerHTML = `<div class="hint" style="padding:8px 12px;color:#ef4444">Failed: ${escapeHtml(e.message || e)}</div>`;
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
    // refresh=true on the user-initiated load forces a fresh probe so
    // the first paint reflects current reality. Subsequent polls (see
    // _kasaPollTimer below) just read the reconciler-maintained cache.
    const r = await fetch('/api/kasa/saved?refresh=true');
    const j = await r.json();
    _savedKasaDevices = j.devices || [];
    renderSavedKasa(_savedKasaDevices);
  } catch (e) {
    list.innerHTML = `<div class="auto-empty">Failed to load: ${escapeHtml(e.message || e)}</div>`;
  }
  _scheduleSavedKasaPoll();
}

// Background re-sync of the Kasa list while the user is on the
// Automation tab.
//
// All-online state: poll the server's cached state every 60s (cheap).
//   The kasa_reconciler_loop keeps that cache fresh on its own
//   schedule.
// Any-offline state: poll with refresh=true (force a probe) every
//   60s. The reconciler's per-device backoff grows up to 30 min after
//   consecutive failures, so the cache can be that stale; without the
//   forced probe the UI sits on OFFLINE long after the plug has
//   recovered. Probing 1-3 plugs per minute is cheap, and we only do
//   it while the user is actively staring at the tab.
let _kasaPollTimer = null;
function _scheduleSavedKasaPoll() {
  if (_kasaPollTimer) { clearInterval(_kasaPollTimer); _kasaPollTimer = null; }
  _kasaPollTimer = setInterval(async () => {
    if (activeTab !== 'automation') {
      clearInterval(_kasaPollTimer);
      _kasaPollTimer = null;
      return;
    }
    try {
      const anyOffline = (_savedKasaDevices || []).some(
        (d) => d.online === false,
      );
      const url = anyOffline
        ? '/api/kasa/saved?refresh=true'
        : '/api/kasa/saved';
      const r = await fetch(url);
      if (!r.ok) return;
      const j = await r.json();
      const next = j.devices || [];
      // Only re-render if the displayed state actually changed.
      // Comparing on the fields the row template depends on keeps
      // arbitrary stat reorderings from triggering layout flicker.
      if (_kasaListChanged(_savedKasaDevices, next)) {
        _savedKasaDevices = next;
        renderSavedKasa(_savedKasaDevices);
      }
    } catch { /* network blip — just try again next tick */ }
  }, 60_000);
}

function _kasaListChanged(prev, next) {
  if ((prev?.length || 0) !== (next?.length || 0)) return true;
  const keysWeRender = ['host', 'alias', 'model', 'is_on', 'online',
    'error', 'last_seen_ts', 'jackery_device_sn'];
  for (let i = 0; i < next.length; i++) {
    const a = prev[i] || {};
    const b = next[i] || {};
    for (const k of keysWeRender) {
      if ((a[k] ?? null) !== (b[k] ?? null)) return true;
    }
  }
  return false;
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
    list.innerHTML = `<div class="auto-empty">Failed to load: ${escapeHtml(e.message || e)}</div>`;
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
        <button class="btn btn-ghost" data-history="${r.id}" type="button">History</button>
        <button class="btn btn-ghost" data-edit="${r.id}" type="button">Edit</button>
        <button class="btn btn-ghost" data-del="${r.id}" type="button">Delete</button>
      </div>
    </div>`;
  }).join('');

  // Wire per-row controls
  list.querySelectorAll('[data-history]').forEach((b) => {
    b.addEventListener('click', () =>
      openAutomationHistory(rules.find((r) => r.id === b.dataset.history)));
  });
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

// ============================================================
// AUTOMATION RULE HISTORY — per-rule firing log + on-time intervals
// ============================================================
let _autoHistoryRule = null;

async function openAutomationHistory(rule) {
  if (!rule) return;
  _autoHistoryRule = rule;
  const card = $('auto-history');
  if (!card) return;
  card.hidden = false;
  $('auto-history-title').textContent = `History — ${rule.name}`;
  card.scrollIntoView({behavior: 'smooth', block: 'start'});
  await _refreshAutoHistory();
}

async function _refreshAutoHistory() {
  const rule = _autoHistoryRule;
  if (!rule) return;
  const days = parseInt($('auto-history-days')?.value || '30', 10) || 30;
  const summary = $('auto-history-summary');
  const empty = $('auto-history-empty');
  const body = $('auto-history-body');
  if (summary) summary.textContent = 'Loading…';
  if (empty) empty.hidden = true;
  if (body) body.innerHTML = '';
  try {
    const r = await fetch(
      `/api/automation/rules/${encodeURIComponent(rule.id)}/history?days=${days}`,
    );
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const j = await r.json();
    _renderAutoHistory(j);
  } catch (e) {
    if (summary) summary.textContent = `Failed to load: ${e.message || e}`;
  }
}

function _renderAutoHistory(j) {
  const summary = $('auto-history-summary');
  const empty = $('auto-history-empty');
  const body = $('auto-history-body');
  const firings = j.firings || [];
  const intervals = j.intervals || [];
  if (!firings.length) {
    if (summary) summary.textContent = `No firings in the last ${j.days} days.`;
    if (empty) empty.hidden = false;
    if (body) body.innerHTML = '';
    return;
  }
  if (empty) empty.hidden = true;
  const totalH = (j.total_on_seconds || 0) / 3600;
  const openCount = intervals.filter((i) => i.open).length;
  const lines = [
    `${firings.length} firing${firings.length === 1 ? '' : 's'} in the last ${j.days} days`,
    `${intervals.length} ON interval${intervals.length === 1 ? '' : 's'} (paired across all rules on this plug)`,
    `total ON-time: ${totalH.toFixed(1)} h`,
  ];
  if (openCount) lines.push('plug currently ON (interval still open)');
  if (summary) summary.textContent = lines.join(' · ');

  const fmtTs = (ts) =>
    ts == null ? '—' : new Date(ts * 1000).toLocaleString();
  const fmtDur = (s) => {
    if (s == null) return '—';
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    return h > 0 ? `${h}h ${m}m` : `${m}m`;
  };
  const safe = (s) => String(s ?? '').replace(/[<>&"]/g, (c) =>
    ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));

  // ON intervals first (the "what actually happened on the plug" view).
  let html = '';
  if (intervals.length) {
    html += `<h3 style="margin:16px 0 6px;font-size:13px;color:var(--text-dim);text-transform:uppercase;letter-spacing:.04em">On intervals</h3>
      <table class="acc-table"><thead><tr>
        <th>Started</th><th>Ended</th><th>Duration</th><th>Opened by</th><th>Closed by</th>
      </tr></thead><tbody>` +
      intervals.slice().reverse().map((i) => `<tr>
        <td>${fmtTs(i.on_at)}</td>
        <td>${i.open ? '<em>still on</em>' : fmtTs(i.off_at)}</td>
        <td>${fmtDur(i.duration_s)}</td>
        <td>${safe(i.opened_by_rule_name || i.opened_by_rule_id || '—')}</td>
        <td>${safe(i.closed_by_rule_name || i.closed_by_rule_id || '—')}</td>
      </tr>`).join('') + '</tbody></table>';
  }
  // Raw firing log second (transparency / debugging).
  html += `<h3 style="margin:16px 0 6px;font-size:13px;color:var(--text-dim);text-transform:uppercase;letter-spacing:.04em">Firings</h3>
    <table class="acc-table"><thead><tr>
      <th>Time</th><th>Action</th><th>SOC</th><th>Threshold</th>
    </tr></thead><tbody>` +
    firings.map((f) => `<tr>
      <td>${fmtTs(f.fired_at)}</td>
      <td>${safe((f.action || '').toUpperCase())}</td>
      <td>${f.soc_at_fire == null ? '—' : Math.round(f.soc_at_fire) + '%'}</td>
      <td>${f.operator || ''} ${f.threshold == null ? '' : f.threshold + '%'}</td>
    </tr>`).join('') + '</tbody></table>';
  if (body) body.innerHTML = html;
}

$('auto-history-close')?.addEventListener('click', () => {
  const card = $('auto-history');
  if (card) card.hidden = true;
  _autoHistoryRule = null;
});

$('auto-history-days')?.addEventListener('change', () => {
  _refreshAutoHistory();
});

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
    wrap.innerHTML = `<div>Discovery failed: ${escapeHtml(e.message || e)}</div>`;
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
    list.innerHTML = `<div class="logs-empty">Failed to load: ${escapeHtml(e.message || e)}</div>`;
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
    out.innerHTML = `<div class="dbg-block"><h3>${escapeHtml(title)}</h3><p class="hint">No data.</p></div>` + out.innerHTML;
    return;
  }
  const cols = columns || Object.keys(rows[0]);
  const head = cols.map(c => `<th>${escapeHtml(c)}</th>`).join('');
  const body = rows.map(r => `<tr>${cols.map(c => `<td>${_dbgFmt(r[c])}</td>`).join('')}</tr>`).join('');
  const ts = new Date().toLocaleTimeString();
  out.innerHTML = `
    <div class="dbg-block">
      <h3>${escapeHtml(title)} <small class="hint">(${ts})</small></h3>
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
  if (typeof v === 'object') return `<code>${escapeHtml(JSON.stringify(v).slice(0,80))}</code>`;
  return escapeHtml(String(v));
}

function _dbgSummary(title, lines) {
  const out = $('debug-output');
  if (!out) return;
  out.hidden = false;
  const ts = new Date().toLocaleTimeString();
  // Callers pass already-formatted strings (often containing intentional
  // <code> / <strong>), so don't escape `l` itself. Title comes from
  // the caller too but is always a literal here.
  const items = lines.map(l => `<div>${l}</div>`).join('');
  out.innerHTML = `
    <div class="dbg-block">
      <h3>${escapeHtml(title)} <small class="hint">(${ts})</small></h3>
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
    const a = all[bucket] || { n: 0, mae: null, bias_pp: null };
    const p = postFix[bucket] || { n: 0, mae: null, bias_pp: null };
    return {
      lead_time: bucket,
      all_samples: a.n,
      all_mae_pp: a.mae,
      all_bias_pp: a.bias_pp,
      post_fix_samples: p.n,
      post_fix_mae_pp: p.mae,
      post_fix_bias_pp: p.bias_pp,
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
    needUpgrade: p.needUpgrade ?? '—',
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
      <pre class="dbg-pre">${escapeHtml(JSON.stringify(j.results || j, null, 2))}</pre>
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
        <pre class="dbg-pre" style="max-height:600px">${escapeHtml(text)}</pre>
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
    fields.innerHTML = `<div class="login-error">Failed to load: ${escapeHtml(e.message || e)}</div>`;
  }
}

function renderSettingsFields(specs) {
  const fields = $('settings-fields');
  fields.innerHTML = '';
  for (const s of specs) {
    const row = document.createElement('div');
    row.className = 'settings-row';
    const k = escapeHtml(s.key);
    row.innerHTML = `
      <label class="settings-label" for="set-${k}">
        <span class="settings-label-text">${escapeHtml(s.label)}</span>
        <span class="settings-hint">${escapeHtml(s.hint)}</span>
      </label>
      <div class="settings-control">
        <input id="set-${k}" name="${k}" type="number"
               min="${escapeHtml(s.min)}" max="${escapeHtml(s.max)}" step="1"
               value="${escapeHtml(s.value)}" required />
        <span class="settings-range">${escapeHtml(s.min)}–${escapeHtml(s.max)}</span>
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
    // Populate the collapsed-card chip so users can see what plan is
    // active without expanding the form. e.g. "TOU · 4 slots · USD" /
    // "Flat · $0.25/kWh" / "—".
    const chip = $('cost-status');
    if (chip) {
      const p = j.plan || {};
      const cur = p.currency || 'USD';
      if (p.type === 'flat') {
        const rate = p.rate_per_kwh != null
          ? `${cur === 'USD' ? '$' : ''}${Number(p.rate_per_kwh).toFixed(2)}/kWh`
          : '';
        chip.textContent = ['Flat', rate, cur === 'USD' ? null : cur]
          .filter(Boolean).join(' · ');
      } else if (p.type === 'tou') {
        const n = (p.slots || []).length;
        chip.textContent = `TOU · ${n} slot${n === 1 ? '' : 's'} · ${cur}`;
      } else {
        chip.textContent = '—';
      }
    }
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
      <span class="slot-label">${escapeHtml(slot.label || '')}</span>
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
    const cfgPayload = cfgRes.ok ? await cfgRes.json() : {};
    const cfg = cfgPayload.config || {};
    const conflicts = cfgPayload.conflicting_rules || [];
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
    renderSmartChargeConflicts(conflicts, cfg.mode);
  } catch (e) {
    console.warn('smart_charge load failed', e);
  }
}

// Battery-rule conflict banner. Shown when smart-charge mode is
// "active" AND there are enabled rules controlling the same plug —
// those rules can fire on battery thresholds and undo what the
// controller just did. Hidden in any other state. The "Disable them"
// button hits POST /api/automation/rules/disable to flip enabled=false
// on every conflicting rule in one round-trip; "Ignore" hides the
// banner for this session (no persistence — it'll come back on next
// load if conflicts still exist).
function renderSmartChargeConflicts(conflicts, mode) {
  const banner = $('sc-conflict-banner');
  if (!banner) return;
  const list = conflicts || [];
  const shouldShow = mode === 'active' && list.length > 0;
  if (!shouldShow) {
    banner.hidden = true;
    return;
  }
  const text = $('sc-conflict-text');
  const ul = $('sc-conflict-list');
  if (text) {
    text.textContent = list.length === 1
      ? '1 enabled battery rule controls the same Kasa plug as smart-charge. '
        + 'It will fire on SOC thresholds and can undo charging decisions.'
      : `${list.length} enabled battery rules control the same Kasa plug as smart-charge. `
        + 'They will fire on SOC thresholds and can undo charging decisions.';
  }
  if (ul) {
    ul.innerHTML = list.map((r) => {
      const op = r.operator || '?';
      const val = r.value ?? '?';
      const action = (r.action || '').toUpperCase();
      const name = r.name || 'Unnamed rule';
      return `<li><strong>${escapeHtml(name)}</strong>: SOC ${escapeHtml(op)} ${escapeHtml(String(val))}% → ${escapeHtml(action)}</li>`;
    }).join('');
  }
  // Stash IDs on the button so the click handler doesn't re-fetch.
  const btn = $('sc-conflict-disable');
  if (btn) btn.dataset.ruleIds = JSON.stringify(list.map(r => r.id));
  banner.hidden = false;
}

document.getElementById('sc-conflict-disable')?.addEventListener('click', async (e) => {
  const btn = e.currentTarget;
  let ids = [];
  try { ids = JSON.parse(btn.dataset.ruleIds || '[]'); } catch { ids = []; }
  if (!ids.length) return;
  btn.disabled = true;
  btn.textContent = 'Disabling…';
  try {
    const r = await fetch('/api/automation/rules/disable', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ rule_ids: ids }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    // Reload the smart-charge panel so the banner clears + rule list
    // reflects the new disabled state.
    await loadSmartCharge();
  } catch (err) {
    btn.disabled = false;
    btn.textContent = 'Disable them';
    const status = $('sc-status');
    if (status) {
      status.hidden = false;
      status.textContent = `Disable failed: ${err.message || err}`;
    }
  }
});

document.getElementById('sc-conflict-dismiss')?.addEventListener('click', () => {
  const banner = $('sc-conflict-banner');
  if (banner) banner.hidden = true;
});

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
  // analytics block above the list summarizes them. Collapsed by
  // default: shows only the latest decision; a "Show N more" toggle
  // reveals the rest. Details buttons inside hidden rows are wired
  // at render time and remain functional once revealed.
  const safe = (s) => String(s || '').replace(/[<>&"]/g, (c) =>
    ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
  const renderRow = (r) => {
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
  };
  renderCollapsedHistory(list, rows.slice(0, 50), renderRow);
  // Wire the Details toggles. Each click hits the decision_details
  // endpoint and renders into the matching panel. The querySelectorAll
  // also finds buttons inside the hidden .history-rest container, so
  // they work as soon as the user expands the list.
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
    panel.innerHTML = `<div class="hint" style="padding:8px 12px; color:#ef4444">Failed: ${escapeHtml(e.message || e)}</div>`;
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
  // The forecast itself is now the truth (no floor), so
  // predicted_sunrise_soc_pct and baseline_predicted_sunrise_soc_pct
  // hold the same value and the counterfactual row would be redundant.
  // The "controller is doing real work" signal is now the deficit +
  // charge schedule rows below — when deficit > 0 and a window is
  // scheduled, smart-charge is actively bridging the gap.
  const baselineLine = '';
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

// ============================================================
// Solar charge UI — inverse controller (Jackery → downstream load)
// ============================================================
async function loadSolarCharge() {
  if (!document.getElementById('solar-form')) return;
  const dev = activeJackeryDevice();
  const deviceSn = dev?.device_sn || '';
  const lbl = document.getElementById('solar-device-label');
  if (lbl) lbl.textContent = dev?.name ? `· ${dev.name}` : '';
  const statusEl = document.getElementById('solar-status');
  if (!deviceSn) {
    if (statusEl) { statusEl.hidden = false; statusEl.textContent = 'No active device — pick one to configure excess diversion.'; }
    return;
  }
  // Clear any stale "no device" message from a prior load that ran
  // before lastStatus was populated. Successful loads should always
  // reset the banner so it doesn't linger.
  if (statusEl) { statusEl.hidden = true; statusEl.textContent = ''; }
  try {
    const q = `?device_sn=${encodeURIComponent(deviceSn)}`;
    const kasaQ = `?jackery_sn=${encodeURIComponent(deviceSn)}`;
    const [cfgRes, kasaRes, statusRes, histRes] = await Promise.all([
      fetch(`/api/solar_charge/config${q}`),
      fetch(`/api/kasa/saved${kasaQ}`),
      fetch(`/api/solar_charge/status${q}`),
      fetch(`/api/solar_charge/history${q}&hours=24&limit=50`),
    ]);
    const cfgPayload = cfgRes.ok ? await cfgRes.json() : {};
    const cfg = cfgPayload.config || {};
    const kasa = kasaRes.ok ? await kasaRes.json() : { devices: [] };
    const status = statusRes.ok ? await statusRes.json() : {};
    const hist = histRes.ok ? await histRes.json() : { decisions: [] };

    // Kasa picker (same pattern as smart-charge).
    const sel = document.getElementById('solar-kasa-host');
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
      const opt = document.createElement('option');
      opt.value = currentHost;
      opt.textContent = `${currentHost} (not in registry)`;
      opt.selected = true;
      sel.appendChild(opt);
    }

    // Hydrate fields with config + defaults.
    document.getElementById('solar-mode').value = cfg.mode || 'off';
    document.getElementById('solar-car-load-w').value = cfg.car_load_w ?? 1400;
    document.getElementById('solar-comfort-high').value = cfg.comfort_high_pct ?? 70;
    document.getElementById('solar-comfort-low').value = cfg.comfort_low_pct ?? 30;
    document.getElementById('solar-min-hold').value = cfg.min_hold_s ?? 30;
    document.getElementById('solar-surplus-buffer').value = cfg.surplus_buffer_w ?? 100;
    document.getElementById('solar-on-hysteresis').value = cfg.on_hysteresis_pp ?? 3;
    document.getElementById('solar-safety-margin').value = cfg.safety_margin_pp ?? 5;
    document.getElementById('solar-max-system-load').value = cfg.max_system_load_w ?? 800;
    document.getElementById('solar-inverter-protect-load').value = cfg.inverter_protect_load_w ?? 2100;
    document.getElementById('solar-inverter-protect-cooldown').value = cfg.inverter_protect_cooldown_s ?? 1800;

    // Runtime panel: plug state + today's diverted kWh + last decision reason.
    const rt = status.runtime || {};
    const divertedKwh = ((status.diverted_wh_today || 0) / 1000).toFixed(2);
    const lastDecision = (status.recent_decisions || [])[0];
    const plugBadge = rt.plug_is_on
      ? '<span class="sc-mode" style="background:#2d5a2d">PLUG ON</span>'
      : '<span class="sc-mode" style="background:#555">PLUG OFF</span>';
    const rtPanel = document.getElementById('solar-runtime');
    if (rtPanel) {
      rtPanel.hidden = false;
      rtPanel.innerHTML = `
        <div style="display:flex; gap:14px; flex-wrap:wrap; align-items:center">
          ${plugBadge}
          <span class="hint">Today diverted: <strong>${divertedKwh} kWh</strong></span>
          ${lastDecision ? `<span class="hint">Last decision: <strong>${lastDecision.action}</strong> — ${escapeHtml(lastDecision.reason || '')}</span>` : ''}
        </div>`;
    }

    // History block (last 24h decisions). Collapsed by default: shows
    // only the latest decision with a "Show N more" toggle for the rest.
    const histBlock = document.getElementById('solar-history-block');
    const histEl = document.getElementById('solar-history');
    if (histBlock && histEl) {
      const rows = hist.decisions || [];
      histBlock.hidden = rows.length === 0;
      const renderRow = (r) => {
        const when = new Date((r.decided_at || 0) * 1000).toLocaleString();
        const action = r.action || '?';
        const modeBadge = `<span class="sc-mode">[${r.mode || '?'}]</span>`;
        const exec = r.executed ? '✓' : '·';
        const surplus = r.surplus_w != null ? `${Math.round(r.surplus_w)}W` : '—';
        const soc = r.current_soc_pct != null ? `${Math.round(r.current_soc_pct)}%` : '—';
        return `<div class="sc-history-row">
          <span class="hint" style="min-width:160px">${when}</span>
          ${modeBadge}
          <strong>${action}</strong> ${exec}
          <span class="hint">SOC ${soc} · surplus ${surplus}</span>
          <span class="hint" style="flex:1; text-align:right">${escapeHtml(r.reason || '')}</span>
        </div>`;
      };
      renderCollapsedHistory(histEl, rows, renderRow);
    }
  } catch (e) {
    console.error('loadSolarCharge', e);
    const s = document.getElementById('solar-status');
    if (s) { s.hidden = false; s.textContent = `Load failed: ${e.message || e}`; }
  }
}

document.getElementById('solar-form')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const status = document.getElementById('solar-status');
  const deviceSn = activeJackeryDevice()?.device_sn;
  if (!deviceSn) {
    status.hidden = false;
    status.textContent = 'No active device — cannot save.';
    return;
  }
  status.hidden = false;
  status.textContent = 'Saving…';
  const cfg = {
    mode: document.getElementById('solar-mode').value,
    kasa_device_host: document.getElementById('solar-kasa-host').value || null,
    car_load_w: parseInt(document.getElementById('solar-car-load-w').value, 10) || 1400,
    comfort_high_pct: parseInt(document.getElementById('solar-comfort-high').value, 10) || 70,
    comfort_low_pct: parseInt(document.getElementById('solar-comfort-low').value, 10) || 30,
    min_hold_s: parseInt(document.getElementById('solar-min-hold').value, 10) || 30,
    surplus_buffer_w: parseInt(document.getElementById('solar-surplus-buffer').value, 10) || 100,
    on_hysteresis_pp: parseInt(document.getElementById('solar-on-hysteresis').value, 10) || 3,
    safety_margin_pp: parseInt(document.getElementById('solar-safety-margin').value, 10) || 5,
    max_system_load_w: parseInt(document.getElementById('solar-max-system-load').value, 10) || 800,
    inverter_protect_load_w: parseInt(document.getElementById('solar-inverter-protect-load').value, 10) || 2100,
    inverter_protect_cooldown_s: parseInt(document.getElementById('solar-inverter-protect-cooldown').value, 10) || 1800,
  };
  try {
    const r = await fetch(`/api/solar_charge/config?device_sn=${encodeURIComponent(deviceSn)}`, {
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
    loadSolarCharge();  // refresh runtime panel
  } catch (err) {
    status.textContent = `Save failed: ${err.message || err}`;
  }
});

document.getElementById('solar-evaluate')?.addEventListener('click', async () => {
  const status = document.getElementById('solar-status');
  const deviceSn = activeJackeryDevice()?.device_sn;
  status.hidden = false;
  status.textContent = 'Evaluating…';
  try {
    const url = deviceSn
      ? `/api/solar_charge/evaluate_now?device_sn=${encodeURIComponent(deviceSn)}`
      : '/api/solar_charge/evaluate_now';
    const r = await fetch(url, { method: 'POST' });
    const j = await r.json();
    if (j.plan) {
      status.textContent = `${j.plan.action.toUpperCase()} — ${j.plan.reason}`;
    } else {
      status.textContent = j.reason || 'No plan returned.';
    }
  } catch (e) {
    status.textContent = `Evaluate failed: ${e.message || e}`;
  }
});

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
    const saved = await r.json().catch(() => ({}));
    status.textContent = 'Saved.';
    // The POST response includes `conflicting_rules` keyed off the
    // saved config — render it immediately so flipping mode → active
    // surfaces the banner without a second round-trip.
    renderSmartChargeConflicts(saved.conflicting_rules || [],
                                saved.config?.mode || cfg.mode);
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
  // Per-browser view selection — sets a cookie so this browser's UI shows
  // the chosen Jackery without changing what other browsers see, and
  // without changing which device the automation worker manages. The
  // bridge polls every device on every tick, so picking a non-bridge-
  // active device just switches which slice of cloud_meta we render.
  try {
    await fetch('/api/view/select_device', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ device_id }),
    });
  } catch (err) {
    console.warn('view/select_device failed', err);
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

// Brand-as-home: clicking "Jackery Monitor" reloads to / (which lands
// on the Live tab — that's the default activeTab on boot). Treats it
// as the "fix it" button: same gesture as cmd+R but always lands on
// Live regardless of which tab the user was on. Modifier/middle-clicks
// fall through to the <a href="/"> default so users can open Live in
// a new tab.
$('brand-home')?.addEventListener('click', (e) => {
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
  e.preventDefault();
  // Clear hash + query first, then reload. location.assign('/') from
  // /#forecast just triggers a hashchange (no reload) because it's
  // same-origin same-path different-hash — so we replaceState to
  // strip the hash, then reload to actually refresh the page.
  if (location.hash || location.search) {
    history.replaceState(null, '', location.pathname);
  }
  location.reload();
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
    } else if (msg.type === 'kasa_updated') {
      // Server-side reconciler / probe just changed a Kasa device's
      // user-visible state (online/is_on/error). Re-fetch the cached
      // list so the row flips immediately, no waiting for the 60s poll.
      _onKasaUpdatedPush();
      // Also refresh the Automation tab dot — its only other refresh
      // is a 3-min poll, so a Kasa device coming back online (or going
      // offline) used to leave the dot stale until the next tick. That
      // produced "lit dot, nothing to act on" when the user clicked in.
      refreshAutomationDot();
    } else if (msg.type === 'automation_fired') {
      // A rule just edge-triggered. Patch our local cache so last_fired
      // updates without re-fetching, and re-render if the user is on
      // the Automation tab.
      _onAutomationFiredPush(msg.data);
    }
  };
  ws.onclose = () => setTimeout(connectWs, 2000);
  ws.onerror = () => { try { ws.close(); } catch {} };
}

// Push handlers — keep the polling fallbacks in place but free-ride
// on WS broadcasts when they arrive so the common case is real-time.
async function _onKasaUpdatedPush() {
  // Only pay the network cost when the user is actually looking. The
  // 60s background poll picks up changes whenever they navigate back.
  if (activeTab !== 'automation') return;
  try {
    const r = await fetch('/api/kasa/saved');
    if (!r.ok) return;
    const j = await r.json();
    const next = j.devices || [];
    if (typeof _kasaListChanged === 'function'
        && _kasaListChanged(_savedKasaDevices, next)) {
      _savedKasaDevices = next;
      renderSavedKasa(_savedKasaDevices);
    }
  } catch { /* WS push lost — next poll tick will catch up */ }
}

function _onAutomationFiredPush(data) {
  if (!data || !data.id) return;
  // Patch the in-memory rule cache so last_fired surfaces immediately
  // even on the dot-tab without re-fetching.
  if (Array.isArray(_allRules)) {
    const rule = _allRules.find((r) => r.id === data.id);
    if (rule) {
      rule.last_fired = data.last_fired || (Date.now() / 1000);
      rule.last_error = null;  // a fire that broadcast = a fire that succeeded
    }
  }
  // Re-render the rules list if the user is looking at it.
  if (activeTab === 'automation' && typeof renderRulesWithFilter === 'function') {
    renderRulesWithFilter();
  }
  // If the History panel is open for this rule, refresh it too.
  if (typeof _autoHistoryRule !== 'undefined' && _autoHistoryRule
      && _autoHistoryRule.id === data.id
      && typeof _refreshAutoHistory === 'function') {
    _refreshAutoHistory();
  }
  // Show a transient toast so even off-tab users see the firing.
  const b = $('alert-banner');
  if (b) {
    b.textContent = `Rule fired: ${data.name} → ${(data.action || '').toUpperCase()} ${data.kasa_alias || ''}`;
    show(b, true);
    setTimeout(() => show(b, false), 5000);
  }
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
    loadSolarCharge();
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
    // Daily table/records are per-device — drop the old cache so a CSV
    // export can't carry the previous device's data, and refresh in
    // place if the Energy tab is what the user is looking at.
    energyDailyCache = null;
    if (activeTab === 'energy') fetchEnergyDaily();
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
    const solBtn = $('solar-charge-toggle');
    if (solBtn) solBtn.hidden = true;
    refreshSolarChargeButton().catch(() => {});
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
  // Use system SOC (capacity-weighted across main + packs) when present —
  // matches what the user sees on the SOC card. Fall back to main pack
  // for legacy / single-unit devices.
  const soc   = Number(t.system_soc_pct ?? t.battery_percent ?? 0);
  // System capacity from the server's _total_capacity_wh (main + every
  // expansion pack). Falls back to a single-pack default when the server
  // doesn't enrich telemetry (mock backend, very old responses).
  const capacityWh = Number(t.capacity_wh ?? 5040);
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
      const wh = ((100 - soc) / 100) * capacityWh;
      const eta = wh / netW;
      timeLabel = eta > 0 && isFinite(eta) ? `${fmt(eta, 1)} h until full` : 'Charging…';
    }
  } else {
    // Discharging — show ETA to empty
    if (ttEmpty > 0) {
      timeLabel = `${fmt(ttEmpty, 1)} h until empty`;
    } else {
      const wh = (soc / 100) * capacityWh;
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
    // Excess-diversion line: show only when there's something to show.
    // Threshold at 50Wh so a brief sensor blip doesn't surface the row;
    // a real diversion session is kWh-scale.
    const divWh = s.energy.today.solar_charge_diverted_wh || 0;
    const divRow = $('today-diverted-row');
    const divEl = $('today-diverted-kwh');
    if (divRow && divEl) {
      if (divWh >= 50) {
        divRow.hidden = false;
        divEl.textContent = fmtKwh(divWh);
      } else {
        divRow.hidden = true;
      }
    }
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

  // Inverter recovery watchdog decoration on the AC button.
  // (Pure decoration — the server is doing the actual retry MQTT
  // commands; this just communicates the state visually.)
  //   - consecutive_attempts > 0, no error  → yellow wd-warn + tooltip
  //   - error_message latched               → red wd-err + tooltip; click
  //                                            dismisses the error
  //   - idle                                → clear both
  // Namespaced .wd-* so they don't collide with .switch.warn (smart-
  // charge active-mode indicator on other buttons).
  const acSw = document.querySelector('.switch[data-port="ac"]');
  const wd = s.inverter_watchdog;
  if (acSw) {
    if (wd && wd.error_message) {
      acSw.classList.remove('wd-warn');
      acSw.classList.add('wd-err');
      acSw.title = `${wd.error_message}\n\nClick AC to dismiss this error.`;
    } else if (wd && wd.consecutive_attempts > 0) {
      acSw.classList.remove('wd-err');
      acSw.classList.add('wd-warn');
      acSw.title = (
        `Inverter recovery in progress: retry ${wd.consecutive_attempts}/5. ` +
        `Sending AC-on every 10s; will surface an error after 5 attempts.`);
    } else {
      acSw.classList.remove('wd-err', 'wd-warn');
      acSw.title = '';
    }
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
// KASA PLUG TOGGLE (factory) — backs both the AC-charge and the
// Excess-diversion (solar_charge) buttons in the Power panel.
//
// Each button is bound to one controller's per-device config
// endpoint (smart_charge or solar_charge) and renders only when
// the user has assigned a Kasa plug to that controller. Inlined
// as the 5th/6th button in the Power row; auto-hides when no
// plug is configured.
//
// When the controller's mode is 'active' the button gets a
// yellow border + tooltip explaining the loop will re-decide on
// the next tick (Q4-A: warn but don't block). For smart_charge
// the loop is 5 min; for solar_charge it's 30s.
//
// Error path: the kasa backend already retries 3x
// (kasa_client._with_retry), so when a click bubbles back here
// the plug is genuinely unreachable. We:
//   1. Re-sync to actual plug state so the button reflects truth
//      (may have succeeded server-side even if the response was
//      lost) — user can immediately click again to retry.
//   2. Surface the error as a tooltip + yellow border, NOT as a
//      replacement button label. State stays readable.
// ============================================================
function makeKasaPlugToggle({ buttonId, stateId, controllerName,
                             configPath, revertWindow }) {
  let busy = false;
  const btn = document.getElementById(buttonId);

  async function readHost() {
    const deviceSn = activeJackeryDevice()?.device_sn;
    if (!deviceSn) return { deviceSn: null, cfg: null };
    try {
      const r = await fetch(`${configPath}?device_sn=${encodeURIComponent(deviceSn)}`);
      if (r.ok) return { deviceSn, cfg: (await r.json()).config };
    } catch (e) { /* ignore */ }
    return { deviceSn, cfg: null };
  }

  async function refresh() {
    if (!btn) return;
    const { deviceSn, cfg } = await readHost();
    if (!deviceSn) { btn.hidden = true; return; }
    const host = cfg?.kasa_device_host;
    if (!host) { btn.hidden = true; return; }
    btn.hidden = false;
    const mode = cfg.mode || 'off';
    if (mode === 'active') {
      btn.classList.add('warn');
      btn.title = `Plug ${host} · ${controllerName} ${mode}\nWarning: your manual toggle may be reverted within ${revertWindow}.`;
    } else {
      btn.classList.remove('warn');
      btn.title = `Plug ${host} · ${controllerName} ${mode}`;
    }
    // Read plug state. Failures = unknown (—). Skip while the
    // user's mid-click so optimistic state isn't clobbered.
    const stateEl = document.getElementById(stateId);
    if (!busy) {
      try {
        const r = await fetch(`/api/kasa/status?host=${encodeURIComponent(host)}`);
        if (r.ok) {
          const isOn = !!(await r.json()).is_on;
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

  btn?.addEventListener('click', async () => {
    if (busy) return;
    const { cfg } = await readHost();
    const host = cfg?.kasa_device_host;
    if (!host) return;
    const stateEl = document.getElementById(stateId);
    const wantOn = !btn.classList.contains('on');
    busy = true;
    btn.classList.add('pending');
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
      // Optimistic update + clear stale error decoration. refresh()
      // fires on the next 30s tick to confirm the actual plug state.
      btn.classList.toggle('on', wantOn);
      btn.classList.remove('pending', 'warn');
      if (stateEl) stateEl.textContent = wantOn ? 'ON' : 'OFF';
      busy = false;
      refresh().catch(() => {});
    } catch (e) {
      busy = false;
      btn.classList.remove('pending');
      await refresh().catch(() => {});
      btn.classList.add('warn');
      btn.title = `Last toggle failed: ${e.message || e}\nClick to retry. Hover for details.`;
    }
  });

  // Periodic refresh — plug state changes infrequently so 30s is plenty.
  setInterval(refresh, 30_000);
  // Initial render — fire once at module load AND once when applyStatus
  // resolves an active device, so the button shows up without waiting 30s.
  refresh().catch(() => {});

  return { refresh };
}

const acChargeToggle = makeKasaPlugToggle({
  buttonId: 'ac-charge-toggle',
  stateId: 'ac-charge-state',
  controllerName: 'smart-charge',
  configPath: '/api/smart_charge/config',
  revertWindow: '5 min',
});
const solarChargeToggle = makeKasaPlugToggle({
  buttonId: 'solar-charge-toggle',
  stateId: 'solar-charge-state',
  controllerName: 'solar-charge',
  configPath: '/api/solar_charge/config',
  revertWindow: '30s',
});
const refreshAcChargeButton = acChargeToggle.refresh;
const refreshSolarChargeButton = solarChargeToggle.refresh;

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

  // Time-to-full (charging) / time-to-empty (discharging). Pulled
  // straight from device telemetry. Compact arrow-prefixed format
  // ("↑ 0.7h" / "↓ 9.2h") keeps the line inside the battery ring's
  // chord at this y-position even on mobile where the SVG fonts are
  // bumped up — the verbose "until full" / "remaining" text is too
  // wide there. Direction is also encoded by the green/amber color
  // matching the battery node ring, so the arrow is reinforcement
  // rather than the only signal.
  const timerEl = $('flow-timer');
  if (timerEl) {
    const ttFull  = Number(t.time_to_full_h    ?? 0);
    const ttEmpty = Number(t.time_remaining_h  ?? 0);
    // Same fallback as the hero-card "until full / until empty" label:
    // when the cloud doesn't fill ttFull/ttEmpty (common during mixed
    // solar+load operation, where net power is small or fluctuating),
    // synthesize from current SOC + net W + system capacity. Without
    // this the flow-timer permanently rendered "—" on rigs that
    // basically never trigger Jackery's own ETA computation.
    const inW = Number(t.input_power_w  ?? 0);
    const outW = Number(t.output_power_w ?? 0);
    const netW = inW - outW;
    const soc = Number(t.system_soc_pct ?? t.battery_percent ?? 0);
    const capacityWh = Number(t.capacity_wh ?? 5040);
    const synthFull  = netW >  25
      ? ((100 - soc) / 100 * capacityWh) / netW
      : 0;
    const synthEmpty = netW < -25
      ? (soc / 100 * capacityWh) / Math.abs(netW)
      : 0;
    let txt = '—';
    let cls = '';
    let title = '';
    if (ttFull > 0) {
      txt = `↑ ${ttFull.toFixed(1)}h`;
      cls = 'charging';
      title = `${ttFull.toFixed(1)} h until full`;
    } else if (ttEmpty > 0) {
      txt = `↓ ${ttEmpty.toFixed(1)}h`;
      cls = 'discharging';
      title = `${ttEmpty.toFixed(1)} h remaining`;
    } else if (synthFull > 0 && isFinite(synthFull)) {
      txt = `↑ ${synthFull.toFixed(1)}h`;
      cls = 'charging';
      title = `~${synthFull.toFixed(1)} h until full (synthesized — cloud didn't supply ETA)`;
    } else if (synthEmpty > 0 && isFinite(synthEmpty)) {
      txt = `↓ ${synthEmpty.toFixed(1)}h`;
      cls = 'discharging';
      title = `~${synthEmpty.toFixed(1)} h until empty (synthesized — cloud didn't supply ETA)`;
    }
    timerEl.textContent = txt;
    timerEl.classList.remove('charging', 'discharging');
    if (cls) timerEl.classList.add(cls);
    // SVG <title> child = native browser tooltip on hover; keeps the
    // verbose phrasing accessible without forcing it into the visible
    // glyph budget. Lazily create on first use; replace text after.
    let titleNode = timerEl.querySelector('title');
    if (title) {
      if (!titleNode) {
        titleNode = document.createElementNS(
          'http://www.w3.org/2000/svg', 'title');
        timerEl.appendChild(titleNode);
      }
      titleNode.textContent = title;
    } else if (titleNode) {
      titleNode.remove();
    }
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

// ============================================================
// ENERGY DAILY BREAKDOWN + RECORDS
// One /api/energy/daily?days=365 fetch per tab-open, cached per
// device (energyDailyCache). Range picker / search / column sort
// all re-render client-side from that cache — no refetch.
// ============================================================
let energyDailyDays = 90;                            // table range (client-side slice)
let energyDailySort = { key: 'date', dir: 'desc' };  // default: newest first
let energyDailySearch = '';
let _energyDailyRetries = 0;

const MONTHS_SHORT = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                      'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

// "2026-07-13" -> "Jul 13". String parse (no Date()) so the browser's
// timezone can't shift the local-day bucket the server computed.
function fmtDayShort(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || ''));
  if (!m) return String(iso || '');
  return `${MONTHS_SHORT[Number(m[2]) - 1] || m[2]} ${Number(m[3])}`;
}

// Today's YYYY-MM-DD in the DEVICE's local tz (endpoint reports tz_offset_s).
function energyDailyToday() {
  const off = Number(energyDailyCache?.tz_offset_s) || 0;
  return new Date(Date.now() + off * 1000).toISOString().slice(0, 10);
}

async function fetchEnergyDaily() {
  const deviceSn = activeJackeryDevice()?.device_sn;
  if (!deviceSn) {
    // The tab can open before the first WS status delivers the device
    // list — retry briefly while the Energy tab is still in front.
    if (activeTab === 'energy' && _energyDailyRetries < 5) {
      _energyDailyRetries++;
      setTimeout(fetchEnergyDaily, 1200);
    }
    return;
  }
  _energyDailyRetries = 0;
  // Serve from cache only while it's fresh — the daily rollup changes as
  // the day accrues, so a long-lived dashboard refetches on tab open
  // rather than freezing the Records card at first render.
  const cacheFresh = energyDailyCache
    && energyDailyCache.device_sn === deviceSn
    && (Date.now() - (energyDailyCache._fetched_at || 0)) < 10 * 60 * 1000;
  if (cacheFresh) {
    renderEnergyRecords();
    renderEnergyDailyTable();
    return;
  }
  try {
    const r = await fetch(`/api/energy/daily?device_sn=${encodeURIComponent(deviceSn)}&days=365`);
    if (!r.ok) return;
    const j = await r.json();
    if (!Array.isArray(j.daily)) return;
    j._fetched_at = Date.now();
    energyDailyCache = j;
    renderEnergyRecords();
    renderEnergyDailyTable();
  } catch (e) { console.warn('energy daily fetch failed', e); }
}

// Day entry with the highest strictly-positive `key`, or null. Used for
// both the Records tiles and the record-row highlights in the table.
function _maxDay(daily, key) {
  let best = null;
  for (const d of daily) {
    const v = Number(d[key]);
    if (!Number.isFinite(v) || v <= 0) continue;
    if (!best || v > Number(best[key])) best = d;
  }
  return best;
}

function renderEnergyRecords() {
  const grid = $('energy-records-grid');
  const card = $('energy-records');
  if (!grid || !card) return;
  const daily = energyDailyCache?.daily || [];
  if (!daily.length) { show(card, false); return; }

  const tiles = [];
  const addRecord = (label, key) => {
    const day = _maxDay(daily, key);
    if (!day) return;
    tiles.push({ label, value: Number(day[key]).toFixed(2), sub: fmtDayShort(day.date) });
  };
  addRecord('Peak solar day',       'solar_kwh');
  addRecord('Peak consumption day', 'consumed_kwh');
  addRecord('Peak grid day',        'grid_kwh');
  addRecord('Best diversion day',   'diverted_kwh');

  // 30-day daily averages (most recent 30 local-day buckets).
  const last30 = daily.slice(-30);
  const avg = (key) =>
    last30.reduce((s, d) => s + (Number(d[key]) || 0), 0) / (last30.length || 1);
  tiles.push({ label: 'Avg solar/day',    value: avg('solar_kwh').toFixed(2),    sub: 'last 30 days' });
  tiles.push({ label: 'Avg consumed/day', value: avg('consumed_kwh').toFixed(2), sub: 'last 30 days' });

  grid.innerHTML = tiles.map((t) => `
    <div class="energy-record">
      <div class="kpi-sub rec-label">${escapeHtml(t.label)}</div>
      <div class="kpi-value">${escapeHtml(t.value)}<small>kWh</small></div>
      <div class="kpi-sub">${escapeHtml(t.sub)}</div>
    </div>`).join('');
  show(card, true);
}

// kWh cell: 0 / null renders as an em dash so real energy pops.
function _kwhCell(v) {
  const n = Number(v);
  return (!Number.isFinite(n) || n === 0) ? '—' : n.toFixed(2);
}

function _wattsCell(v) {
  const n = Number(v);
  return Number.isFinite(n) ? String(Math.round(n)) : '—';
}

// Range slice + search filter + sort over the cached 365d payload.
// Search matches the ISO date AND the human "Jul 13" form (case-insensitive).
function _energyDailyRows() {
  const daily = energyDailyCache?.daily || [];
  let rows = daily.slice(-energyDailyDays);   // payload is oldest -> newest
  const q = energyDailySearch.trim().toLowerCase();
  if (q) {
    rows = rows.filter((d) =>
      String(d.date || '').toLowerCase().includes(q) ||
      fmtDayShort(d.date).toLowerCase().includes(q));
  }
  const { key, dir } = energyDailySort;
  const sign = dir === 'asc' ? 1 : -1;
  return rows.slice().sort((a, b) => {
    if (key === 'date') return sign * String(a.date).localeCompare(String(b.date));
    // Explicit null check first: Number(null) === 0, which would sort
    // missing values mid-list instead of last.
    const av = a[key], bv = b[key];
    const an = (av == null || !Number.isFinite(Number(av))) ? -Infinity : Number(av);
    const bn = (bv == null || !Number.isFinite(Number(bv))) ? -Infinity : Number(bv);
    if (an !== bn) return sign * (an - bn);
    return String(b.date).localeCompare(String(a.date)); // deterministic tiebreak
  });
}

function renderEnergyDailyTable() {
  const body = $('energy-daily-body');
  const card = $('energy-daily');
  if (!body || !card) return;
  const daily = energyDailyCache?.daily || [];
  if (!daily.length) { show(card, false); return; }

  const rows = _energyDailyRows();
  const today = energyDailyToday();
  // Record highlights are computed on the FULL payload, not the visible
  // slice, so a record day is marked in every range that contains it.
  const peakSolarDate = _maxDay(daily, 'solar_kwh')?.date;
  const peakLoadDate  = _maxDay(daily, 'consumed_kwh')?.date;

  body.innerHTML = rows.map((d) => {
    const cls = [
      d.date === peakSolarDate && 'record-solar',
      d.date === peakLoadDate  && 'record-load',
    ].filter(Boolean).join(' ');
    const soc = (d.min_soc != null && d.max_soc != null)
      ? `${escapeHtml(d.min_soc)}–${escapeHtml(d.max_soc)}%` : '—';
    const badge = d.date === today ? ' <span class="today-badge">today</span>' : '';
    return `
      <tr${cls ? ` class="${cls}"` : ''}>
        <td title="${escapeHtml(d.date)}">${escapeHtml(fmtDayShort(d.date))}${badge}</td>
        <td class="num">${_kwhCell(d.solar_kwh)}</td>
        <td class="num">${_kwhCell(d.consumed_kwh)}</td>
        <td class="num">${_kwhCell(d.charged_kwh)}</td>
        <td class="num">${_kwhCell(d.grid_kwh)}</td>
        <td class="num">${_kwhCell(d.diverted_kwh)}</td>
        <td class="num">${_wattsCell(d.peak_solar_w)}</td>
        <td class="num">${_wattsCell(d.peak_output_w)}</td>
        <td class="num">${soc}</td>
      </tr>`;
  }).join('');
  show($('energy-daily-empty'), rows.length === 0);
  show(card, true);
}

// ---- Daily breakdown controls (static DOM, bound once at load) ----
document.querySelectorAll('.drange-btn').forEach((btn) => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.drange-btn').forEach((b) => b.classList.toggle('on', b === btn));
    energyDailyDays = parseInt(btn.dataset.days, 10) || 90;
    renderEnergyDailyTable();
  });
});

document.querySelectorAll('.daily-table th.sortable').forEach((th) => {
  th.addEventListener('click', () => {
    const key = th.dataset.sort;
    if (energyDailySort.key === key) {
      energyDailySort.dir = energyDailySort.dir === 'asc' ? 'desc' : 'asc';
    } else {
      energyDailySort = { key, dir: 'desc' };  // new column: biggest/newest first
    }
    document.querySelectorAll('.daily-table th.sortable').forEach((t) => {
      t.classList.toggle('sort-asc',  t === th && energyDailySort.dir === 'asc');
      t.classList.toggle('sort-desc', t === th && energyDailySort.dir === 'desc');
    });
    renderEnergyDailyTable();
  });
});

$('energy-daily-search')?.addEventListener('input', (e) => {
  energyDailySearch = e.target.value || '';
  renderEnergyDailyTable();
});

// Export the CURRENTLY FILTERED rows (range + search + sort applied).
$('energy-daily-export')?.addEventListener('click', () => {
  if (!energyDailyCache) return;
  const cols = ['date', 'solar_kwh', 'consumed_kwh', 'charged_kwh', 'grid_kwh',
                'diverted_kwh', 'peak_solar_w', 'peak_output_w', 'min_soc', 'max_soc'];
  const lines = [cols.join(',')];
  for (const d of _energyDailyRows()) {
    lines.push(cols.map((c) => (d[c] == null ? '' : String(d[c]))).join(','));
  }
  const sn = String(energyDailyCache.device_sn || 'device').replace(/[^A-Za-z0-9._-]/g, '_');
  const blob = new Blob([lines.join('\n') + '\n'], { type: 'text/csv' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `energy-daily-${sn}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
});

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
    // Forecast is per-device — pass the currently-viewed Jackery's SN so
    // the Forecast tab follows the per-browser picker.
    const sn = activeJackeryDevice()?.device_sn;
    const url = sn ? `/api/forecast?device_sn=${encodeURIComponent(sn)}` : '/api/forecast';
    const r = await fetch(url);
    if (!r.ok) { showNeedsConfig(); return; }
    const j = await r.json();
    if (!j.configured) {
      showNeedsConfig('Allow location to enable forecasts',
        'The forecaster needs your approximate location to know which weather to fetch. Click below to share it.');
      return;
    }
    if (j.error) {
      const detail = j.error_detail
        ? `The forecast needs weather data and can't reach the weather service right now (${j.error_detail}). It refreshes automatically once the connection is restored.`
        : body.textContent;
      showNeedsConfig(j.error, detail);
      return;
    }
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
    // Show the location these forecasts are based on. Async, fires its
    // own /api/location request — doesn't block the chart render.
    refreshForecastCurrentLoc();
    const setText = (id, v, d = 0) => { const el = $(id); if (el) el.textContent = fmt(v, d); };
    const dev = activeJackeryDevice();
    const title = $('forecast-title');
    if (title) {
      const base = dev?.name
        ? `${dev.name} — next 5 days`
        : 'State of charge — next 5 days';
      // Note the fallback when the live weather API is down: either the
      // real last-good stored forecast, or (cold start) a recent-average
      // climatology.
      const wxNote = j.weather_synthetic
        ? ' · ⚠ recent-average solar (weather service offline)'
        : j.weather_stale
          ? ' · ⚠ last-good forecast (weather service offline)'
          : '';
      title.textContent = base + wxNote;
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
    // Per-day strip above the chart — 5 compact tiles, one per day
    // in the forecast window. Same data, no extra fetch.
    renderForecastDayStrip(j);
    // Today's energy budget breakdown — same data as the chart, but
    // walks the simulator math from now to sunset so the user can
    // verify how SOC_now becomes SOC_sunset. Pure transform of `j`,
    // no extra fetch.
    renderTodayEnergyBudget(j);
    // Daily sunset/sunrise pred-vs-actual table + MAE summary. Independent
    // of the main forecast — own /api/daily_summary fetch with a user-
    // controlled window. Render is fire-and-forget; failures shouldn't
    // block the rest of the Forecast tab.
    renderDailyAccuracy().catch((err) =>
      console.warn('daily accuracy render failed', err));
  } catch (e) { console.warn('forecast fetch failed', e); }
}

// ============================================================
// Per-day strip above the forecast chart — one compact tile per
// day in the ~5-day forecast window. Headline numbers only
// (start→end SOC + solar kWh), with color + ⚠ marker when SOC
// bottoms at 0. Pure transform of /api/forecast.
// ============================================================
function renderForecastDayStrip(j) {
  const strip = $('forecast-day-strip');
  if (!strip) return;
  const fc = j?.forecast || [];
  if (!fc.length || !j.ready) {
    strip.innerHTML = '';
    strip.hidden = true;
    return;
  }
  // Group hours by local-date string. Browser timezone is what the
  // user sees on the chart x-axis, so it matches their mental model.
  const days = [];
  let currentKey = null;
  let current = null;
  for (const h of fc) {
    const d = new Date(h.ts * 1000);
    const key = d.toDateString(); // "Wed May 06 2026"
    if (key !== currentKey) {
      if (current) days.push(current);
      currentKey = key;
      current = {
        key,
        date: d,
        hours: [],
        solarWh: 0,
        minSoc: Infinity,
        maxSoc: -Infinity,
        depleted: false,
      };
    }
    current.hours.push(h);
    current.solarWh += Number(h.solar_w ?? 0);
    if (h.predicted_soc != null) {
      const s = Number(h.predicted_soc);
      if (s < current.minSoc) current.minSoc = s;
      if (s > current.maxSoc) current.maxSoc = s;
      // The simulator clamps at 0 (hard floor), so any predicted_soc
      // <= 0.5 means we ran out — flag it.
      if (s <= 0.5) current.depleted = true;
    }
  }
  if (current) days.push(current);

  // For start_soc on each day: day 0 starts at j.starting_soc_pct
  // (NOW); subsequent days inherit the previous day's end. End_soc =
  // last hour's predicted_soc (clamped to 0+).
  let prevEnd = Number(j.starting_soc_pct ?? 0);
  for (const d of days) {
    d.startSoc = prevEnd;
    const lastSoc = d.hours[d.hours.length - 1]?.predicted_soc;
    d.endSoc = lastSoc != null ? Number(lastSoc) : prevEnd;
    prevEnd = d.endSoc;
    // Sunset SOC: predicted_soc at the LAST hour with solar > 0 for
    // this day. Matches the hero "At sunset" card's calc (renderEodPill)
    // so the two surfaces show identical numbers on overlapping days.
    // Sunrise SOC: predicted_soc at the LAST dark hour BEFORE the
    // first daylight hour — the overnight low (matches hero's
    // "At sunrise" calc). Null on days where the corresponding
    // transition isn't in the window (e.g. today's tile, where
    // sunrise already happened before "now" and isn't in fc).
    let sunsetSoc = null;
    let sunriseSoc = null;
    let firstDaylightIdx = -1;
    for (let k = 0; k < d.hours.length; k++) {
      const h = d.hours[k];
      const isDay = (h.solar_w || 0) > 0;
      if (isDay && firstDaylightIdx === -1) firstDaylightIdx = k;
      if (isDay && h.predicted_soc != null) sunsetSoc = Number(h.predicted_soc);
    }
    // Sunrise = SOC at the dark hour just before the first daylight
    // hour. Skip if the day starts already in daylight (e.g. today's
    // forecast that begins mid-afternoon).
    if (firstDaylightIdx > 0) {
      const prev = d.hours[firstDaylightIdx - 1];
      if (prev?.predicted_soc != null) sunriseSoc = Number(prev.predicted_soc);
    }
    d.sunsetSoc = sunsetSoc;
    d.sunriseSoc = sunriseSoc;
  }

  // For the FIRST tile (Today), the forecast only contains hours from
  // now forward — morning solar that already happened isn't in `fc`.
  // Add today's actual solar_wh so far (from the samples table, surfaced
  // as `today_actual_solar_wh` on the response) so the "Today" label
  // reflects the FULL day's total, not just the remaining hours. Other
  // tiles stay forecast-only.
  if (days.length > 0) {
    const todayActualWh = Number(j?.today_actual_solar_wh ?? 0);
    if (todayActualWh > 0) days[0].solarWh += todayActualWh;
  }

  // Cap to the visible 5-day window. The forecaster returns ~5d so
  // this is usually a no-op; defensive against backend changes.
  const visible = days.slice(0, 5);
  if (!visible.length) {
    strip.innerHTML = '';
    strip.hidden = true;
    return;
  }
  strip.hidden = false;

  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const labelFor = (d) => {
    const dayStart = new Date(d.date);
    dayStart.setHours(0, 0, 0, 0);
    const diffDays = Math.round((dayStart - today) / 86400000);
    if (diffDays === 0) return 'Today';
    if (diffDays === 1) return 'Tomorrow';
    return d.date.toLocaleDateString([], {weekday: 'short', month: 'numeric', day: 'numeric'});
  };
  // Tile color: green when SOC stays >=20 all day, amber when min
  // dips into 10-19, red+⚠ when depleted. minSoc could be Infinity
  // if there were zero predicted_soc values — treat as unknown.
  const classFor = (d) => {
    if (d.depleted) return 'tile-bad';
    if (d.minSoc === Infinity) return '';
    if (d.minSoc < 20) return 'tile-warn';
    return 'tile-good';
  };

  strip.innerHTML = visible.map((d) => {
    const cls = classFor(d);
    const warnLine = d.depleted
      ? '<span class="fdt-warn">⚠ depleted</span>'
      : '';
    // Build the SOC trajectory as labeled points. The today tile
    // (first in strip) extends through midnight to also show
    // tomorrow's sunrise (overnight low), so the user can see both
    // ends of the night in one place. Other tiles show the full
    // daily arc within their own window. Rendered as a label-above-
    // value grid so each point fits compactly without wrapping —
    // an inline "78% → SUNSET 100% → MIDNIGHT 90% → SUNRISE 73%"
    // line was overflowing the 5-up tile width.
    const tileIdx = visible.indexOf(d);
    const isFirst = tileIdx === 0;
    const nextSunriseSoc = isFirst && visible[1]?.sunriseSoc != null
      ? Number(visible[1].sunriseSoc) : null;

    const points = [];
    points.push({ label: isFirst ? 'now' : 'midnight', value: d.startSoc });
    if (d.sunriseSoc != null && Math.abs(d.startSoc - d.sunriseSoc) > 2) {
      points.push({ label: 'sunrise', value: d.sunriseSoc });
    }
    const lastShown = points[points.length - 1].value;
    if (d.sunsetSoc != null
        && Math.abs(d.sunsetSoc - lastShown) > 2
        && Math.abs(d.sunsetSoc - d.endSoc) > 2) {
      points.push({ label: 'sunset', value: d.sunsetSoc });
    }
    points.push({ label: 'midnight', value: d.endSoc });
    if (nextSunriseSoc != null) {
      points.push({ label: 'sunrise', value: nextSunriseSoc });
    }

    const arc = points.map((p) => `
      <div class="fdt-pt">
        <span class="fdt-pt-label">${p.label}</span>
        <span class="fdt-pt-value">${Math.round(p.value)}<small>%</small></span>
      </div>
    `).join('');

    return `<div class="forecast-day-tile ${cls}">
      <span class="fdt-label">${labelFor(d)}</span>
      <div class="fdt-arc">${arc}</div>
      <span class="fdt-solar">${(d.solarWh / 1000).toFixed(1)} kWh ↑</span>
      ${warnLine}
    </div>`;
  }).join('');
}

// ============================================================
// Today's energy budget — narrative breakdown of the forecast.
// Walks from now to the last solar hour, summing solar + load,
// and replaying the simulator's per-hour math (charge_efficiency
// applied only to net-positive hours). load_w in the forecast
// already includes parasitic + inverter overhead.
// ============================================================
function renderTodayEnergyBudget(j) {
  const card = $('today-budget-card');
  if (!card) return;
  const fc = j?.forecast || [];
  const empty = $('today-budget-empty');
  const table = $('today-budget-table');
  const summary = $('today-budget-summary');
  if (!fc.length || !j.ready) {
    card.hidden = false;
    if (empty) empty.hidden = false;
    if (table) table.hidden = true;
    if (summary) summary.textContent = '—';
    return;
  }
  // Find the window from now to the last solar hour. fc[0] is the
  // first hour ahead. Walk to the first non-solar hour after dawn;
  // the index just before is the last daylight hour ("sunset" in
  // the EOD-pill sense).
  let i = 0;
  // Skip any leading non-solar (we're in the dark part of today
  // already — happens at night).
  while (i < fc.length && (fc[i].solar_w || 0) <= 0) i++;
  // Walk through the daylight band.
  const dawnIdx = i;
  while (i < fc.length && (fc[i].solar_w || 0) > 0) i++;
  // dawnIdx..i-1 inclusive is today's daylight band.
  if (i <= dawnIdx) {
    card.hidden = false;
    if (empty) empty.hidden = false;
    if (table) table.hidden = true;
    if (summary) summary.textContent = 'After sunset — open tomorrow';
    return;
  }
  // The "starting SOC" for the walk is whatever the simulator was at
  // before the first hour in our window. fc[dawnIdx-1].predicted_soc
  // if there's a prior hour, else j.starting_soc_pct.
  const socStart = dawnIdx > 0
    ? fc[dawnIdx - 1].predicted_soc
    : j.starting_soc_pct;
  const window = fc.slice(dawnIdx, i);
  const eff = Number(j.charge_efficiency ?? 0.85);
  const cap = Number(j.capacity_wh ?? 30240);
  let solarWh = 0;
  let loadWh = 0;
  let storedWh = 0;
  const rows = window.map((h, idx) => {
    const solar = Number(h.solar_w ?? 0);
    const load = Number(h.load_w ?? 0);
    const net = solar - load;
    const effNet = net > 0 ? net * eff : net;
    solarWh += solar;
    loadWh += load;
    storedWh += effNet;
    const dpp = effNet / cap * 100;
    return {
      h: new Date(h.ts * 1000).toLocaleTimeString([], {hour: 'numeric'}),
      solar: Math.round(solar),
      load: Math.round(load),
      net: Math.round(net),
      dpp,
      soc: h.predicted_soc,
    };
  });
  const socEnd = window[window.length - 1].predicted_soc;

  card.hidden = false;
  if (empty) empty.hidden = true;
  if (table) table.hidden = false;

  // Header summary collapsed-state hint: "16 → 40% · 20.2 kWh in"
  if (summary) {
    summary.textContent =
      `${Math.round(socStart)} → ${Math.round(socEnd)}% · `
      + `${(solarWh / 1000).toFixed(1)} kWh solar`;
  }

  const fmt1 = (n) => (Math.round(n * 10) / 10).toFixed(1);
  const setText = (id, txt) => {
    const el = $(id);
    if (el) el.textContent = txt;
  };
  setText('tb-eff', eff.toFixed(2));
  setText('tb-solar-kwh', fmt1(solarWh / 1000));
  setText('tb-solar-sub', `${rows.length} h, peak ${Math.round(Math.max(...rows.map(r => r.solar)))} W`);
  setText('tb-load-kwh', fmt1(loadWh / 1000));
  setText('tb-load-sub', `avg ${Math.round(loadWh / rows.length)} W`);
  setText('tb-net-kwh', fmt1(storedWh / 1000));
  setText('tb-net-sub',
    `${(socEnd - socStart >= 0 ? '+' : '')}${(socEnd - socStart).toFixed(1)} pp `
    + `(loss: ${fmt1((solarWh - loadWh - storedWh) / 1000)} kWh)`);

  const tbody = $('today-budget-tbody');
  if (tbody) {
    tbody.innerHTML = rows.map((r) => {
      const netCls = r.net > 0 ? 'acc-err-good' : (r.net < 0 ? 'acc-err-bad' : '');
      const dppCls = r.dpp >= 0 ? 'acc-err-good' : 'acc-err-bad';
      const sign = (n) => (n > 0 ? '+' : '') + Math.round(n);
      return `<tr>
        <td>${r.h}</td>
        <td>${r.solar}</td>
        <td>${r.load}</td>
        <td class="${netCls}">${sign(r.net)}</td>
        <td class="${dppCls}">${(r.dpp >= 0 ? '+' : '') + r.dpp.toFixed(2)}</td>
        <td>${r.soc.toFixed(1)}</td>
      </tr>`;
    }).join('');
  }
}

// ============================================================
// Daily sunset/sunrise prediction accuracy table on Forecast tab.
// Data source: /api/daily_summary, written each smart-charge tick.
// ============================================================
async function renderDailyAccuracy() {
  const card = $('daily-accuracy-card');
  if (!card) return;
  const sel = $('daily-accuracy-days');
  const days = parseInt(sel?.value || '14', 10) || 14;
  const sn = activeJackeryDevice()?.device_sn;
  const url = sn
    ? `/api/daily_summary?days=${days}&device_sn=${encodeURIComponent(sn)}`
    : `/api/daily_summary?days=${days}`;
  const r = await fetch(url);
  if (!r.ok) { card.hidden = true; return; }
  const j = await r.json();
  const rows = j.rows || [];
  card.hidden = false;
  const tbody = $('daily-accuracy-tbody');
  const table = $('daily-accuracy-table');
  const empty = $('daily-accuracy-empty');
  if (!rows.length) {
    if (tbody) tbody.innerHTML = '';
    if (table) table.hidden = true;
    if (empty) empty.hidden = false;
    _renderAccuracySummary([], [], [], []);
    return;
  }
  if (empty) empty.hidden = true;
  if (table) table.hidden = false;

  // Render newest-first. Track sunset/sunrise errors in two buckets:
  // ALL (everything visible) and POST-FIX (predictions_made_at >=
  // cutoff_ts — i.e. made by current code, not stale older code).
  // Stale rows still render so the user can see history; they're
  // visually muted and don't count toward the post-fix MAE headline.
  const cutoffTs = Number(j.cutoff_ts || 0);
  const sorted = [...rows].sort((a, b) => (b.date || '').localeCompare(a.date || ''));
  const sunsetErrsAll = [];
  const sunriseErrsAll = [];
  const sunsetErrsPostFix = [];
  const sunriseErrsPostFix = [];
  const html = sorted.map((row) => {
    const date = row.date || '—';
    const sp = row.predicted_sunset_soc_pct;
    const sa = row.actual_sunset_soc_pct;
    const rp = row.predicted_sunrise_soc_pct;
    const ra = row.actual_sunrise_soc_pct;
    const sErr = (sp != null && sa != null) ? sa - sp : null;
    const rErr = (rp != null && ra != null) ? ra - rp : null;
    const madeAt = Number(row.predictions_made_at || 0);
    const isPostFix = cutoffTs > 0 && madeAt > 0 && madeAt >= cutoffTs;
    if (sErr != null) {
      sunsetErrsAll.push(sErr);
      if (isPostFix) sunsetErrsPostFix.push(sErr);
    }
    if (rErr != null) {
      sunriseErrsAll.push(rErr);
      if (isPostFix) sunriseErrsPostFix.push(rErr);
    }
    const rowCls = isPostFix ? '' : 'acc-row-stale';
    const staleBadge = isPostFix ? '' : ' <span class="acc-stale-badge" title="Predictions made by older code (before the latest forecaster fix). Excluded from the post-fix MAE.">stale</span>';
    return `<tr class="${rowCls}">
      <td>${date}${staleBadge}</td>
      <td>${_fmtPct(sp)}</td>
      <td>${_fmtPct(sa)}</td>
      ${_fmtErrCell(sErr)}
      <td>${_fmtPct(rp)}</td>
      <td>${_fmtPct(ra)}</td>
      ${_fmtErrCell(rErr)}
    </tr>`;
  }).join('');
  if (tbody) tbody.innerHTML = html;
  _renderAccuracySummary(
    sunsetErrsAll, sunriseErrsAll,
    sunsetErrsPostFix, sunriseErrsPostFix,
  );
}

function _fmtPct(v) {
  return (v == null) ? '<span class="acc-missing">—</span>' : `${Math.round(v)}%`;
}

function _fmtErrCell(err) {
  if (err == null) return '<td class="acc-missing">—</td>';
  const abs = Math.abs(err);
  const cls = abs <= 3 ? 'acc-err-good' : abs <= 8 ? 'acc-err-warn' : 'acc-err-bad';
  const sign = err > 0 ? '+' : '';
  return `<td class="${cls}">${sign}${err.toFixed(1)}</td>`;
}

function _renderAccuracySummary(sunsetErrsAll, sunriseErrsAll,
                                 sunsetErrsPostFix, sunriseErrsPostFix) {
  const mae = (xs) => xs.length
    ? (xs.reduce((s, x) => s + Math.abs(x), 0) / xs.length).toFixed(1)
    : null;
  const setText = (id, v) => { const el = $(id); if (el) el.textContent = v; };
  // Headline is post-fix when there's any post-fix data; falls back
  // to all-shown when no post-fix samples are available yet (e.g.
  // immediately after a cutoff bump). The subline always shows the
  // alternate count so the user can tell which one they're seeing.
  const hasPostFix = sunsetErrsPostFix.length + sunriseErrsPostFix.length > 0;
  const sunsetErrs = hasPostFix ? sunsetErrsPostFix : sunsetErrsAll;
  const sunriseErrs = hasPostFix ? sunriseErrsPostFix : sunriseErrsAll;
  const scopeNote = hasPostFix ? ' · post-fix' : ' · all (no post-fix yet)';

  const sunsetMae = mae(sunsetErrs);
  const sunriseMae = mae(sunriseErrs);
  setText('daily-acc-sunset-mae', sunsetMae ?? '—');
  setText('daily-acc-sunset-n', sunsetErrs.length
    ? `${sunsetErrs.length} day${sunsetErrs.length === 1 ? '' : 's'} measured${scopeNote}`
    : 'no data');
  setText('daily-acc-sunrise-mae', sunriseMae ?? '—');
  setText('daily-acc-sunrise-n', sunriseErrs.length
    ? `${sunriseErrs.length} day${sunriseErrs.length === 1 ? '' : 's'} measured${scopeNote}`
    : 'no data');
  // Hit rate = share of all measured points (sunset + sunrise) where
  // |error| ≤ 5pp. Single number across both phases gives a quick "is
  // the model basically right" read independent of which phase.
  const all = sunsetErrs.concat(sunriseErrs);
  const hits = all.filter((e) => Math.abs(e) <= 5).length;
  const hitRate = all.length ? Math.round((hits / all.length) * 100) : null;
  setText('daily-acc-hitrate', hitRate ?? '—');
  setText('daily-acc-hitrate-sub', all.length
    ? `${hits} of ${all.length} predictions within 5pp${scopeNote}`
    : 'no data');
}

// Re-render when the user picks a different window. No new server data
// per change — the API call is small and re-running keeps the summary
// in sync with whatever range is active.
$('daily-accuracy-days')?.addEventListener('change', () => {
  renderDailyAccuracy().catch((err) =>
    console.warn('daily accuracy render failed', err));
});

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

// Render the "Forecasting for: <city> (lat, lon)" line that sits under
// the Forecast tab title. Reads from /api/location each time so it
// reflects whatever was last persisted (manual override or geolocation).
// Older saves won't have a `label` — we degrade gracefully to coords +
// timezone in that case.
async function refreshForecastCurrentLoc() {
  const el = $('forecast-current-loc');
  if (!el) return;
  try {
    const r = await fetch('/api/location');
    if (!r.ok) { el.textContent = ''; return; }
    const j = await r.json();
    if (j == null || j.latitude == null || j.longitude == null) {
      el.textContent = 'No location set — forecasts unavailable.';
      return;
    }
    const lat = Number(j.latitude).toFixed(4);
    const lon = Number(j.longitude).toFixed(4);
    // Build the inner HTML safely — textContent on each span avoids any
    // injection risk from a maliciously-saved label, while still letting
    // us style "name" vs "coords" differently.
    el.innerHTML = '';
    const prefix = document.createTextNode('Forecasting for: ');
    el.appendChild(prefix);
    if (j.label) {
      const name = document.createElement('span');
      name.className = 'loc-name';
      name.textContent = j.label;
      el.appendChild(name);
      el.appendChild(document.createTextNode(' — '));
    }
    const coords = document.createElement('span');
    coords.className = 'loc-coords';
    coords.textContent = `${lat}, ${lon}`;
    el.appendChild(coords);
    if (j.timezone) {
      el.appendChild(document.createTextNode(` (${j.timezone})`));
    }
  } catch {
    // Silent fail — the line is informational, not blocking.
    el.textContent = '';
  }
}

// ---- Manual location override --------------------------------------------
// Browser geolocation can give bad coords (IP fallback to ISP POP, stale
// Wi-Fi BSSID database entries, VPN, etc). The manual card lets the user
// either type a city name (server geocodes via Open-Meteo) or paste raw
// lat/lon. Both paths POST to /api/location which validates, persists,
// and busts the weather cache so the next forecast pulls fresh.

function _showManualLoc(open) {
  const card = $('forecast-manual-loc');
  if (!card) return;
  card.hidden = !open;
  if (open) {
    // Refresh the "currently using" line each time the card opens.
    fetch('/api/location').then(r => r.json()).then(j => {
      const cur = $('forecast-manual-current');
      if (!cur) return;
      if (j && j.latitude != null && j.longitude != null) {
        cur.textContent = `Currently using ${j.latitude.toFixed(4)}, ${j.longitude.toFixed(4)}` +
                          (j.timezone ? ` (${j.timezone})` : '');
      } else {
        cur.textContent = 'No location saved yet.';
      }
    }).catch(() => {});
    // Focus the city search box for quick typing (only if search mode
    // is the active pane).
    const q = $('manual-loc-q');
    const searchPane = $('manual-loc-search');
    if (q && searchPane && !searchPane.hidden) q.focus();
  }
}

// "Set manually" / "Change location" buttons — open the override card.
document.addEventListener('click', (e) => {
  const id = e.target?.id;
  if (id === 'forecast-manual-btn' || id === 'forecast-change-loc') {
    _showManualLoc(true);
  } else if (id === 'forecast-manual-close') {
    _showManualLoc(false);
  }
});

// Mode radio (search vs coords).
document.addEventListener('change', (e) => {
  if (e.target?.name !== 'manual-loc-mode') return;
  const mode = e.target.value;
  const search = $('manual-loc-search');
  const coords = $('manual-loc-coords');
  if (search) search.hidden = mode !== 'search';
  if (coords) coords.hidden = mode !== 'coords';
});

// Debounced city search. Open-Meteo's geocoding is fast (<200ms), but
// we still want to coalesce keystrokes so we don't fire on every letter.
let _manualLocSearchTimer = null;
let _manualLocLastQ = '';
document.addEventListener('input', (e) => {
  if (e.target?.id !== 'manual-loc-q') return;
  const q = e.target.value.trim();
  if (_manualLocSearchTimer) clearTimeout(_manualLocSearchTimer);
  if (q.length < 2) {
    // Clear results and the hint when the field is effectively empty —
    // a stale 1-char result list is worse than nothing.
    const ul = $('manual-loc-results');
    if (ul) ul.innerHTML = '';
    const hint = $('manual-loc-search-hint');
    if (hint) { hint.textContent = ''; hint.dataset.kind = ''; }
    _manualLocLastQ = '';
    return;
  }
  _manualLocSearchTimer = setTimeout(() => _runManualLocSearch(q), 350);
});

async function _runManualLocSearch(q) {
  if (q === _manualLocLastQ) return;
  _manualLocLastQ = q;
  const ul = $('manual-loc-results');
  const hint = $('manual-loc-search-hint');
  if (!ul) return;
  if (hint) { hint.textContent = 'Searching…'; hint.dataset.kind = ''; }
  try {
    const r = await fetch('/api/location/geocode?q=' + encodeURIComponent(q) + '&count=8');
    const j = await r.json();
    // Race-guard: if the user kept typing while this request was in
    // flight, drop the stale response.
    if (q !== _manualLocLastQ) return;
    ul.innerHTML = '';
    const results = (j && j.results) || [];
    if (!results.length) {
      if (hint) { hint.textContent = 'No matches. Try a different spelling, or switch to coordinates.'; hint.dataset.kind = ''; }
      return;
    }
    if (hint) { hint.textContent = `${results.length} match${results.length === 1 ? '' : 'es'} — click to use.`; hint.dataset.kind = ''; }
    for (const item of results) {
      const li = document.createElement('li');
      const meta = [item.admin1, item.country].filter(Boolean).join(', ');
      li.innerHTML = `<span class="loc-name"></span><span class="loc-meta"></span>`;
      li.querySelector('.loc-name').textContent = item.name || '(unnamed)';
      li.querySelector('.loc-meta').textContent =
        (meta ? meta + ' — ' : '') +
        `${item.latitude.toFixed(4)}, ${item.longitude.toFixed(4)}` +
        (item.timezone ? ` · ${item.timezone}` : '');
      // Build a richer label from the geocoder fields so the Forecast
      // tab shows "San Jose, California, US" instead of bare "San Jose".
      const richLabel = [item.name, item.admin1, item.country]
        .filter(Boolean).join(', ');
      li.addEventListener('click', () => _saveManualLocation(item.latitude, item.longitude, richLabel));
      ul.appendChild(li);
    }
  } catch (err) {
    if (hint) { hint.textContent = 'Search failed: ' + (err.message || err); hint.dataset.kind = 'err'; }
  }
}

// Coords-mode save button.
document.addEventListener('click', async (e) => {
  if (e.target?.id !== 'manual-loc-save-coords') return;
  const lat = parseFloat($('manual-loc-lat')?.value);
  const lon = parseFloat($('manual-loc-lon')?.value);
  const hint = $('manual-loc-coords-hint');
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
    if (hint) { hint.textContent = 'Enter both latitude and longitude.'; hint.dataset.kind = 'err'; }
    return;
  }
  if (lat < -90 || lat > 90 || lon < -180 || lon > 180) {
    if (hint) { hint.textContent = 'Coordinates out of range.'; hint.dataset.kind = 'err'; }
    return;
  }
  await _saveManualLocation(lat, lon, null, { hintEl: hint });
});

// Common save path used by both modes. POSTs to /api/location, refreshes
// the forecast on success, hides the card.
async function _saveManualLocation(lat, lon, label, opts) {
  opts = opts || {};
  const hintEl = opts.hintEl || $('manual-loc-search-hint');
  if (hintEl) { hintEl.textContent = 'Saving…'; hintEl.dataset.kind = ''; }
  // Forget any earlier "denied geolocation" stickiness — user has
  // explicitly chosen a location now, the boot prompt should stay quiet.
  localStorage.setItem('jackery-location-denied', '1');
  try {
    const r = await fetch('/api/location', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      // Forward the optional label so the server can persist it for
      // the "Forecasting for: <city>" line. Coords-only saves leave
      // it null and the UI falls back to displaying just coords.
      body: JSON.stringify({ latitude: lat, longitude: lon, label: label || null }),
    });
    let j = {};
    try { j = await r.json(); } catch (_) {}
    if (!r.ok) {
      const msg = j.error || j.detail || `HTTP ${r.status}`;
      if (hintEl) { hintEl.textContent = 'Failed: ' + msg; hintEl.dataset.kind = 'err'; }
      return;
    }
    if (hintEl) {
      hintEl.textContent = label
        ? `Saved: ${label} (${lat.toFixed(4)}, ${lon.toFixed(4)})`
        : `Saved: ${lat.toFixed(4)}, ${lon.toFixed(4)}`;
      hintEl.dataset.kind = 'ok';
    }
    // Hide the manual card and refresh dependent UI — the server has
    // already busted the weather cache, so the next forecast call will
    // pull fresh GHI for the new coords.
    setTimeout(() => _showManualLoc(false), 800);
    fetchForecast();
    fetchEodForecast();
    // Update the "Forecasting for: …" line immediately so the user sees
    // the new place name before fetchForecast() finishes its round-trip.
    refreshForecastCurrentLoc();
  } catch (err) {
    if (hintEl) { hintEl.textContent = 'Save failed: ' + (err.message || err); hintEl.dataset.kind = 'err'; }
  }
}

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

  // Helper: surface the "still calibrating" state with progress hints
  // instead of silently hiding. The forecast tab has the same info; this
  // saves the user a tab switch to find out why the hero number is
  // missing for a freshly-added device.
  const showCalibrating = (readiness) => {
    const r = readiness || {};
    const haveH = Math.round(r.have_hours ?? 0);
    const needH = r.needed_hours ?? 24;
    const haveW = r.have_idle_windows ?? 0;
    const needW = r.needed_idle_windows ?? 5;
    el.classList.remove('eod-pair');  // drop the two-column layout if it was on
    const labelEl = el.querySelector('.eod-label');
    if (labelEl) labelEl.textContent = 'Calibrating';
    const pct = $('eod-pct');
    const trend = $('eod-trend');
    if (trend) { trend.hidden = false; trend.textContent = ''; trend.classList.remove('up', 'down'); }
    // Reuse #eod-pct as the message slot — keeps the layout stable.
    if (pct) pct.textContent = `${haveH}/${needH} h · ${haveW}/${needW} cycles`;
    el.classList.remove('low');
    el.classList.add('calibrating');
    el.title = 'Forecaster needs more history before it can predict this device. '
      + `Need ${needH}h of samples (${haveH}h captured) and ${needW} clean discharge windows (${haveW} so far).`;
    el.hidden = false;
  };

  try {
    // Forecast is per-device — pass the currently-viewed Jackery's SN so
    // the hero card's prediction follows the picker (each browser may be
    // viewing a different device).
    const sn = activeJackeryDevice()?.device_sn;
    const url = sn ? `/api/forecast?device_sn=${encodeURIComponent(sn)}` : '/api/forecast';
    const r = await fetch(url);
    if (!r.ok) { el.hidden = true; return; }
    const j = await r.json();
    if (!j.configured || j.error) { el.hidden = true; return; }
    // Readiness gate failed — show progress instead of hiding.
    if (j.ready === false) { showCalibrating(j.readiness); return; }
    if (!Array.isArray(j.forecast) || !j.forecast.length) {
      el.hidden = true;
      return;
    }
    // Ready path — clear any prior calibrating styling.
    el.classList.remove('calibrating');
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
    const pct = $('eod-pct');
    const trend = $('eod-trend');
    const start = j.starting_soc_pct ?? best.predicted_soc;
    _eodAnchorSOC = start;
    const sunsetVal = Math.round(best.predicted_soc);

    // During daytime, also surface the PEAK SOC over today's remaining
    // daylight. With afternoon shade the battery can crest mid-afternoon
    // then drain through the shaded evening (solar < house load), so the
    // last-daylight-hour value ("sunset") alone reads flat/low and hides
    // the high-water mark. Show "Peak X% · sunset Y%" when they diverge;
    // fall back to the single sunset value when they don't (no shade, or
    // SOC still rising into dusk — then peak ≈ sunset).
    let peakVal = sunsetVal;
    if (isDayNow) {
      let pk = -Infinity;
      for (let d = 0; d < i; d++) {
        if (fc[d].predicted_soc != null) pk = Math.max(pk, fc[d].predicted_soc);
      }
      if (isFinite(pk)) peakVal = Math.round(pk);
    }
    // Only frame as "Peak · sunset" when the battery will actually climb
    // ABOVE where it is now (a real peak still ahead) AND then decline to
    // a lower sunset. In the evening the day's real peak is already past
    // and only the shaded-evening decline remains, so the max over
    // remaining daylight sits at/below current SOC — showing "Peak 66%"
    // when you're at 68% is nonsensical. There, fall back to the single
    // "At sunset" (declining) value.
    const showBoth = isDayNow
      && peakVal > Math.round(start)
      && peakVal > sunsetVal + 1;

    const trailingPct = el.querySelector('.eod-row > small');
    const delta = best.predicted_soc - start;
    if (showBoth) {
      // Two-column aligned layout: the label and the value row both
      // become full-width 3-col grids (value · value) via the `eod-pair`
      // class, so "Peak" sits over the peak number and "sunset" over the
      // sunset number with the dots lined up. The trend arrow is dropped
      // here — it would offset column 1 and the peak·sunset already shows
      // the day's direction. Each number carries its own <small>%</small>
      // so the percent signs match (.eod-row small is a descendant rule).
      el.classList.add('eod-pair');
      if (trailingPct) trailingPct.hidden = true;
      if (trend) trend.hidden = true;
      if (labelEl) labelEl.innerHTML =
        '<span>Peak</span><span class="eod-dot">·</span><span>sunset</span>';
      pct.innerHTML =
        `<span>${peakVal}<small>%</small></span>`
        + '<span class="eod-dot">·</span>'
        + `<span>${sunsetVal}<small>%</small></span>`;
      el.title = `Battery peaks ~${peakVal}% today, then the shaded evening `
        + `(solar below house load) drains it to ~${sunsetVal}% by sunset. `
        + `Refits every 30 min and on live SOC drift.`;
    } else {
      // Single value (no afternoon decline, or nighttime "At sunrise").
      // The fixed trailing <small>%</small> completes the number.
      el.classList.remove('eod-pair');
      if (trailingPct) trailingPct.hidden = false;
      if (labelEl) labelEl.textContent = label;
      pct.textContent = `${sunsetVal}`;
      el.title = 'Predicted state of charge at the next sun-phase boundary '
        + '(sunset during the day, sunrise at night). Refits every 30 min '
        + 'and on live SOC drift.';
      // Trend arrow reflects the NET direction (target vs now).
      if (trend) {
        trend.hidden = false;
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
      }
    }
    // "Low" warning keys off the true end-of-day (sunset) value — that's
    // the trough that matters for "will I have enough overnight".
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

function packRow({ idx, soc, flow, flowClass, temp, label, snTitle, isMain,
                   needUpgrade }) {
  const cls = isMain ? 'pack-row pack-row-main' : 'pack-row';
  const socTxt = soc != null ? Math.round(soc) : '—';
  // Firmware-update-available badge (per-pack `needUpgrade` from the
  // cloud). Yellow accent + Space Mono to match the deck style; inline
  // so it needs no stylesheet change.
  const fwBadge = needUpgrade
    ? ' <span class="pack-fw" title="Firmware update available in the Jackery app"'
      + ' style="color:#EAF044;font:600 9px/1 \'Space Mono\',monospace;'
      + 'letter-spacing:.5px;border:1px solid #EAF044;border-radius:3px;'
      + 'padding:1px 3px;vertical-align:middle">UPD</span>'
    : '';
  return `
    <div class="${cls}">
      <span class="pack-idx">${idx}</span>
      <span class="pack-bar"><span class="pack-bar-fill" style="width:${Math.max(0, Math.min(100, soc || 0))}%"></span></span>
      <span class="pack-soc">${socTxt}<small>%</small></span>
      <span class="pack-flow ${flowClass}">${flow}</span>
      <span class="pack-temp">${temp}</span>
      <span class="pack-sn" title="${snTitle}">${label}${fwBadge}</span>
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
  const upgrades = packs.filter(p => p.needUpgrade).length;
  const upgTxt = upgrades ? ` · ⬆ ${upgrades} update${upgrades > 1 ? 's' : ''}` : '';
  summary.textContent = `${packs.length} packs · avg ${Math.round(avgPack)}%${sysTxt} · ${totalIn}W in${upgTxt}`;

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
      needUpgrade: !!p.needUpgrade,
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

// Generic collapse: any card with `data-collapse-key` works the same
// way as battery-packs (default-collapsed + sticky per-key state via
// localStorage). Used by the Forecast tab's "Today's energy budget"
// and "Sunset/sunrise accuracy" cards. Distinct from
// `data-settings-collapse-key` (settings-tab-only with extra search
// indexing) so adding one of these doesn't pollute settings search.
function _collapseStorageKey(key) { return `jackery-collapse-${key}`; }
function _applyCollapseFor(card) {
  const key = card.dataset.collapseKey;
  if (!key) return;
  const saved = localStorage.getItem(_collapseStorageKey(key));
  // Default state is whatever the HTML declared (typically `collapsed`
  // class set in markup). User pref overrides if set.
  if (saved === '1') card.classList.add('collapsed');
  else if (saved === '0') card.classList.remove('collapsed');
  card.querySelector('.collapsible-header')?.setAttribute(
    'aria-expanded', String(!card.classList.contains('collapsed')),
  );
}
function _toggleCollapseFor(card) {
  const key = card.dataset.collapseKey;
  if (!key) return;
  const nowCollapsed = !card.classList.contains('collapsed');
  card.classList.toggle('collapsed', nowCollapsed);
  card.querySelector('.collapsible-header')?.setAttribute(
    'aria-expanded', String(!nowCollapsed),
  );
  localStorage.setItem(_collapseStorageKey(key), nowCollapsed ? '1' : '0');
}
document.querySelectorAll('[data-collapse-key]').forEach((card) => {
  _applyCollapseFor(card);
  const hdr = card.querySelector('.collapsible-header');
  if (!hdr) return;
  hdr.addEventListener('click', (e) => {
    // Don't hijack clicks on form controls inside the header (e.g.
    // the days-window dropdown on the accuracy card).
    if (e.target.closest('input, select, button, a, textarea')) return;
    _toggleCollapseFor(card);
  });
  hdr.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      _toggleCollapseFor(card);
    }
  });
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
                  `not a real temperature reading (display-only; not used).`;
    // Show the actual reading (amber + dotted underline = untrusted) so
    // the value is visible at a glance — the point is to eyeball whether
    // firmware fixed pack temp reporting. Not hidden behind a "⚠?".
    return `<span class="pack-temp-fault" title="${title}">${formatTemp(c)}</span>`;
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
  // Build the query string properly. The previous code did
  // `${params}&status=pending`.replace(/^&/, '?') — that only fixes
  // the leading-`&` case if `params` is THE WHOLE URL, not when
  // `params` is empty mid-template. When deviceSn was missing (fresh
  // page load before WS populates lastStatus), the URL came out as
  // `/api/algorithm/suggestions&status=pending` and 404'd. URLSearch-
  // Params handles the joining cleanly.
  const qs = new URLSearchParams({status: 'pending'});
  if (deviceSn) qs.set('device_sn', deviceSn);
  try {
    const r = await fetch(`/api/algorithm/suggestions?${qs}`);
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
      'Click "Run review now" to ask the AI advisor for a fresh analysis.</div>';
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

// Provider-aware label for advisor status lines ("Claude (claude-opus-4-7)"
// or "ChatGPT (gpt-5.6-sol)"), refreshed from prefs before each run so a
// Settings change shows correctly without a reload.
let _aiAdvisorLabel = 'AI advisor';
async function refreshAIAdvisorLabel() {
  try {
    const r = await fetch('/api/anthropic/prefs');
    const j = r.ok ? await r.json() : {};
    const isOpenAI = j.provider === 'openai';
    const model = isOpenAI ? j.openai_advisor_model : j.advisor_model;
    const brand = isOpenAI ? 'ChatGPT' : 'Claude';
    _aiAdvisorLabel = model ? `${brand} (${model})` : brand;
  } catch { /* keep previous label */ }
  return _aiAdvisorLabel;
}

document.getElementById('alg-review-now')?.addEventListener('click', async () => {
  const status = $('alg-status');
  const summaryEl = $('alg-summary');
  const btn = $('alg-review-now');
  status.hidden = false;
  status.textContent = 'Starting AI review (60-180s)…';
  refreshAIAdvisorLabel().then((label) => {
    // Only upgrade the text if we're still in the starting phase.
    if (status.textContent.startsWith('Starting AI review')) {
      status.textContent = `Starting ${label} review (60-180s)…`;
    }
  });
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
        status.textContent = `${_aiAdvisorLabel} is reviewing… ${elapsed}`;
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
        const modelTag = result.model ? ` · ${result.model}` : '';
        status.textContent = `Review complete: ${newCount} new ` +
          `(${result.turns || 0} turns, ${result.tool_calls || 0} DB queries` +
          `${modelTag}).`;
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
    if (r.ok && j.status === 'running') {
      refreshAIAdvisorLabel();  // async; polling text picks it up next tick
      pollAdvisorJob(dev.device_sn);
    }
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
    const brand = j.provider === 'openai' ? 'ChatGPT' : 'Claude';
    const effortLine = j.provider === 'openai'
      ? `reasoning effort: <code>${safe(j.reasoning_effort || '—')}</code>`
      : `thinking effort: <code>${safe(j.reasoning_effort || '—')}</code>`;
    wrap.innerHTML = `
      <h3 style="margin:0 0 4px;font-size:14px">Context sent to ${brand}</h3>
      <p class="hint" style="margin:0 0 8px">
        Model: <code>${safe(j.model)}</code> · ${effortLine}.
        This is the exact text the advisor reads as the user message —
        minus the system prompt and tool schema.
      </p>
      <pre class="dbg-pre" style="max-height:520px">${safe(j.rendered)}</pre>`;
  } catch (e) {
    wrap.innerHTML = `<div class="hint">Failed: ${escapeHtml(e.message || e)}</div>`;
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
    wrap.innerHTML = `<div class="hint">Failed: ${escapeHtml(e.message || e)}</div>`;
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
        : '✅ <strong>API key saved.</strong> Advisor + AI narration are unlocked when Anthropic is the active provider.';
    } else {
      line.innerHTML = 'No key saved. Enter one below to use Anthropic as the active provider.';
    }
    // Populate the collapsed-card chip so the user can see at a glance
    // whether they're configured. The advisor model name (when known)
    // makes this richer — see _refreshAnthropicChip below, called
    // again by loadAnthropicModelPickers once the model list resolves.
    if (status) {
      status.hidden = false;
      status.textContent = j.has_key ? 'connected' : 'not configured';
    }
  } catch (e) {
    console.warn('anthropic key status fetch failed', e);
  }
  // Also re-evaluate the smart-charge claude toggle disabled state, in
  // case the user saved a key while sitting on Settings then jumped to
  // Automation without a full reload.
  applyClaudeToggleGate();
}

function applyAnthropicGates() {
  // Anything that depends on a saved + verified key for the ACTIVE AI
  // provider gets gated here. Re-runnable on every render or status
  // change. window._aiActiveHasKey (set by loadAIProvider) reflects the
  // active provider's key; fall back to the Anthropic flag before the
  // provider status has loaded.
  const hasKey = (window._aiActiveHasKey ?? false) || _anthropicHasKey;
  const reason = 'Save an API key for the active AI provider on the Settings page first.';

  const t = $('sc-claude-toggle');
  if (t) {
    if (hasKey) {
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
    if (hasKey) {
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
    // loadAnthropicKeyStatus() resets the chip to 'connected' / 'not
    // configured'; no timeout-hide needed any more — the chip is
    // always-on now (it's the collapsed-card status indicator).
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
  if (!confirm('Forget the saved Anthropic API key? The advisor + AI narration stop working if Anthropic is the active provider.')) return;
  status.hidden = false;
  status.textContent = 'Clearing…';
  try {
    const r = await fetch('/api/anthropic/key', { method: 'DELETE' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    status.textContent = 'Forgotten.';
    loadAnthropicKeyStatus();  // resets chip to 'not configured'
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
// AI PROVIDER SELECTOR + OPENAI KEY/MODEL PICKERS
// Mirrors the Anthropic block. Provider switch → /api/ai/provider;
// key → /api/openai/{key,models}; model prefs reuse saveAnthropicPref
// (the /api/anthropic/prefs endpoint also accepts openai_* fields).
// ============================================================
async function loadAIProvider() {
  const sel = $('ai-provider-select');
  const status = $('ai-provider-status');
  if (!sel) return;
  try {
    const r = await fetch('/api/ai/provider');
    const j = r.ok ? await r.json() : {};
    if (j.provider) sel.value = j.provider;
    if (status) status.textContent = j.provider ? `active: ${j.provider}` : '';
    // Gate the narration toggle + advisor hour on the ACTIVE provider's key.
    window._aiActiveHasKey = j.provider === 'openai'
      ? !!j.openai_has_key : !!j.anthropic_has_key;
    applyAnthropicGates();
  } catch (e) { console.warn('ai provider fetch failed', e); }
}

document.getElementById('ai-provider-select')?.addEventListener('change', async (e) => {
  const status = $('ai-provider-status');
  if (status) status.textContent = 'Switching…';
  try {
    const r = await fetch('/api/ai/provider', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ provider: e.target.value }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
    if (status) status.textContent = `active: ${j.provider}`;
    loadAIProvider();  // refresh the active-provider key flag + re-gate
  } catch (err) {
    if (status) status.textContent = `switch failed: ${err.message || err}`;
  }
});

async function loadOpenAIKeyStatus() {
  const line = $('oa-state-line');
  const status = $('oa-status');
  if (!line) return;
  try {
    const r = await fetch('/api/openai/key');
    const j = r.ok ? await r.json() : { has_key: false };
    if (j.has_key) {
      line.innerHTML = j.source === 'env'
        ? '✅ <strong>Configured via env var</strong> on the bridge — UI changes here only affect the saved-on-disk key.'
        : '✅ <strong>API key saved.</strong>';
    } else {
      line.innerHTML = 'No key saved. Enter one below to use OpenAI as the active provider.';
    }
    if (status) { status.hidden = false; status.textContent = j.has_key ? 'connected' : 'not configured'; }
  } catch (e) { console.warn('openai key status fetch failed', e); }
}

document.getElementById('oa-form')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  const status = $('oa-status');
  const input = $('oa-input');
  const key = input.value.trim();
  if (!key) return;
  status.hidden = false;
  status.textContent = 'Validating with OpenAI…';
  try {
    const r = await fetch('/api/openai/key', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ api_key: key }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || j.error || `HTTP ${r.status}`);
    status.textContent = 'Saved. Test call succeeded.';
    input.value = '';
    loadOpenAIKeyStatus();
    loadOpenAIModelPickers();
  } catch (err) { status.textContent = String(err.message || err); }
});

document.getElementById('oa-clear')?.addEventListener('click', async () => {
  const status = $('oa-status');
  if (!confirm('Forget the saved OpenAI API key?')) return;
  status.hidden = false;
  status.textContent = 'Clearing…';
  try {
    const r = await fetch('/api/openai/key', { method: 'DELETE' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    status.textContent = 'Forgotten.';
    loadOpenAIKeyStatus();
  } catch (err) { status.textContent = String(err.message || err); }
});

async function loadOpenAIModelPickers() {
  const advisorSel = $('oa-advisor-model');
  const narratorSel = $('oa-narrator-model');
  const effortSel = $('oa-advisor-effort');
  const statusEl = $('oa-models-status');
  if (!advisorSel || !narratorSel || !effortSel) return;
  if (statusEl) statusEl.textContent = 'Loading model list…';
  try {
    const [modelsR, prefsR] = await Promise.all([
      fetch('/api/openai/models'),
      fetch('/api/anthropic/prefs'),
    ]);
    const modelsJ = modelsR.ok ? await modelsR.json() : { models: [], source: 'fetch_failed' };
    const prefsJ = prefsR.ok ? await prefsR.json() : {};
    const models = modelsJ.models || [];
    const advisorCurrent = prefsJ.openai_advisor_model || '';
    const narratorCurrent = prefsJ.openai_narrator_model || '';
    const effort = prefsJ.openai_advisor_effort || 'high';
    const populate = (sel, current) => {
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
      for (const m of models) addOpt(m.id, m.display_name);
      if (current && !seen.has(current)) addOpt(current, `${current} (saved)`);
      sel.disabled = false;
    };
    populate(advisorSel, advisorCurrent);
    populate(narratorSel, narratorCurrent);
    for (const opt of effortSel.options) opt.selected = opt.value === effort;
    if (statusEl) {
      const sourceLabel = ({
        live: '✅ Live list from OpenAI.',
        cache: '✅ Live list (cached).',
        fallback_no_key: '⚠ No API key saved — showing well-known models as a fallback.',
        fallback_no_sdk: '⚠ OpenAI SDK not in this image — showing fallback list.',
        fallback_fetch_failed: `⚠ Couldn't reach OpenAI to refresh — showing fallback. ${modelsJ.error || ''}`,
        fallback_empty: '⚠ OpenAI returned an empty list — showing fallback.',
      })[modelsJ.source] || `Source: ${modelsJ.source || 'unknown'}.`;
      statusEl.textContent = sourceLabel;
    }
  } catch (e) {
    if (statusEl) statusEl.textContent = `Failed to load model list: ${e.message || e}`;
  }
}

document.getElementById('oa-advisor-model')?.addEventListener('change', (e) => {
  saveAnthropicPref({ openai_advisor_model: e.target.value }, 'OpenAI advisor model');
});
document.getElementById('oa-narrator-model')?.addEventListener('change', (e) => {
  saveAnthropicPref({ openai_narrator_model: e.target.value }, 'OpenAI narrator model');
});
document.getElementById('oa-advisor-effort')?.addEventListener('change', (e) => {
  saveAnthropicPref({ openai_advisor_effort: e.target.value }, 'OpenAI reasoning effort');
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

// ===========================================================================
// Backup & restore — Settings tab.
// Three logical groupings: (1) destination credentials + connectivity test,
// (2) status / run history / run-now, (3) snapshot list + restore picker.
// All UI state is derived from /api/backup/*. Polling is event-driven (only
// while the Settings tab is visible) — see loadBackupAll() above.
// ===========================================================================

async function loadBackupAll() {
  // Fan-out the three independent loads. Status & creds are cheap (local
  // file reads); snapshot listing requires mounting the remote so we leave
  // it lazy until the user clicks "List snapshots".
  await loadBackupCreds();
  loadBackupStatus();
  loadBackupSchedule();
  // After creds load we know whether to auto-discover. loadBackupCreds
  // sets _backupHasCreds; if false AND we're on SMB transport we sweep
  // the LAN once for SMB hosts (cached for the rest of the session).
  // rsync transports don't get LAN discovery — port-445 SMB scanning is
  // SMB-specific and would mislead a user who's set Transport to rsync.
  if (!_backupHasCreds && _backupTransport() === 'smb') {
    runBackupDiscovery({ force: false });
  } else {
    const panel = $('backup-discover-panel');
    if (panel) panel.hidden = true;
  }
}

// Cached state for discovery so re-entering the Settings tab doesn't
// trigger another 5-second LAN sweep. Manual "Rescan" bypasses this.
let _backupHasCreds = false;
let _backupDiscoveryRan = false;

// Required fields per transport. Mirrors the same map on the server
// side. Used to decide when the auto-test has enough input to fire and
// to validate Save before sending the body.
const _BACKUP_REQUIRED_FIELDS = {
  smb: ['host', 'share', 'username', 'password'],
  rsync_ssh: ['host', 'ssh_user', 'ssh_key', 'target_dir'],
  rsyncd: ['host', 'rsync_module', 'rsyncd_user', 'rsyncd_password'],
  rsyncd_ssh: ['host', 'ssh_user', 'ssh_password', 'rsync_module'],
};

// Every input/textarea/select that contributes to the creds body. Used
// by the Forget button to wipe state and by the auto-test wiring to
// listen for input changes regardless of which transport is active.
const _ALL_BACKUP_INPUT_IDS = [
  'backup-transport',
  'backup-host',
  // SMB
  'backup-share', 'backup-subdir', 'backup-username',
  'backup-password', 'backup-domain',
  // rsync over SSH (key auth, filesystem path)
  'backup-ssh-user', 'backup-target-dir', 'backup-ssh-key',
  // rsyncd (port 873) + rsyncd over SSH (port 22) share these
  'backup-rsync-module', 'backup-target-subpath',
  // rsyncd-only
  'backup-rsyncd-user', 'backup-rsyncd-password',
  // rsyncd-over-SSH-only
  'backup-ssh-port', 'backup-ssh-password',
];

function _backupTransport() {
  return ($('backup-transport')?.value || 'smb');
}

// Show only the rows tagged for the current transport (or the universal
// rows tagged data-transport-row="all"). Also hides the SMB-only LAN
// discovery panel for non-SMB transports.
function _applyBackupTransportVisibility() {
  const t = _backupTransport();
  document.querySelectorAll('[data-transport-row]').forEach(row => {
    const allowed = row.dataset.transportRow.split(',').map(s => s.trim());
    row.hidden = !(allowed.includes('all') || allowed.includes(t));
  });
  const panel = $('backup-discover-panel');
  if (panel && t !== 'smb') panel.hidden = true;
}

// Module discovery for rsyncd / rsyncd_ssh transports. Hits the
// /api/backup/list-rsync-modules endpoint with the current form state
// and populates the datalist + writes a hint line. Mirrors what
// Synology Hyper Backup does — the user enters host + creds, hits
// "Discover modules", and gets the available module names.
$('backup-rsync-module-discover')?.addEventListener('click', async () => {
  const btn = $('backup-rsync-module-discover');
  const hint = $('backup-rsync-module-hint');
  const dl = $('backup-rsync-module-options');
  const moduleInput = $('backup-rsync-module');
  if (!btn) return;
  const t = _backupTransport();
  if (t !== 'rsyncd' && t !== 'rsyncd_ssh') return;
  const f = _readBackupForm();
  if (!f.host) {
    if (hint) { hint.textContent = 'Enter host first.'; hint.dataset.kind = ''; }
    return;
  }
  const body = { transport: t, host: f.host };
  if (t === 'rsyncd_ssh') {
    if (!f.ssh_user || !f.ssh_password) {
      if (hint) { hint.textContent = 'Enter SSH user + password first.'; hint.dataset.kind = ''; }
      return;
    }
    Object.assign(body, {
      ssh_port: f.ssh_port || 22,
      ssh_user: f.ssh_user,
      ssh_password: f.ssh_password,
    });
  } else {
    if (!f.rsyncd_user || !f.rsyncd_password) {
      if (hint) { hint.textContent = 'Enter rsyncd user + password first.'; hint.dataset.kind = ''; }
      return;
    }
    Object.assign(body, {
      rsyncd_user: f.rsyncd_user,
      rsyncd_password: f.rsyncd_password,
    });
  }
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = 'Discovering…';
  if (hint) { hint.textContent = ''; hint.dataset.kind = ''; }
  try {
    const r = await fetch('/api/backup/list-rsync-modules', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    });
    let j = {};
    try { j = await r.json(); } catch (_) {}
    if (!r.ok || !j.ok) {
      if (hint) {
        hint.textContent = 'Failed: ' + (j.error || j.detail || ('HTTP ' + r.status));
        hint.dataset.kind = 'err';
      }
      return;
    }
    const modules = j.modules || [];
    if (dl) {
      dl.innerHTML = '';
      for (const m of modules) {
        const o = document.createElement('option');
        o.value = m;
        dl.appendChild(o);
      }
    }
    if (hint) {
      if (modules.length === 0) {
        hint.textContent = 'No modules exposed for this account.';
        hint.dataset.kind = '';
      } else if (modules.length === 1 && moduleInput && !moduleInput.value) {
        // Only one module + the field is empty → auto-fill it.
        moduleInput.value = modules[0];
        hint.textContent = `Found 1 module — auto-filled.`;
        hint.dataset.kind = 'ok';
      } else {
        hint.textContent = `${modules.length} module${modules.length === 1 ? '' : 's'} available — click the field to pick.`;
        hint.dataset.kind = 'ok';
      }
    }
  } catch (err) {
    if (hint) {
      hint.textContent = 'Failed: ' + (err.message || err);
      hint.dataset.kind = 'err';
    }
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
});

$('backup-transport')?.addEventListener('change', () => {
  _applyBackupTransportVisibility();
  // Changing transport invalidates anything we cached about the form
  // (share lookup, last-tested signature) — let the auto-test wiring
  // pick up fresh on the next keystroke.
});

// Translate machine-readable error tokens from the backup module into
// something a normal human can act on. Anything not in this map gets
// shown verbatim (it's already a real error string from mount.cifs or
// smbclient).
function _backupHumaniseError(s) {
  if (!s) return 'unknown error';
  const map = {
    no_credentials: 'Enter NAS host, share, username, and password to test.',
  };
  return map[s] || s;
}

async function runBackupDiscovery({ force }) {
  const panel = $('backup-discover-panel');
  const list = $('backup-discover-list');
  const state = $('backup-discover-state');
  if (!panel || !list || !state) return;
  if (_backupDiscoveryRan && !force) {
    panel.hidden = false;
    return;
  }
  _backupDiscoveryRan = true;
  panel.hidden = false;
  state.textContent = 'Scanning your network…';
  state.dataset.kind = '';
  list.innerHTML = '';
  try {
    const r = await fetch('/api/backup/discover');
    const j = await r.json();
    const hosts = (j && j.hosts) || [];
    if (!hosts.length) {
      state.textContent = 'No SMB devices found. Type the NAS address below.';
      return;
    }
    state.textContent = `${hosts.length} device${hosts.length === 1 ? '' : 's'} found — click to use`;
    for (const h of hosts) {
      const li = document.createElement('li');
      const labelHost = h.name && h.name !== h.ip ? h.name : '';
      li.innerHTML = labelHost
        ? `<span class="nas-name"></span><span class="nas-ip"></span>`
        : `<span class="nas-ip"></span>`;
      const nameEl = li.querySelector('.nas-name');
      const ipEl = li.querySelector('.nas-ip');
      if (nameEl) nameEl.textContent = labelHost;
      if (ipEl) ipEl.textContent = h.ip;
      // Click chooses this host: prefer hostname when we have one (works
      // across IP changes if the NAS broadcasts a name) but fall back to
      // raw IP. Connectivity test will fire automatically once the user
      // adds username + password.
      li.addEventListener('click', () => {
        const hostInput = $('backup-host');
        if (hostInput) {
          hostInput.value = labelHost || h.ip;
          hostInput.dispatchEvent(new Event('input', { bubbles: true }));
        }
        // Hide the panel once a pick is made — it's served its purpose.
        panel.hidden = true;
        // If the user already typed creds, populating host should
        // immediately satisfy the auto-test guard; otherwise focus the
        // username field as the natural next step.
        const u = $('backup-username');
        if (u && !u.value) u.focus();
      });
      list.appendChild(li);
    }
  } catch (err) {
    state.textContent = 'Scan failed: ' + (err.message || err);
    state.dataset.kind = 'err';
  }
}

$('backup-discover-rescan')?.addEventListener('click', () => {
  runBackupDiscovery({ force: true });
});

// Share dropdown: as soon as host + username + password are all filled
// in, ask the server to enumerate shares on that host so the user can
// pick from a datalist instead of guessing the share name. Debounced
// the same way as the connectivity auto-test, but with its own signature
// so it doesn't double-fire alongside the test.
let _backupShareSig = '';
async function _maybeLoadShares() {
  const f = _readBackupForm();
  // Share enumeration uses smbclient -L, so it's SMB-specific. Other
  // transports don't have a "share" concept and the auto-test wiring
  // skips this for non-SMB anyway, but be defensive in case a caller
  // invokes _maybeLoadShares() directly.
  if (f.transport !== 'smb') return;
  if (!f.host || !f.username || !f.password) return;
  const sig = `${f.host}|${f.username}|${f.password}|${f.domain}`;
  if (sig === _backupShareSig) return;
  _backupShareSig = sig;
  const dl = $('backup-share-options');
  const hint = $('backup-share-hint');
  if (!dl) return;
  if (hint) { hint.textContent = 'Looking up available shares…'; hint.dataset.kind = ''; }
  try {
    const r = await fetch('/api/backup/list-shares', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        host: f.host, username: f.username,
        password: f.password, domain: f.domain,
      }),
    });
    let j = {};
    try { j = await r.json(); } catch (_) {}
    if (!r.ok || !j.ok) {
      // Non-fatal: user can still type the share name manually.
      if (hint) {
        hint.textContent = 'Couldn\u2019t list shares (' + (j.error || j.detail || ('HTTP ' + r.status)) + ') \u2014 type it manually.';
        hint.dataset.kind = '';
      }
      return;
    }
    const shares = j.shares || [];
    dl.innerHTML = '';
    for (const name of shares) {
      const opt = document.createElement('option');
      opt.value = name;
      dl.appendChild(opt);
    }
    if (hint) {
      if (shares.length) {
        hint.textContent = `${shares.length} share${shares.length === 1 ? '' : 's'} available — click the field to pick.`;
        hint.dataset.kind = 'ok';
      } else {
        hint.textContent = 'No shares listed for this account.';
        hint.dataset.kind = '';
      }
    }
  } catch (err) {
    if (hint) { hint.textContent = 'Share lookup failed: ' + (err.message || err); hint.dataset.kind = ''; }
  }
}

function _backupSetMsg(id, text, kind) {
  // kind: '', 'ok', 'err'. We just toggle inline color; styles in style.css.
  const el = $(id);
  if (!el) return;
  if (!text) { el.hidden = true; el.textContent = ''; el.dataset.kind = ''; return; }
  el.hidden = false;
  el.textContent = text;
  el.dataset.kind = kind || '';
}

async function loadBackupCreds() {
  const status = $('backup-creds-status');
  if (!status) return;
  try {
    const r = await fetch('/api/backup/credentials');
    const j = await r.json();
    _backupHasCreds = !!j.has_credentials;
    if (j.has_credentials && j.remote) {
      const remote = j.remote;
      const transport = remote.transport || 'smb';

      // Build a transport-aware "saved · <where>" status line so the
      // user can tell at a glance which destination is configured.
      let saved_label;
      if (transport === 'smb') {
        saved_label = `saved · \\\\${remote.host || '?'}\\${remote.share || '?'}`;
      } else if (transport === 'rsync_ssh') {
        saved_label = `saved · ${remote.ssh_user || '?'}@${remote.host || '?'}:${remote.target_dir || ''}`;
      } else if (transport === 'rsyncd') {
        const sub = remote.target_subpath ? `/${remote.target_subpath}` : '';
        saved_label = `saved · rsync://${remote.rsyncd_user || '?'}@${remote.host || '?'}/${remote.rsync_module || '?'}${sub}`;
      } else {
        saved_label = `saved · ${remote.host || '?'}`;
      }
      status.textContent = saved_label;

      // Set the transport selector and apply visibility BEFORE
      // populating fields so hidden rows don't end up with stale data.
      const sel = $('backup-transport');
      if (sel) {
        sel.value = transport;
        _applyBackupTransportVisibility();
      }

      // Pre-fill non-secret fields from the saved record so users can
      // edit a single field without re-typing everything. Secrets
      // (password / ssh_key / rsyncd_password) are never echoed back
      // from the server — the user re-pastes them when re-saving.
      const setIfEmpty = (id, value) => {
        const el = $(id);
        if (!el) return;
        if (!el.value || el.dataset.fromSaved === '1') {
          el.value = value || '';
          el.dataset.fromSaved = '1';
        }
      };
      setIfEmpty('backup-host', remote.host);
      if (transport === 'smb') {
        setIfEmpty('backup-share', remote.share);
        setIfEmpty('backup-subdir', remote.subdir || 'jackery-monitor');
        setIfEmpty('backup-username', remote.username);
        setIfEmpty('backup-domain', remote.domain || 'WORKGROUP');
      } else if (transport === 'rsync_ssh') {
        setIfEmpty('backup-ssh-user', remote.ssh_user);
        setIfEmpty('backup-target-dir', remote.target_dir);
      } else if (transport === 'rsyncd') {
        setIfEmpty('backup-rsync-module', remote.rsync_module);
        setIfEmpty('backup-target-subpath', remote.target_subpath);
        setIfEmpty('backup-rsyncd-user', remote.rsyncd_user);
      } else if (transport === 'rsyncd_ssh') {
        setIfEmpty('backup-ssh-user', remote.ssh_user);
        setIfEmpty('backup-ssh-port', String(remote.ssh_port || 22));
        setIfEmpty('backup-rsync-module', remote.rsync_module);
        setIfEmpty('backup-target-subpath', remote.target_subpath);
      }
      $('backup-state-line').textContent = 'Destination configured. Daily backup will run automatically.';
    } else {
      status.textContent = 'not configured';
      $('backup-state-line').textContent = 'No destination saved yet — fill in the form below and Test before saving.';
      _applyBackupTransportVisibility();
    }
  } catch (e) {
    status.textContent = 'failed to load';
  }
}

document.getElementById('backup-creds-form')?.addEventListener('submit', async (e) => {
  e.preventDefault();
  _backupSetMsg('backup-creds-msg', 'Saving…', '');
  const body = _readBackupForm();
  // Validate per-transport required fields up-front so the user gets
  // a meaningful inline message instead of a 400 from the server.
  const required = _BACKUP_REQUIRED_FIELDS[body.transport] || [];
  const missing = required.filter(k => !body[k]);
  if (missing.length) {
    const labelMap = {
      password: 'Password',
      ssh_key: 'SSH private key',
      ssh_password: 'SSH password',
      rsyncd_password: 'rsyncd password',
      ssh_user: 'SSH user',
      target_dir: 'Target directory',
      rsync_module: 'rsync module',
      rsyncd_user: 'rsyncd user',
      host: 'Host',
      share: 'Share',
      username: 'Username',
    };
    const human = missing.map(k => labelMap[k] || k).join(', ');
    _backupSetMsg('backup-creds-msg',
      `Missing required field${missing.length === 1 ? '' : 's'}: ${human}.`, 'err');
    return;
  }
  try {
    const r = await fetch('/api/backup/credentials', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    });
    let j = {};
    try { j = await r.json(); } catch (_) {}
    if (!r.ok) throw new Error(j.detail || j.error || ('HTTP ' + r.status));
    _backupSetMsg('backup-creds-msg', 'Saved.', 'ok');
    // Clear secret fields so they're not left in the DOM after save.
    // Non-secrets are repopulated from the server response, so leaving
    // those alone is fine.
    ['backup-password', 'backup-ssh-key', 'backup-rsyncd-password']
      .forEach(id => { const el = $(id); if (el) el.value = ''; });
    setTimeout(() => _backupSetMsg('backup-creds-msg', '', ''), 2000);
    loadBackupCreds();
    loadBackupStatus();
  } catch (err) {
    _backupSetMsg('backup-creds-msg', 'Failed: ' + (err.message || err), 'err');
  }
});

// Shared connectivity-test runner. `mode` controls how we treat partial
// form input: 'manual' (button click) tests saved creds when the form is
// blank; 'auto' (live debounce) only runs when the form is fully filled
// in to avoid noisy errors mid-typing.
async function _runBackupTest(mode = 'manual') {
  const formBody = _readBackupForm();
  const required = _BACKUP_REQUIRED_FIELDS[formBody.transport] || [];
  const completeForm = required.every(k => !!formBody[k]);
  if (mode === 'auto' && !completeForm) {
    // Quietly do nothing while the user is still typing.
    return;
  }
  // The /api/backup/test endpoint requires either a complete creds set in
  // the body OR no body at all (in which case it uses the saved creds).
  const body = completeForm ? formBody : {};
  _backupSetMsg('backup-creds-msg', 'Testing connection…', '');
  try {
    const r = await fetch('/api/backup/test', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    });
    let j = {};
    try { j = await r.json(); } catch (_) { /* non-JSON body */ }
    if (r.ok && j.ok) {
      const ms = (j.latency_ms != null) ? ` (${j.latency_ms} ms)` : '';
      _backupSetMsg('backup-creds-msg', `OK — write/read probe succeeded${ms}.`, 'ok');
    } else {
      // FastAPI HTTPException returns {detail: "..."} on 4xx/5xx; our own
      // ok:false body uses {error: "..."}. Try both, then HTTP status, so
      // the user always sees something specific.
      const reason = _backupHumaniseError(j.error || j.detail || `HTTP ${r.status}`);
      _backupSetMsg('backup-creds-msg', 'Failed: ' + reason, 'err');
    }
  } catch (err) {
    _backupSetMsg('backup-creds-msg', 'Failed: ' + (err.message || err), 'err');
  }
}

$('backup-test')?.addEventListener('click', () => _runBackupTest('manual'));

// Live auto-test: as soon as the per-transport required fields are all
// non-empty, fire a connectivity check 700ms after the last keystroke.
// The debounce keeps us from hammering the NAS while the user is still
// typing and keeps the message line stable.
(function _wireBackupAutoTest() {
  let testTimer = null;
  let shareTimer = null;
  let lastSig = '';
  function schedule() {
    const f = _readBackupForm();
    // SMB share lookup is SMB-only; schedule it independently of the
    // connectivity test (which uses the full per-transport field set).
    if (f.transport === 'smb' && f.host && f.username && f.password) {
      if (shareTimer) clearTimeout(shareTimer);
      shareTimer = setTimeout(() => _maybeLoadShares(), 700);
    }
    const required = _BACKUP_REQUIRED_FIELDS[f.transport] || [];
    if (!required.every(k => !!f[k])) return;
    // JSON-stringify the body for the change-detection signature so
    // adding new transports doesn't require a custom join() format.
    const sig = JSON.stringify(f);
    if (sig === lastSig) return;
    lastSig = sig;
    if (testTimer) clearTimeout(testTimer);
    testTimer = setTimeout(() => _runBackupTest('auto'), 700);
  }
  // Listen on every transport's inputs + the transport selector
  // itself, so switching transports + filling the new fields fires
  // the auto-test on the next idle window.
  _ALL_BACKUP_INPUT_IDS.forEach(id => {
    const el = $(id);
    if (!el) return;
    const evt = (el.tagName === 'SELECT') ? 'change' : 'input';
    el.addEventListener(evt, schedule);
  });
})();

$('backup-clear')?.addEventListener('click', async () => {
  if (!confirm('Forget saved backup destination? Daily backups will stop until new credentials are saved.')) return;
  await fetch('/api/backup/credentials', { method: 'DELETE' });
  // Wipe every transport's input so a re-save starts from a clean form.
  _ALL_BACKUP_INPUT_IDS.forEach(id => {
    const el = $(id);
    if (!el) return;
    el.value = '';
    delete el.dataset.fromSaved;
  });
  // Restore defaults for fields with non-empty placeholders.
  if ($('backup-subdir')) $('backup-subdir').value = 'jackery-monitor';
  if ($('backup-domain')) $('backup-domain').value = 'WORKGROUP';
  if ($('backup-transport')) $('backup-transport').value = 'smb';
  _applyBackupTransportVisibility();
  loadBackupCreds();
  loadBackupStatus();
});

// Read the form into a creds body shaped for the API. Returns only the
// fields relevant to the currently-selected transport so unused fields
// from a previous transport don't sneak into the JSON body. `transport`
// is always present and used by the server to dispatch.
function _readBackupForm() {
  const transport = _backupTransport();
  const base = {
    transport,
    host: ($('backup-host')?.value || '').trim(),
  };
  if (transport === 'smb') {
    return {
      ...base,
      share: ($('backup-share')?.value || '').trim(),
      subdir: ($('backup-subdir')?.value || '').trim() || 'jackery-monitor',
      username: ($('backup-username')?.value || '').trim(),
      password: $('backup-password')?.value || '',
      domain: ($('backup-domain')?.value || '').trim() || 'WORKGROUP',
    };
  }
  if (transport === 'rsync_ssh') {
    return {
      ...base,
      ssh_user: ($('backup-ssh-user')?.value || '').trim(),
      target_dir: ($('backup-target-dir')?.value || '').trim(),
      // Don't .trim() ssh_key — leading/trailing whitespace in a key
      // can change its identity and break the saved value.
      ssh_key: $('backup-ssh-key')?.value || '',
    };
  }
  if (transport === 'rsyncd') {
    return {
      ...base,
      rsync_module: ($('backup-rsync-module')?.value || '').trim(),
      target_subpath: ($('backup-target-subpath')?.value || '').trim(),
      rsyncd_user: ($('backup-rsyncd-user')?.value || '').trim(),
      rsyncd_password: $('backup-rsyncd-password')?.value || '',
    };
  }
  if (transport === 'rsyncd_ssh') {
    return {
      ...base,
      ssh_port: parseInt($('backup-ssh-port')?.value, 10) || 22,
      ssh_user: ($('backup-ssh-user')?.value || '').trim(),
      ssh_password: $('backup-ssh-password')?.value || '',
      rsync_module: ($('backup-rsync-module')?.value || '').trim(),
      target_subpath: ($('backup-target-subpath')?.value || '').trim(),
    };
  }
  return base;
}

async function loadBackupStatus() {
  const wrap = $('backup-runs');
  if (!wrap) return;
  try {
    const r = await fetch('/api/backup/status');
    const j = await r.json();
    const runs = j.runs || [];
    if (j.last_ok_ts) {
      const d = new Date(j.last_ok_ts * 1000);
      $('backup-last-line').textContent = 'Last successful backup: ' + d.toLocaleString();
    } else if (j.configured) {
      $('backup-last-line').textContent = 'No successful backup yet.';
    } else {
      $('backup-last-line').textContent = 'No destination configured.';
    }
    if (!runs.length) {
      wrap.innerHTML = '<div class="hint">No runs yet. Save credentials and click <strong>Run now</strong> to validate.</div>';
      return;
    }
    wrap.innerHTML = runs.map(_renderBackupRunRow).join('');
  } catch (e) {
    wrap.innerHTML = '<div class="hint">Failed to load status.</div>';
  }
}

function _renderBackupRunRow(run) {
  const ts = run.iso || (run.ts ? new Date(run.ts * 1000).toLocaleString() : '?');
  const ok = !!run.ok;
  const skipped = run.skipped_reason ? ` (skipped: ${run.skipped_reason})` : '';
  const dur = (run.duration_s != null) ? `${run.duration_s.toFixed(1)}s` : '—';
  const bytes = run.bytes_written ? _fmtBytes(run.bytes_written) : '—';
  const files = run.files_written != null ? `${run.files_written} files` : '—';
  const tag = ok
    ? '<span class="backup-pill backup-pill-ok">success</span>'
    : (run.skipped_reason
        ? '<span class="backup-pill backup-pill-warn">skipped</span>'
        : '<span class="backup-pill backup-pill-err">failed</span>');
  const detail = ok
    ? `${files} · ${bytes} · ${dur}`
    : (run.error ? `error: ${run.error}` : skipped || 'unknown failure');
  return `<div class="backup-run">
    <div class="backup-run-head">${tag}<span class="backup-run-ts">${ts}</span></div>
    <div class="backup-run-body">${detail}</div>
  </div>`;
}

function _fmtBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n/1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n/(1024*1024)).toFixed(1)} MB`;
  return `${(n/(1024*1024*1024)).toFixed(2)} GB`;
}

$('backup-status-reload')?.addEventListener('click', loadBackupStatus);

// Shared on-demand backup runner. Supports both the inline button (in the
// credentials card, feedback in `backup-creds-msg`) and the original
// button in the Status & history card (feedback in `backup-run-msg`).
async function _triggerBackupNow(btnId, msgId) {
  const btn = $(btnId);
  if (!btn) return;
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Running…';
  _backupSetMsg(msgId, 'Backup in progress — this may take a minute over slow links.', '');
  try {
    const r = await fetch('/api/backup/run', { method: 'POST' });
    let j = {};
    try { j = await r.json(); } catch (_) {}
    if (r.ok && j.ok) {
      _backupSetMsg(msgId, `OK — wrote ${j.files_written} files (${_fmtBytes(j.bytes_written || 0)}).`, 'ok');
    } else if (j.skipped_reason) {
      _backupSetMsg(msgId, `Skipped: ${j.skipped_reason}`, 'err');
    } else {
      const reason = j.error || j.detail || `HTTP ${r.status}`;
      _backupSetMsg(msgId, 'Failed: ' + reason, 'err');
    }
  } catch (err) {
    _backupSetMsg(msgId, 'Failed: ' + (err.message || err), 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = original;
    loadBackupStatus();
  }
}

$('backup-run-now')?.addEventListener('click',
  () => _triggerBackupNow('backup-run-now', 'backup-run-msg'));
$('backup-run-now-inline')?.addEventListener('click',
  () => _triggerBackupNow('backup-run-now-inline', 'backup-creds-msg'));

// Generic debounced auto-save for one numeric backup setting. Both the
// daily-hour input and the retention input use this — the only thing
// that changes per setting is the key, the bounds, and the formatter
// for the success message. Keeps the keystroke -> /api/settings POST
// throttled to one call per 500ms idle window.
function _wireBackupNumericSetting(inputId, msgId, settingKey, opts) {
  const el = $(inputId);
  if (!el) return;
  let timer = null;
  el.addEventListener('input', () => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(async () => {
      const n = Number(el.value);
      if (!Number.isInteger(n) || n < opts.min || n > opts.max) {
        _backupSetMsg(msgId, opts.outOfRangeMsg, 'err');
        return;
      }
      _backupSetMsg(msgId, 'Saving…', '');
      try {
        const r = await fetch('/api/settings', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ [settingKey]: n }),
        });
        if (!r.ok) {
          let j = {};
          try { j = await r.json(); } catch (_) {}
          throw new Error(j.detail || j.error || ('HTTP ' + r.status));
        }
        _backupSetMsg(msgId, opts.okMsg(n), 'ok');
        setTimeout(() => _backupSetMsg(msgId, '', ''), 2500);
      } catch (err) {
        _backupSetMsg(msgId, 'Save failed: ' + (err.message || err), 'err');
      }
    }, 500);
  });
}

_wireBackupNumericSetting('backup-schedule-hour', 'backup-schedule-msg',
  'backup_schedule_hour', {
    min: 0, max: 23,
    outOfRangeMsg: 'Hour must be 0–23.',
    okMsg: (n) => `Saved — next run at ${String(n).padStart(2, '0')}:00.`,
  });

_wireBackupNumericSetting('backup-keep-count', 'backup-keep-msg',
  'backup_keep_count', {
    min: 1, max: 3650,
    outOfRangeMsg: 'Must be between 1 and 3650.',
    okMsg: (n) => `Saved — keeping ${n} snapshot${n === 1 ? '' : 's'}.`,
  });

// One-time: apply the initial transport visibility now so the hidden
// rsync rows are correctly hidden on first paint, before loadBackupCreds
// fires and possibly re-applies. Subsequent transport changes go
// through _applyBackupTransportVisibility() via the change handler
// registered above.
_applyBackupTransportVisibility();

async function loadBackupSchedule() {
  const hourEl = $('backup-schedule-hour');
  const keepEl = $('backup-keep-count');
  if ((!hourEl || hourEl.dataset.loaded === '1')
      && (!keepEl || keepEl.dataset.loaded === '1')) return;
  try {
    const r = await fetch('/api/settings');
    const j = await r.json();
    // /api/settings returns {settings: [{key, value, label, hint, ...}]}
    const byKey = Object.fromEntries(
      (j?.settings || []).map(s => [s.key, s]));
    const hourEntry = byKey['backup_schedule_hour'];
    if (hourEl && hourEntry && hourEntry.value != null) {
      hourEl.value = String(hourEntry.value);
      hourEl.dataset.loaded = '1';
    }
    const keepEntry = byKey['backup_keep_count'];
    if (keepEl && keepEntry && keepEntry.value != null) {
      keepEl.value = String(keepEntry.value);
      keepEl.dataset.loaded = '1';
    }
  } catch (_) { /* leave blank — user can still type */ }
}

// ---- snapshots / restore --------------------------------------------------

$('backup-snapshots-reload')?.addEventListener('click', loadBackupSnapshots);

async function loadBackupSnapshots() {
  const wrap = $('backup-snapshots');
  if (!wrap) return;
  wrap.innerHTML = '<div class="hint">Mounting remote and listing snapshots…</div>';
  try {
    const r = await fetch('/api/backup/snapshots');
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || j.error || ('HTTP ' + r.status));
    const snaps = j.snapshots || [];
    if (!snaps.length) {
      wrap.innerHTML = '<div class="hint">No snapshots found at the destination yet.</div>';
      return;
    }
    wrap.innerHTML = snaps.map(_renderSnapshotRow).join('');
    // Wire up the per-row controls.
    wrap.querySelectorAll('[data-restore-snap]').forEach(btn => {
      btn.addEventListener('click', () => _doRestore(btn.dataset.restoreSnap, btn));
    });
    wrap.querySelectorAll('[data-toggle-files]').forEach(btn => {
      btn.addEventListener('click', () => {
        const target = wrap.querySelector(`[data-files-for="${btn.dataset.toggleFiles}"]`);
        if (target) target.toggleAttribute('hidden');
      });
    });
  } catch (err) {
    wrap.innerHTML = `<div class="hint">Failed to list snapshots: ${escapeHtml(err.message || err)}</div>`;
  }
}

function _renderSnapshotRow(s) {
  if (!s.valid) {
    return `<div class="backup-snap backup-snap-bad">
      <div class="backup-snap-head">${s.dir}</div>
      <div class="backup-snap-body">Invalid snapshot: ${s.error || 'manifest unreadable'}</div>
    </div>`;
  }
  const bytes = s.total_bytes ? _fmtBytes(s.total_bytes) : '—';
  const files = (s.files || []).length;
  const ts = s.iso || s.dir;
  const filesEsc = (s.files || []).map(f => f.replace(/[<>&"]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]))).join(', ');
  return `<div class="backup-snap">
    <div class="backup-snap-head">
      <strong>${ts}</strong>
      <span class="hint">${files} files · ${bytes}</span>
    </div>
    <div class="backup-snap-body">
      <label class="hint" style="display:flex; gap:6px; align-items:center; margin-bottom:8px">
        <input type="checkbox" class="backup-snap-selective" data-snap="${s.dir}" />
        Selective restore (pick files)
      </label>
      <div class="backup-snap-files" data-files-for="${s.dir}" hidden>
        <div class="hint" style="margin-bottom:6px">Uncheck files you don't want to restore:</div>
        ${(s.files || []).map(f => `<label class="backup-file-pick"><input type="checkbox" class="backup-file-cb" data-snap="${s.dir}" value="${f}" checked> <code>${f}</code></label>`).join('')}
      </div>
      <button class="btn btn-ghost" type="button" data-toggle-files="${s.dir}">Show files (${filesEsc.length > 0 ? files : 0})</button>
      <button class="btn btn-primary" type="button" data-restore-snap="${s.dir}" style="margin-left:8px">Restore this snapshot</button>
    </div>
  </div>`;
}

async function _doRestore(snapDir, btn) {
  // Selective vs full is picked per-row via the "Selective restore" checkbox.
  const wrap = $('backup-snapshots');
  const selective = wrap.querySelector(`.backup-snap-selective[data-snap="${snapDir}"]`)?.checked;
  let scope;
  if (selective) {
    const checked = Array.from(wrap.querySelectorAll(`.backup-file-cb[data-snap="${snapDir}"]:checked`)).map(cb => cb.value);
    if (!checked.length) {
      _backupSetMsg('backup-restore-msg', 'Pick at least one file for selective restore.', 'err');
      return;
    }
    scope = { files: checked };
  } else {
    scope = { full: true };
  }
  const label = scope.full ? 'a full restore' : `restore of ${scope.files.length} file(s)`;
  if (!confirm(`Run ${label} from snapshot ${snapDir}?\n\nThis overwrites the corresponding files in /data on this device.`)) return;
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = 'Restoring…';
  _backupSetMsg('backup-restore-msg', 'Restore in progress…', '');
  try {
    const r = await fetch('/api/backup/restore', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ snapshot: snapDir, scope }),
    });
    const j = await r.json();
    if (!r.ok || !j.ok) throw new Error(j.detail || j.error || ('HTTP ' + r.status));
    const restoredList = j.restored_files || [];
    const n = restoredList.length;
    _backupSetMsg('backup-restore-msg', `Restored ${n} file(s). Reload the page to pick up restored settings.`, 'ok');
  } catch (err) {
    _backupSetMsg('backup-restore-msg', 'Failed: ' + (err.message || err), 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}
