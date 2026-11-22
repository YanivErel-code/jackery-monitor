/* Jackery Monitor — V2 dashboard.
 *
 * Live readings arrive on the same /ws feed the classic view uses. The
 * plan, rates, laundry hold and activity come from /api/v2/overview, which
 * the server builds from the latest saved smart-charge decision.
 */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const SVG = 'http://www.w3.org/2000/svg';
  const OVERVIEW_MS = 30000;
  const HISTORY_MS = 60000;
  const TIER_NAME = { low: 'Off-peak', mid: 'Partial-peak', peak: 'Peak' };
  const PORTS = [['ac', 'AC outlets'], ['dc', 'DC'], ['usb', 'USB'], ['car', 'Car port']];

  let status = null;       // latest /ws payload
  let overview = null;     // latest /api/v2/overview
  let history6 = [];       // 6 h of samples for the battery chart
  let powerHours = 6;
  let powerRows = [];
  let attention = { review: null, lg: [] };
  const pendingPorts = new Set();

  // ---------- small helpers ----------
  function el(tag, props, ...kids) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (k === 'class') node.className = v;
      else if (k === 'text') node.textContent = v;
      else node.setAttribute(k, v);
    }
    for (const kid of kids) if (kid != null) node.append(kid);
    return node;
  }
  function svgEl(tag, attrs, text) {
    const node = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
    if (text != null) node.textContent = text;
    return node;
  }
  const num = (v) => (typeof v === 'number' && isFinite(v) ? v : null);
  const pct = (v) => (num(v) == null ? '—' : Math.round(v) + '%');
  const watts = (v) => (num(v) == null ? '—' : Math.round(v).toLocaleString('en-US') + ' W');
  const usd = (v) => (num(v) == null ? '—' : '$' + v.toFixed(2));
  const tz = () => (overview ? overview.tz_offset_seconds : -new Date().getTimezoneOffset() * 60);
  function clock(ts, withMinutes) {
    const d = new Date((ts + tz()) * 1000);
    const h = d.getUTCHours(), m = d.getUTCMinutes();
    const base = (h % 12 || 12) + (m || withMinutes ? ':' + String(m).padStart(2, '0') : '');
    return base + (h < 12 ? ' AM' : ' PM');
  }
  async function getJson(path) {
    const r = await fetch(path);
    if (r.status === 401) { location.href = '/login'; throw new Error('signed out'); }
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || 'HTTP ' + r.status);
    return r.json();
  }
  const deviceSn = () => status?.device?.device_sn || null;
  const snQuery = () => (deviceSn() ? '?device_sn=' + encodeURIComponent(deviceSn()) : '');

  // ---------- theme, version switch, navigation ----------
  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    const dark = theme === 'dark';
    $('theme-toggle').setAttribute('aria-pressed', String(dark));
    $('theme-toggle').textContent = dark ? 'Light mode' : 'Dark mode';
  }
  $('theme-toggle').addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    try { localStorage.setItem('jackery_theme', next); } catch (e) { /* private mode */ }
    applyTheme(next);
  });
  applyTheme(document.documentElement.dataset.theme || 'light');

  $('use-v1').addEventListener('click', () => {
    try { localStorage.setItem('jackery_ui', 'v1'); } catch (e) { /* private mode */ }
    location.href = '/';
  });

  function route() {
    const page = location.hash === '#charging' ? 'charging' : 'live';
    $('page-live').hidden = page !== 'live';
    $('page-charging').hidden = page !== 'charging';
    for (const a of document.querySelectorAll('.nav-item[data-page]')) {
      if (a.dataset.page === page) a.setAttribute('aria-current', 'page');
      else a.removeAttribute('aria-current');
    }
    drawCharts();
  }
  window.addEventListener('hashchange', route);

  // ---------- live readings ----------
  function renderStatus() {
    if (!status) return;
    const t = status.telemetry || {};
    const connected = status.connection_status === 'connected';
    $('conn-dot').classList.toggle('ok', connected);
    $('conn-text').textContent = connected ? 'Connected' : (status.connection_status || 'Disconnected');

    const soc = num(t.system_soc_pct) ?? num(t.battery_percent);
    const capacity = num(t.capacity_wh);
    $('bat-pct').textContent = pct(soc);
    $('bat-kwh').textContent = soc != null && capacity
      ? (soc / 100 * capacity / 1000).toFixed(1) + ' of ' + (capacity / 1000).toFixed(1) + ' kWh' : '';
    $('bat-fill').style.width = (soc ?? 0) + '%';

    const solar = num(t.solar_input_w), grid = num(t.ac_input_w), home = num(t.output_power_w);
    $('now-solar').textContent = watts(solar);
    $('now-grid').textContent = watts(grid);
    $('now-home').textContent = watts(home);
    for (const [id, v] of [['now-solar', solar], ['now-grid', grid], ['now-home', home]]) {
      $(id).classList.toggle('zero', !v);
    }
    const net = (num(t.input_power_w) ?? 0) - (home ?? 0);
    const filling = net > 20, draining = net < -20;
    $('now-net').classList.toggle('filling', filling);
    $('now-net-text').textContent = filling ? 'Battery filling' : draining ? 'Battery draining' : 'Battery holding steady';
    $('now-net-w').textContent = filling || draining ? (net > 0 ? '+' : '−') + watts(Math.abs(net)) : '';

    renderPacks(t);
    renderOutputs(t);
    renderDevices();
  }

  function renderPacks(t) {
    const packs = status.battery_packs || [];
    $('packs').hidden = !packs.length;
    if (!packs.length) return;
    const list = $('packs-list');
    list.replaceChildren(el('div', { class: 'row' }, el('span', { text: 'Main unit' }), el('b', { text: pct(t.main_soc_pct) })));
    packs.forEach((p, i) => list.append(
      el('div', { class: 'row' }, el('span', { text: 'Pack ' + (i + 1) }), el('b', { text: pct(p.rb) }))));
  }

  function renderOutputs(t) {
    const box = $('outputs');
    if (!box.childElementCount) {
      for (const [port, label] of PORTS) {
        const btn = el('button', { type: 'button', class: 'switch', role: 'switch', 'aria-label': label, 'data-port': port });
        btn.addEventListener('click', () => toggleOutput(port, label, btn));
        box.append(el('div', { class: 'row' }, el('span', { text: label }), btn));
      }
    }
    for (const btn of box.querySelectorAll('.switch')) {
      const port = btn.dataset.port;
      if (pendingPorts.has(port)) continue;
      btn.setAttribute('aria-checked', String(!!t[port + '_on']));
      btn.disabled = !!status.device?.read_only;
    }
  }

  async function toggleOutput(port, label, btn) {
    const on = btn.getAttribute('aria-checked') !== 'true';
    if (port === 'ac' && !on && !confirm('Turn AC outlets off? This cuts power to everything plugged into the Jackery.')) return;
    pendingPorts.add(port);
    btn.disabled = true;
    $('outputs-msg').textContent = 'Turning ' + label + (on ? ' on…' : ' off…');
    try {
      const r = await fetch('/api/set_output', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ port, on, device_sn: deviceSn() }),
      });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || 'HTTP ' + r.status);
      btn.setAttribute('aria-checked', String(on));
      $('outputs-msg').textContent = label + ' turned ' + (on ? 'on.' : 'off.');
    } catch (e) {
      $('outputs-msg').textContent = 'Could not change ' + label + ': ' + e.message;
    } finally {
      btn.disabled = false;
      // Let the device report the new state before telemetry overwrites the switch.
      setTimeout(() => pendingPorts.delete(port), 6000);
    }
  }

  function renderDevices() {
    const select = $('device');
    let devices = status.cloud?.devices || [];
    if (!devices.length && status.device) devices = [{ device_id: '', name: status.device.name }];
    const selected = String(status.cloud?.selected_device_id ?? '');
    if (select.childElementCount !== devices.length) {
      select.replaceChildren(...devices.map((d) => el('option', { value: String(d.device_id), text: d.name })));
    }
    if (document.activeElement !== select) select.value = selected;
  }
  $('device').addEventListener('change', async (e) => {
    await fetch('/api/view/select_device', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ device_id: e.target.value }),
    }).catch(() => {});
    location.reload();
  });

  // ---------- overview: headline, battery marks, cost, charging page ----------
  function renderOverview() {
    const o = overview;
    if (!o) return;
    const h = o.headline;
    $('hl-badge').className = 'badge ' + h.tone;
    $('hl-badge').textContent = { ok: 'On track', warn: 'Heads up', alert: 'Needs attention', info: 'Manual' }[h.tone] || '';
    $('hl-title').textContent = h.title;
    $('hl-detail').textContent = h.detail || '';
    $('clock').textContent = 'Plan checked at ' + (o.decided_at ? clock(o.decided_at, true) : '—');

    for (const [key, label] of [['reserve', 'Reserve'], ['target', 'Sunrise target']]) {
      const v = num(o.battery[key + '_pct']);
      $('bat-' + key).hidden = $('bat-' + key + '-label').hidden = v == null;
      if (v == null) continue;
      $('bat-' + key).style.left = $('bat-' + key + '-label').style.left = v + '%';
      $('bat-' + key + '-label').textContent = label + ' ' + Math.round(v) + '%';
    }

    const g = o.grid_today;
    $('cost-total').textContent = usd(g.cost);
    $('cost-kwh').textContent = g.kwh.toFixed(2) + ' kWh bought';
    $('cost-tiers').replaceChildren(...g.tiers.map((t) => el('div', { class: 'row' },
      el('span', { text: TIER_NAME[t.tier] + ', ' + usd(t.rate) }),
      el('b', { class: t.kwh ? '' : 'zero', text: t.kwh.toFixed(2) + ' kWh' }))));

    renderCharging(o);
    renderAttention();
    drawCharts();
  }

  function renderCharging(o) {
    const p = o.plan, next = p.blocks[0];
    $('charging-sub').textContent = o.mode === 'active'
      ? 'Automatic grid charging · plan checked at ' + clock(o.decided_at, true) : 'Automatic charging is off';
    if (p.charging_now && next) $('next-title').textContent = 'Charging now, until ' + clock(next.end);
    else if (next) $('next-title').textContent = clock(next.start) + ' – ' + clock(next.end);
    else $('next-title').textContent = 'No grid charging planned';
    const more = p.blocks.length > 1 ? ' across ' + p.blocks.length + ' blocks' : '';
    $('next-detail').textContent = p.minutes
      ? (p.minutes / 60).toFixed(1) + ' hours planned' + more + '. About ' + p.est_kwh + ' kWh, about ' + usd(p.est_cost) + '.'
      : '';
    $('stat-now').textContent = pct(o.battery.soc_pct);
    $('stat-low').textContent = pct(p.min_soc_pct);
    $('stat-sunrise').textContent = pct(p.sunrise_soc_pct);
    $('next-nocharge').textContent = num(p.no_charge_sunrise_soc_pct) != null && p.minutes
      ? 'Without charging, the battery would be at about ' + pct(p.no_charge_sunrise_soc_pct) + ' by sunrise.' : '';

    const checks = [];
    const l = o.laundry;
    if (!l.enabled) checks.push(['warn', 'Laundry hold is off', 'Charging is not paused while the washer or dryer runs.']);
    else if (l.blocked) checks.push(['warn', 'On hold for laundry', l.expected_finish_ts
      ? 'Charging is held while ' + l.names + ' runs, expected to finish around ' + clock(l.expected_finish_ts) + '.'
      : 'Charging is held while ' + l.names + ' runs.']);
    else if (l.monitoring_degraded) checks.push(['warn', 'Laundry status is uncertain', 'The washer or dryer is not reporting reliably.']);
    else checks.push(['ok', 'Laundry is clear', 'Charging is held whenever the washer or dryer runs.']);
    const v = o.verification.status;
    if (v === 'not_charging') checks.push(['alert', 'Plug is on but the battery is not charging', 'Check the charger cable and the garage plug.']);
    else if (v === 'charging') checks.push(['ok', 'Charging confirmed', 'The battery is taking power while the plug is on.']);
    else checks.push(['ok', 'Charge check is ready', 'Each charge is confirmed by watching the battery take power.']);
    const reserve = num(o.battery.reserve_pct), low = num(p.min_soc_pct);
    if (p.feasible === false) checks.push(['alert', 'Reserve at risk', p.warnings[0] || 'The plan cannot keep the reserve.']);
    else if (low != null && reserve != null) checks.push(['ok', 'Reserve is safe',
      'The projected low of ' + pct(low) + ' stays above the ' + pct(reserve) + ' reserve.']);
    $('protections').replaceChildren(...checks.map(([tone, title, text]) => el('div', { class: 'check ' + tone },
      el('i', { 'aria-hidden': 'true', text: tone === 'ok' ? '✓' : '!' }),
      el('div', {}, el('b', { text: title }), el('span', { text: text })))));

    renderStrip(o);

    $('activity').replaceChildren(...(o.activity.length ? o.activity.map((e) => el('div', { class: 'event' },
      el('time', { text: clock(e.ts, true) }),
      el('div', {}, el('b', { text: e.title }), el('span', { text: e.detail }))))
      : [el('p', { class: 'muted', text: 'Nothing changed in the last 24 hours.' })]));

    const c = o.config;
    const rows = [
      ['Battery level wanted at sunrise', pct(c.target_sunrise_soc_pct)],
      ['Never go below', pct(c.reserve_soc_pct)],
      ['Charger plug', c.kasa_device_host || 'Not set'],
      ['Circuit limit', watts(c.ac_supply_limit_w)],
      ['Hold charging during laundry', c.lg_interlock_enabled ? 'On' : 'Off'],
      ['Measured charging power', watts(c.max_charge_w)],
    ];
    $('settings-list').replaceChildren(...rows.map(([k, val]) =>
      el('div', { class: 'row' }, el('span', { text: k }), el('b', { text: val }))));
  }

  function renderStrip(o) {
    const start = o.now, end = o.now + 24 * 3600, span = end - start;
    const segs = o.rates.map((s) => ({ ...s, start: Math.max(s.start, start), end: Math.min(s.end, end) }))
      .filter((s) => s.end > s.start);
    $('strip').replaceChildren(...segs.map((s) => {
      const d = el('div', { class: s.tier, text: (s.end - s.start) / span > 0.12 ? TIER_NAME[s.tier] + ' ' + usd(s.rate) : usd(s.rate) });
      d.style.flex = String(s.end - s.start);
      return d;
    }));
    $('strip-charge').replaceChildren(...o.plan.blocks.filter((b) => b.start < end).map((b) => {
      const d = el('div', { title: 'Charging ' + clock(b.start) + ' – ' + clock(b.end) });
      d.style.left = (Math.max(b.start, start) - start) / span * 100 + '%';
      d.style.width = (Math.min(b.end, end) - Math.max(b.start, start)) / span * 100 + '%';
      return d;
    }));
    const ticks = [start];
    for (let t = Math.ceil(start / 21600) * 21600 - (tz() % 21600); t < end; t += 21600) {
      if (t - start > 5400 && end - t > 5400) ticks.push(t);
    }
    ticks.push(end);
    $('strip-axis').replaceChildren(...ticks.map((t) => {
      const s = el('span', { text: clock(t) });
      s.style.left = (t - start) / span * 100 + '%';
      return s;
    }));
  }

  function renderAttention() {
    const notes = [];
    if (status && status.connection_status !== 'connected') {
      notes.push(['alert', 'Not connected to the Jackery', status.connection_error || 'Readings on this page may be out of date.']);
    }
    if (overview && overview.plan.feasible === false) {
      for (const w of overview.plan.warnings) notes.push(['alert', 'Charging plan', w]);
    }
    for (const w of attention.lg) notes.push(['warn', 'Laundry monitoring', w]);
    const r = attention.review;
    if (r && (r.status === 'error' || r.status === 'failed')) {
      notes.push(['warn', 'The AI review could not run', (r.error || r.message || 'It failed the last time it ran.') +
        ' You can change the AI provider under Settings.']);
    }
    $('attention').replaceChildren(...(notes.length
      ? notes.map(([tone, title, text]) => el('div', { class: 'note ' + tone }, el('b', { text: title }), el('span', { text: text })))
      : [el('p', { class: 'muted', text: 'Nothing needs your attention.' })]));
  }

  // ---------- charts ----------
  function frame(svg, x0, x1, yMax, yLabel) {
    svg.replaceChildren();
    const W = svg.clientWidth || 800, H = 260;
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    const box = { l: 44, r: W - 12, t: 12, b: H - 44 };
    const X = (ts) => box.l + (ts - x0) / (x1 - x0) * (box.r - box.l);
    const Y = (v) => box.b - v / yMax * (box.b - box.t);
    return { svg, W, H, box, X, Y, yMax, yLabel, x0, x1 };
  }
  function axes(f, stepH) {
    for (let i = 0; i <= 4; i++) {
      const v = f.yMax * i / 4;
      f.svg.append(svgEl('line', { class: i ? 'gridline' : 'axis', x1: f.box.l, x2: f.box.r, y1: f.Y(v), y2: f.Y(v) }));
      f.svg.append(svgEl('text', { x: f.box.l - 6, y: f.Y(v) + 4, 'text-anchor': 'end' }, f.yLabel(v)));
    }
    const step = stepH * 3600 * (f.W < 520 ? 2 : 1);
    const offset = tz() % step;
    for (let t = Math.ceil((f.x0 + offset) / step) * step - offset; t <= f.x1; t += step) {
      f.svg.append(svgEl('text', { x: f.X(t), y: f.H - 8, 'text-anchor': 'middle' }, clock(t)));
    }
  }
  const path = (f, rows, key) => rows.filter((r) => num(r[key]) != null)
    .map((r) => f.X(r.ts).toFixed(1) + ',' + f.Y(Math.min(r[key], f.yMax)).toFixed(1)).join(' ');

  function drawBattery() {
    const svg = $('chart-battery');
    if (!overview || !svg.clientWidth) return;
    const o = overview, now = o.now;
    const f = frame(svg, now - 6 * 3600, now + 18 * 3600, 100, (v) => v + '%');
    for (const s of o.rates) {
      if (s.tier === 'low' || s.end <= f.x0 || s.start >= f.x1) continue;
      const a = f.X(Math.max(s.start, f.x0)), b = f.X(Math.min(s.end, f.x1));
      svg.append(svgEl('rect', { class: 'band-' + s.tier, x: a, y: f.box.t, width: b - a, height: f.box.b - f.box.t }));
    }
    axes(f, 3);
    for (const [key, label] of [['target', 'Sunrise target'], ['reserve', 'Reserve']]) {
      const v = num(o.battery[key + '_pct']);
      if (v == null) continue;
      svg.append(svgEl('line', { class: 'ref', x1: f.box.l, x2: f.box.r, y1: f.Y(v), y2: f.Y(v) }));
      svg.append(svgEl('text', { x: f.box.l + 6, y: f.Y(v) - 5 }, label + ' ' + Math.round(v) + '%'));
    }
    // Grid charging: measured (past samples with AC input) and planned blocks.
    const bars = o.plan.blocks.map((b) => [Math.max(b.start, now), b.end]);
    let run = null;
    for (const r of history6) {
      if ((r.ac_input_w || 0) > 50) run = run ? [run[0], r.ts] : [r.ts, r.ts];
      else if (run) { bars.push(run); run = null; }
    }
    if (run) bars.push(run);
    for (const [a, b] of bars) {
      const xa = f.X(Math.max(a, f.x0)), xb = f.X(Math.min(b, f.x1));
      if (xb > xa) svg.append(svgEl('rect', { class: 'charge', x: xa, y: f.box.b + 4, width: Math.max(2, xb - xa), height: 10, rx: 3 }));
    }
    svg.append(svgEl('polyline', { class: 'measured', points: path(f, history6.filter((r) => r.ts <= now), 'battery_pct') }));
    const proj = o.projection.filter((p) => p.ts <= f.x1);
    svg.append(svgEl('polyline', { class: 'forecast', points: path(f, proj, 'soc') }));
    const soc = num(o.battery.soc_pct);
    svg.append(svgEl('line', { class: 'now-line', x1: f.X(now), x2: f.X(now), y1: f.box.t, y2: f.box.b }));
    if (soc != null) {
      svg.append(svgEl('circle', { class: 'now-dot', cx: f.X(now), cy: f.Y(soc), r: 5 }));
      svg.append(svgEl('text', { class: 'lbl', x: f.X(now) + 8, y: f.Y(soc) - 10 }, 'Now ' + Math.round(soc) + '%'));
    }
    const sunrise = o.plan.sunrise_ts;
    if (sunrise && sunrise < f.x1 && num(o.plan.sunrise_soc_pct) != null) {
      const nearEdge = f.X(sunrise) > f.box.r - 50;
      svg.append(svgEl('circle', { class: 'now-dot', cx: f.X(sunrise), cy: f.Y(o.plan.sunrise_soc_pct), r: 4 }));
      svg.append(svgEl('text', { class: 'lbl', x: f.X(sunrise) - (nearEdge ? 8 : 0), y: f.Y(o.plan.sunrise_soc_pct) - 12, 'text-anchor': nearEdge ? 'end' : 'middle' },
        'Sunrise ' + Math.round(o.plan.sunrise_soc_pct) + '%'));
    }
  }

  function drawPower() {
    const svg = $('chart-power');
    if (!svg.clientWidth) return;
    if (!powerRows.length) {
      svg.replaceChildren(svgEl('text', { class: 'empty', x: '50%', y: '50%', 'text-anchor': 'middle' }, 'No readings in this range yet.'));
      return;
    }
    const peak = Math.max(500, ...powerRows.map((r) => Math.max(r.solar_w || 0, r.ac_input_w || 0, r.output_w || 0)));
    const unit = peak > 4000 ? 2000 : peak > 2000 ? 1000 : 500;
    const yMax = Math.ceil(peak / unit / 4) * unit * 4;
    const x1 = powerRows[powerRows.length - 1].ts;
    const f = frame(svg, x1 - powerHours * 3600, x1, yMax, (v) => (v >= 1000 ? (v / 1000) + ' kW' : v + ' W'));
    axes(f, powerHours === 1 ? 0.25 : powerHours === 6 ? 1 : 4);
    svg.append(svgEl('polyline', { class: 's-home', points: path(f, powerRows, 'output_w') }));
    svg.append(svgEl('polyline', { class: 's-grid', points: path(f, powerRows, 'ac_input_w') }));
    svg.append(svgEl('polyline', { class: 's-solar', points: path(f, powerRows, 'solar_w') }));
  }
  function drawCharts() { drawBattery(); drawPower(); }
  let resizeTimer = null;
  window.addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(drawCharts, 150); });

  for (const btn of document.querySelectorAll('.seg button')) {
    btn.addEventListener('click', () => {
      powerHours = Number(btn.dataset.hours);
      for (const b of document.querySelectorAll('.seg button')) b.setAttribute('aria-pressed', String(b === btn));
      loadHistory();
    });
  }

  // ---------- loading ----------
  async function loadOverview() {
    if (!deviceSn()) return;
    try {
      overview = await getJson('/api/v2/overview' + snQuery());
      renderOverview();
    } catch (e) {
      $('hl-badge').className = 'badge warn';
      $('hl-badge').textContent = 'Unavailable';
      $('hl-title').textContent = 'The charging plan is not available.';
      $('hl-detail').textContent = e.message;
    }
    getJson('/api/algorithm/review_status' + snQuery()).then((r) => { attention.review = r; renderAttention(); }).catch(() => {});
    getJson('/api/lg_thinq/status').then((r) => { attention.lg = r.protection_enabled ? r.warnings || [] : []; renderAttention(); }).catch(() => {});
  }
  async function loadHistory() {
    if (!deviceSn()) return;
    const sn = '&device_sn=' + encodeURIComponent(deviceSn());
    try {
      history6 = (await getJson('/api/energy/history?hours=6' + sn)).history || [];
      powerRows = powerHours === 6 ? history6 : (await getJson('/api/energy/history?hours=' + powerHours + sn)).history || [];
      drawCharts();
    } catch (e) { /* keep the last drawing */ }
  }

  function applyStatus(data) {
    const firstDevice = !deviceSn();
    const previous = deviceSn();
    status = data;
    renderStatus();
    renderAttention();
    if (firstDevice || previous !== deviceSn()) { loadOverview(); loadHistory(); }
  }
  function connect() {
    const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.type === 'snapshot' || msg.type === 'telemetry' || msg.type === 'status') applyStatus(msg.data);
    };
    ws.onclose = () => {
      $('conn-dot').classList.remove('ok');
      $('conn-text').textContent = 'Reconnecting…';
      setTimeout(connect, 3000);
    };
  }

  route();
  getJson('/api/status').then(applyStatus).catch(() => {});
  connect();
  setInterval(loadOverview, OVERVIEW_MS);
  setInterval(loadHistory, HISTORY_MS);
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
})();
