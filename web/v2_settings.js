/* Jackery Monitor — V2 Settings page: general, rates, AI, laundry, alerts,
 * backup, device and logs. Same endpoints as the classic Settings, Device and Logs
 * tabs. Changing the backup destination and restoring stay on the classic
 * backup screen. */
(function () {
  'use strict';
  const { $, el, num, getJson, deviceSn } = window.V2;
  const { send, say, run } = window.V2.forms;

  const loaded = new Set();
  let section = 'general';
  let runtimeSpecs = [];
  let presets = [];
  let prefs = {};
  let lastDevice = null;

  const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : 'never');
  const row = (label, value) => el('div', { class: 'row' }, el('span', { text: label }), el('b', { text: value }));
  const dq = () => '?device_sn=' + encodeURIComponent(deviceSn());

  // ---------- general ----------
  async function loadGeneral() {
    const j = await getJson('/api/settings');
    const specs = j.settings || [];
    const def = specs.find((s) => s.key === 'default_ui_version');
    if (def) $('set-default-ui').value = String(def.value);
    runtimeSpecs = specs.filter((s) => s.control !== 'custom');
    $('runtime-form').replaceChildren(...runtimeSpecs.map((s) => {
      const input = el('input', { id: 'rt-' + s.key, type: 'number', min: s.min, max: s.max, step: '1', inputmode: 'numeric' });
      input.value = s.value;
      return el('div', { class: 'f-row' },
        el('label', { class: 'f-label', for: 'rt-' + s.key }, el('b', { text: s.label }), el('span', { text: s.hint || '' })),
        el('div', { class: 'f-control' }, input, el('span', { class: 'f-unit', text: s.min + '–' + s.max })));
    }));
    getJson('/api/location').then((loc) => {
      $('loc-current').textContent = num(loc.latitude) == null ? 'No location set yet.'
        : 'Now: ' + (loc.label || 'unnamed place') + ' (' + loc.latitude.toFixed(3) + ', ' + loc.longitude.toFixed(3) + ')' + (loc.timezone ? ', ' + loc.timezone : '');
    }).catch(() => {});
  }
  $('set-default-ui').addEventListener('change', () => run('default-msg', 'Saving…',
    () => send('POST', '/api/settings', { default_ui_version: Number($('set-default-ui').value) }),
    () => ($('set-default-ui').value === '1' ? 'Saved. The classic view now opens by default.' : 'Saved. The new dashboard now opens by default.')));
  $('runtime-save').addEventListener('click', async () => {
    const body = {};
    for (const s of runtimeSpecs) {
      const raw = $('rt-' + s.key).value.trim(), n = Number(raw);
      if (raw === '' || !Number.isInteger(n) || n < s.min || n > s.max) { say('runtime-msg', s.label + ' must be a whole number from ' + s.min + ' to ' + s.max + '.', 'bad'); return; }
      if (n !== s.value) body[s.key] = n;
    }
    if (!Object.keys(body).length) { say('runtime-msg', 'Nothing changed.'); return; }
    const out = await run('runtime-msg', 'Saving…', () => send('POST', '/api/settings', body), 'Saved.');
    if (out) for (const s of runtimeSpecs) { if (s.key in (out.settings || {})) { s.value = out.settings[s.key]; $('rt-' + s.key).value = s.value; } }
  });
  $('loc-search').addEventListener('click', async () => {
    const term = $('loc-q').value.trim();
    if (!term) { say('loc-msg', 'Type a town to search for.', 'bad'); return; }
    const found = await run('loc-msg', 'Searching…', () => getJson('/api/location/geocode?q=' + encodeURIComponent(term)), (j) => ((j.results || j || []).length ? 'Pick one:' : 'Nothing found.'));
    const list = found ? (found.results || found) : [];
    $('loc-results').replaceChildren(...(Array.isArray(list) ? list : []).map((r) => {
      const label = [r.name, r.admin1, r.country].filter(Boolean).join(', ');
      const use = el('button', { class: 'btn small', type: 'button', text: 'Use this' });
      use.addEventListener('click', async () => {
        if (await run('loc-msg', 'Saving…', () => send('POST', '/api/location', { latitude: r.latitude, longitude: r.longitude, label: r.name || label }), 'Location saved.')) { $('loc-results').replaceChildren(); loaded.delete('general'); show(); }
      });
      return el('div', { class: 'item' }, el('div', {}, el('b', { text: label }), el('span', { text: Number(r.latitude).toFixed(3) + ', ' + Number(r.longitude).toFixed(3) })), el('div', { class: 'item-side' }, use));
    }));
  });

  // ---------- rates ----------
  function rateRow(r) {
    const cell = (cls, value, attrs) => { const i = el('input', { class: cls, ...attrs }); i.value = value ?? ''; return el('td', {}, i); };
    const remove = el('button', { class: 'btn small', type: 'button', text: 'Remove', 'aria-label': 'Remove this time band' });
    const tr = el('tr', {},
      cell('r-label', r.label, { type: 'text', 'aria-label': 'Name' }),
      cell('r-start', r.start_hour, { type: 'number', min: '0', max: '23', step: '1', 'aria-label': 'From hour' }),
      cell('r-end', r.end_hour, { type: 'number', min: '1', max: '24', step: '1', 'aria-label': 'To hour' }),
      cell('r-rate', r.rate, { type: 'number', min: '0', step: '0.01', 'aria-label': 'Dollars per kWh' }),
      cell('r-months', (r.months || []).join(','), { type: 'text', 'aria-label': 'Months' }),
      el('td', {}, remove));
    remove.addEventListener('click', () => tr.remove());
    return tr;
  }
  function fillRates(plan) {
    $('rate-type').value = plan.type === 'tou' ? 'tou' : 'flat';
    $('rate-flat').value = plan.rate_per_kwh ?? '';
    $('rate-table').tBodies[0].replaceChildren(...(plan.tou_rates || []).map(rateRow));
    $('rate-flat-row').hidden = plan.type === 'tou';
    $('rate-tou').hidden = plan.type !== 'tou';
  }
  async function loadRates() {
    const j = await getJson('/api/cost/plan');
    presets = j.presets || [];
    $('rate-preset').replaceChildren(el('option', { value: '', text: 'Choose a plan…' }), ...presets.map((p) => el('option', { value: p.id, text: p.label })));
    fillRates(j.plan || { type: 'flat' });
  }
  function readRates() {
    if ($('rate-type').value === 'flat') {
      const rate = Number($('rate-flat').value);
      if ($('rate-flat').value.trim() === '' || !(rate >= 0)) throw new Error('Enter a price per kWh.');
      return { type: 'flat', rate_per_kwh: rate, currency: 'USD' };
    }
    const bands = [...$('rate-table').tBodies[0].rows].map((tr, i) => {
      const get = (c) => tr.querySelector('.' + c).value.trim();
      const start = Number(get('r-start')), end = Number(get('r-end')), rate = Number(get('r-rate'));
      const where = 'Time band ' + (i + 1) + ': ';
      if (get('r-start') === '' || !Number.isInteger(start) || start < 0 || start > 23) throw new Error(where + '"from hour" must be 0 to 23.');
      if (get('r-end') === '' || !Number.isInteger(end) || end < 1 || end > 24) throw new Error(where + '"to hour" must be 1 to 24.');
      if (get('r-rate') === '' || !(rate >= 0)) throw new Error(where + 'enter a price.');
      const months = get('r-months') ? get('r-months').split(/[\s,]+/).filter(Boolean).map(Number) : [];
      if (months.some((m) => !Number.isInteger(m) || m < 1 || m > 12)) throw new Error(where + 'months must be numbers from 1 to 12.');
      const band = { start_hour: start, end_hour: end, rate, label: get('r-label') };
      if (months.length) band.months = months;
      return band;
    });
    if (!bands.length) throw new Error('Add at least one time band.');
    return { type: 'tou', currency: 'USD', tou_rates: bands };
  }
  $('rate-type').addEventListener('change', () => { $('rate-flat-row').hidden = $('rate-type').value === 'tou'; $('rate-tou').hidden = $('rate-type').value !== 'tou'; });
  $('rate-add').addEventListener('click', () => $('rate-table').tBodies[0].append(rateRow({ label: '', start_hour: 0, end_hour: 24, rate: '' })));
  $('rate-preset').addEventListener('change', () => {
    const p = presets.find((x) => x.id === $('rate-preset').value);
    if (p) { fillRates(p.plan); say('rate-msg', 'Filled in from "' + p.label + '". Press Save to use it.'); }
  });
  $('rate-save').addEventListener('click', async () => {
    let plan;
    try { plan = readRates(); } catch (e) { say('rate-msg', e.message, 'bad'); return; }
    const out = await run('rate-msg', 'Saving…', () => send('POST', '/api/cost/plan', plan), 'Rates saved.');
    if (out?.plan) fillRates(out.plan);
  });

  // ---------- AI ----------
  const EFFORTS = [['low', 'Low'], ['medium', 'Medium'], ['high', 'High']];
  const AI = {
    anthropic: { fields: [['advisor_model', 'Model for reviews', 'models'], ['narrator_model', 'Model for decision explanations', 'models'], ['advisor_thinking_effort', 'Thinking effort for reviews', EFFORTS]] },
    openai: { fields: [['openai_advisor_model', 'Model for reviews', 'models'], ['openai_narrator_model', 'Model for decision explanations', 'models'], ['openai_advisor_effort', 'Thinking effort for reviews', EFFORTS]] },
  };
  async function loadAiCard(name) {
    const card = document.querySelector('[data-ai="' + name + '"]');
    const [key, models] = await Promise.all([getJson('/api/' + name + '/key'), getJson('/api/' + name + '/models').catch(() => ({ models: [] }))]);
    card.querySelector('.ai-key').textContent = key.has_key ? (key.source === 'env' ? 'A key is set in the server environment.' : 'A key is saved.') : 'No key saved. This provider cannot be used until one is added.';
    card.querySelector('.ai-key-remove').hidden = !(key.has_key && key.source !== 'env');
    const ids = (models.models || []).map((m) => [m.id, m.display_name || m.id]);
    card.querySelector('.ai-models').replaceChildren(...AI[name].fields.map(([k, label, options]) => {
      const select = el('select', { id: 'ai-' + k, 'data-key': k });
      const list = options === 'models' ? ids.slice() : options;
      if (prefs[k] != null && !list.some(([v]) => v === prefs[k])) list.unshift([prefs[k], prefs[k]]);
      for (const [v, text] of list) select.append(el('option', { value: v, text }));
      if (prefs[k] != null) select.value = prefs[k];
      return el('div', { class: 'f-row' }, el('label', { class: 'f-label', for: 'ai-' + k }, el('b', { text: label })), el('div', { class: 'f-control' }, select));
    }));
  }
  async function loadAi() {
    prefs = await getJson('/api/anthropic/prefs');
    $('ai-provider').value = prefs.provider || 'anthropic';
    await Promise.all(['anthropic', 'openai'].map(loadAiCard));
  }
  $('ai-provider').addEventListener('change', () => run('provider-msg', 'Saving…', () => send('POST', '/api/ai/provider', { provider: $('ai-provider').value }), 'Saved.'));
  for (const name of ['anthropic', 'openai']) {
    const card = document.querySelector('[data-ai="' + name + '"]');
    const msg = card.querySelector('.ai-msg'); msg.id = 'ai-msg-' + name;
    card.querySelector('.ai-key-save').addEventListener('click', async () => {
      const input = card.querySelector('.ai-key-input'), key = input.value.trim();
      if (!key) { say(msg.id, 'Paste a key first.', 'bad'); return; }
      if (await run(msg.id, 'Checking the key…', () => send('POST', '/api/' + name + '/key', { api_key: key }), 'Key saved.')) { input.value = ''; loadAiCard(name); }
    });
    card.querySelector('.ai-key-remove').addEventListener('click', async () => {
      if (!confirm('Remove the saved key? Reviews with this provider will stop working.')) return;
      if (await run(msg.id, 'Removing…', () => send('DELETE', '/api/' + name + '/key'), 'Key removed.')) loadAiCard(name);
    });
    card.querySelector('.ai-models-save').addEventListener('click', async () => {
      const body = {};
      for (const select of card.querySelectorAll('.ai-models select')) body[select.dataset.key] = select.value;
      const out = await run(msg.id, 'Saving…', () => send('POST', '/api/anthropic/prefs', body), 'Model choices saved.');
      if (out && typeof out === 'object') prefs = { ...prefs, ...body };
    });
  }

  // ---------- laundry ----------
  function renderLg(j) {
    $('lg-state').textContent = !j.configured ? 'Not connected.' : j.error ? 'Connected, but the last check failed: ' + j.error : 'Connected. Checked every ' + j.poll_seconds + ' seconds.';
    $('lg-devices').replaceChildren(...(j.devices || []).map((d) => {
      const state = d.disconnected ? 'Offline' : d.activity === 'active' ? 'Running' + (num(d.remaining_minutes) != null ? ', ' + d.remaining_minutes + ' min left' : '') : 'Not running';
      return el('div', { class: 'item' }, el('div', {}, el('b', { text: d.alias || 'Appliance' }), el('span', { text: d.model || '' })),
        el('div', { class: 'item-side' }, el('span', { class: 'pill' + (d.activity === 'active' ? ' on' : ''), text: state })));
    }), ...(j.warnings || []).map((w) => el('div', { class: 'note warn' }, el('span', { text: w }))));
  }
  const loadLg = async () => renderLg(await getJson('/api/lg_thinq/status'));
  $('lg-refresh').addEventListener('click', async () => { const j = await run('lg-msg', 'Checking…', () => send('POST', '/api/lg_thinq/refresh'), 'Checked.'); if (j) renderLg(j); });
  $('lg-save').addEventListener('click', async () => {
    const token = $('lg-token').value.trim(), country = $('lg-country').value.trim().toUpperCase();
    if (!token) { say('lg-msg', 'Paste the LG token first.', 'bad'); return; }
    if (!/^[A-Z]{2}$/.test(country)) { say('lg-msg', 'Country code is two letters, such as US.', 'bad'); return; }
    const j = await run('lg-msg', 'Connecting…', () => send('POST', '/api/lg_thinq/credentials', { token, country_code: country }), 'Connected.');
    if (j) { $('lg-token').value = ''; renderLg(j); }
  });

  // ---------- alerts ----------
  const ALERT_OPTIONS = { threshold_pct: ['at or below', '%', 5, 95], after_min: ['after', 'minutes', 1, 1440] };
  let alertRules = [];
  function renderPush(push) {
    const phones = push.devices || [];
    const ready = phones.filter((d) => d.alerts).length;
    $('al-state').textContent = !push.configured ? 'Not set up yet: add the Apple push key below.'
      : !ready ? 'The key is in place, but no phone is registered yet. Open the Jackery app and allow notifications.'
        : 'Ready: ' + ready + (ready === 1 ? ' phone receives notifications.' : ' phones receive notifications.');
    $('al-phones').replaceChildren(...(phones.length ? phones.map((d) => {
      const remove = el('button', { class: 'btn small', type: 'button', text: 'Remove' });
      remove.addEventListener('click', async () => {
        const j = await run('al-phone-msg', 'Removing…', () => send('DELETE', '/api/app/devices/' + encodeURIComponent(d.install_id)), 'Removed. The phone registers again the next time the app opens.');
        if (j) renderPush(j.push);
      });
      const can = [d.alerts ? 'notifications' : null, d.live_activity ? 'lock-screen status' : null].filter(Boolean).join(' and ') || 'notifications not allowed yet';
      return el('div', { class: 'item' },
        el('div', {}, el('b', { text: d.name || 'iPhone' }), el('div', { class: 'muted small', text: can + ' · last seen ' + when(d.updated_ts) + (d.environment === 'development' ? ' · test build' : '') })),
        el('div', { class: 'item-side' }, remove));
    }) : [el('p', { class: 'zero', text: 'No phones yet.' })]));
    $('al-key-state').textContent = push.configured ? 'Key ' + push.key_id + ' for team ' + push.team_id + ', app ' + push.bundle_id + '.' : 'No key yet. Notifications cannot be sent without it.';
    $('al-key-remove').hidden = !push.configured;
    if (push.configured) { $('al-team').value = push.team_id; $('al-bundle').value = push.bundle_id; }
    else $('al-key-form').open = true;
  }
  async function loadAlerts() {
    const j = await getJson('/api/alerts');
    alertRules = j.rules || [];
    $('al-enabled').checked = !!j.config.enabled;
    $('al-live').checked = !!j.config.live_activity;
    renderPush(j.push || {});
    $('al-rules').replaceChildren(...alertRules.map((r) => {
      const cfg = j.config.rules[r.name] || {};
      const box = el('input', { id: 'al-on-' + r.name, type: 'checkbox' });
      box.checked = !!cfg.enabled;
      const control = el('div', { class: 'f-control' });
      const key = Object.keys(ALERT_OPTIONS).find((k) => k in cfg);
      if (key) {
        const [before, unit, min, max] = ALERT_OPTIONS[key];
        const input = el('input', { id: 'al-opt-' + r.name, type: 'number', min, max, step: '1', inputmode: 'numeric', 'aria-label': r.label + ': ' + before + ' how many ' + unit });
        input.value = cfg[key];
        input.dataset.key = key;
        control.append(el('span', { class: 'f-unit', text: before }), input, el('span', { class: 'f-unit', text: unit }));
      }
      control.append(box);
      return el('div', { class: 'f-row check' + (key ? ' opt' : '') }, el('label', { class: 'f-label', for: 'al-on-' + r.name }, el('b', { text: r.label })), control);
    }));
    const recent = (j.recent || []).slice(0, 15);
    $('al-table').tBodies[0].replaceChildren(...(recent.length ? recent.map((a) => el('tr', {},
      el('td', { class: 'left', text: when(a.ts) }),
      el('td', { class: 'left' }, el('b', { text: a.title }), el('div', { class: 'muted small', text: a.message })),
      el('td', { class: 'left', text: a.delivered ? 'Yes' : 'No: ' + (a.error || 'unknown') })))
      : [el('tr', {}, el('td', { colspan: '3', class: 'zero', text: 'No notifications have been sent yet.' }))]));
  }
  $('al-save').addEventListener('click', async () => {
    const rules = {};
    for (const r of alertRules) {
      rules[r.name] = { enabled: $('al-on-' + r.name).checked };
      const opt = $('al-opt-' + r.name);
      if (!opt) continue;
      const n = Number(opt.value);
      if (opt.value.trim() === '' || !Number.isInteger(n) || n < Number(opt.min) || n > Number(opt.max)) { say('al-msg', r.label + ': enter a whole number from ' + opt.min + ' to ' + opt.max + '.', 'bad'); return; }
      rules[r.name][opt.dataset.key] = n;
    }
    await run('al-msg', 'Saving…', () => send('POST', '/api/alerts/config', { enabled: $('al-enabled').checked, live_activity: $('al-live').checked, rules }),
      () => ($('al-enabled').checked ? 'Saved. Notifications are on.' : 'Saved. Notifications are off.'));
  });
  $('al-test').addEventListener('click', async () => {
    say('al-msg', 'Sending…');
    try {
      const j = await send('POST', '/api/alerts/test');
      say('al-msg', j.ok ? 'Sent. It should reach your phone within a few seconds.' : 'Not sent. ' + (j.error || 'unknown'), j.ok ? 'ok' : 'bad');
    } catch (e) { say('al-msg', 'Not sent: ' + e.message, 'bad'); }
  });
  $('al-key-save').addEventListener('click', async () => {
    const file = $('al-keyfile').files[0];
    if (!file) { say('al-key-msg', 'Choose the .p8 file you downloaded from Apple.', 'bad'); return; }
    if (file.size > 10000) { say('al-key-msg', 'That file is too big to be an Apple push key.', 'bad'); return; }
    const body = { team_id: $('al-team').value.trim().toUpperCase(), key_id: $('al-keyid').value.trim().toUpperCase(), bundle_id: $('al-bundle').value.trim(), key_pem: await file.text() };
    const j = await run('al-key-msg', 'Saving…', () => send('POST', '/api/alerts/push_key', body), 'Key saved.');
    if (j) { $('al-keyfile').value = ''; $('al-keyid').value = ''; $('al-key-form').open = false; renderPush(j.push); }
  });
  $('al-key-remove').addEventListener('click', async () => {
    if (!confirm('Remove the Apple push key? Notifications stop until a key is added again.')) return;
    const j = await run('al-key-msg', 'Removing…', () => send('DELETE', '/api/alerts/push_key'), 'Key removed.');
    if (j) renderPush(j.push);
  });

  // ---------- backup ----------
  const size = (b) => (num(b) == null ? '—' : b >= 1e9 ? (b / 1e9).toFixed(2) + ' GB' : (b / 1e6).toFixed(0) + ' MB');
  async function loadBackup() {
    const j = await getJson('/api/backup/status');
    const r = j.remote || {};
    const where = r.transport === 'smb' ? '\\\\' + r.host + '\\' + r.share + (r.subdir ? '\\' + r.subdir : '') : [r.transport, r.host, r.target_dir || r.rsync_module].filter(Boolean).join(' · ');
    $('bk-state').textContent = j.configured ? 'Backing up daily to ' + where + '. Last good backup: ' + when(j.last_ok_ts) + '.' : 'No backup destination is set up.';
    $('bk-run').disabled = $('bk-test').disabled = !j.configured;
    const runs = (j.runs || []).slice(0, 10);
    $('bk-table').tBodies[0].replaceChildren(...(runs.length ? runs.map((x) => el('tr', {},
      el('td', { text: when(x.ts) }),
      el('td', { text: x.ok ? 'Done' : x.skipped_reason ? 'Skipped: ' + x.skipped_reason : 'Failed: ' + (x.error || 'unknown') }),
      el('td', { text: size(x.bytes_written) }), el('td', { text: num(x.duration_s) == null ? '—' : x.duration_s.toFixed(0) + ' s' })))
      : [el('tr', {}, el('td', { colspan: '4', class: 'zero', text: 'No backups have run yet.' }))]));
  }
  $('bk-run').addEventListener('click', async () => {
    const j = await run('bk-msg', 'Backing up (this can take a minute)…', () => send('POST', '/api/backup/run'), (x) => (x.ok ? 'Backup finished.' : 'Backup did not finish: ' + (x.error || x.skipped_reason || 'unknown')));
    if (j) loadBackup();
  });
  $('bk-test').addEventListener('click', () => run('bk-msg', 'Testing…', () => send('POST', '/api/backup/test'), (x) => (x.ok ? 'The backup destination is reachable.' : 'Could not reach it: ' + (x.error || x.message || 'unknown'))));

  // ---------- device ----------
  async function loadDevice() {
    const st = window.V2.status() || {}, dev = st.device || {}, sn = deviceSn();
    const [auth, cap, params, wd] = await Promise.all([getJson('/api/auth/status').catch(() => ({})), getJson('/api/devices/capacity'), getJson('/api/devices/params' + dq()), getJson('/api/inverter_watchdog/config' + dq()).catch(() => null)]);
    const paused = st.cloud?.pause_remaining_s;
    $('dev-info').replaceChildren(row('Name', dev.name || '—'), row('Serial number', sn || '—'),
      row('Connection', st.connection_status || '—'), row('Jackery cloud', auth.cloud_state || '—'),
      row('Updates', paused ? 'Paused for ' + Math.ceil(paused / 60) + ' more minutes' : 'Running'));
    $('dev-pause').textContent = paused ? 'Resume updates' : 'Pause updates for 10 minutes';
    $('dev-pause').dataset.paused = paused ? '1' : '';
    const mine = (cap.devices || []).find((d) => d.device_sn === sn) || {};
    $('dev-capacity').value = mine.capacity_wh_override ?? '';
    $('dev-capacity-auto').textContent = 'Using ' + (num(mine.effective_capacity_wh) != null ? mine.effective_capacity_wh.toLocaleString('en-US') + ' Wh' : 'an unknown size') +
      (mine.capacity_wh_override ? ' (set by you)' : mine.pack_count ? ' (main unit + ' + mine.pack_count + ' packs)' : '');
    $('dev-watchdog').checked = !!wd?.enabled;
    $('dev-watchdog').disabled = !wd;
    $('dev-params').replaceChildren(...(params.params || []).map((p) => {
      const side = el('div', { class: 'item-side' }, el('span', { class: 'pill', text: num(p.value) == null ? '—' : Math.round(p.value * 100) / 100 + ' ' + (p.unit || '') }));
      if (!p.read_only && p.source === 'fit') {
        const again = el('button', { class: 'btn small', type: 'button', text: 'Measure again' });
        again.addEventListener('click', async () => { if (await run('param-msg', 'Measuring…', () => send('POST', '/api/devices/params/refit', { device_sn: sn, key: p.key }), p.label + ' measured again.')) { loaded.delete('device'); show(); } });
        side.append(again);
      }
      const origin = { fit: 'measured', catalog: 'from the model', override: 'set by you', default: 'default' }[p.source] || p.source || '';
      return el('div', { class: 'item' }, el('div', {}, el('b', { text: p.label }), el('span', { text: [origin, p.note || p.description].filter(Boolean).join(' · ') })), side);
    }));
    const out = el('button', { class: 'btn small', type: 'button', text: 'Sign out of this app' });
    out.addEventListener('click', async () => { await fetch('/api/auth/logout', { method: 'POST' }).catch(() => {}); location.href = '/login'; });
    const forget = el('button', { class: 'btn small', type: 'button', text: 'Disconnect the Jackery account' });
    forget.addEventListener('click', async () => {
      if (!confirm('Disconnect the Jackery account? Live readings and automatic charging stop until you sign in again.')) return;
      if (await run('acct-msg', 'Disconnecting…', () => send('POST', '/api/auth/forget'), 'Disconnected.')) location.href = '/classic';
    });
    $('dev-account').replaceChildren(
      el('div', { class: 'item' }, el('div', {}, el('b', { text: 'Jackery account' }), el('span', { text: (auth.email || 'Signed in') + (auth.region ? ' · region ' + auth.region : '') })), el('div', { class: 'item-side' }, forget)),
      el('div', { class: 'item' }, el('div', {}, el('b', { text: 'This app' }), el('span', { text: 'Signs this browser out of the dashboard.' })), el('div', { class: 'item-side' }, out)));
  }
  $('dev-reconnect').addEventListener('click', () => run('dev-msg', 'Reconnecting…', () => send('POST', '/api/reconnect'), (j) => (j.ok ? 'Reconnected.' : 'Could not reconnect: ' + (j.error || 'unknown'))));
  $('dev-pause').addEventListener('click', async () => {
    const paused = $('dev-pause').dataset.paused === '1';
    if (await run('dev-msg', paused ? 'Resuming…' : 'Pausing…', () => send('POST', paused ? '/api/resume_polling' : '/api/pause_polling', paused ? undefined : { seconds: 600 }), paused ? 'Updates resumed.' : 'Updates paused for 10 minutes.')) {
      $('dev-pause').dataset.paused = paused ? '' : '1';
      $('dev-pause').textContent = paused ? 'Pause updates for 10 minutes' : 'Resume updates';
    }
  });
  $('dev-capacity-save').addEventListener('click', async () => {
    const raw = $('dev-capacity').value.trim(), n = Number(raw);
    if (raw !== '' && !(n >= 500 && n <= 200000)) { say('cap-msg', 'Enter 500 to 200,000 Wh, or leave empty for automatic.', 'bad'); return; }
    if (await run('cap-msg', 'Saving…', () => send('POST', '/api/devices/capacity', { device_sn: deviceSn(), capacity_wh: raw === '' ? null : n }), raw === '' ? 'Back to automatic.' : 'Battery size saved.')) { loaded.delete('device'); show(); }
  });
  $('dev-watchdog').addEventListener('change', async () => {
    const enabled = $('dev-watchdog').checked;
    if (!await run('wd-msg', 'Saving…', () => send('PUT', '/api/inverter_watchdog/config', { device_sn: deviceSn(), enabled }), enabled ? 'AC auto recovery is on.' : 'AC auto recovery is off.')) $('dev-watchdog').checked = !enabled;
  });

  // ---------- logs ----------
  async function loadLogs() {
    const j = await getJson('/api/events?limit=200');
    const only = $('log-problems').checked;
    const events = (j.events || []).slice().reverse().filter((e) => !only || e.level === 'warning' || e.level === 'error');
    $('log-table').tBodies[0].replaceChildren(...(events.length ? events.map((e) => el('tr', {},
      el('td', { class: 'left', text: when(e.ts) }),
      el('td', { class: 'left' }, el('span', { class: 'tag ' + (e.level || ''), text: e.category || e.level || 'event' })),
      el('td', { class: 'wrap', text: e.message || '' })))
      : [el('tr', {}, el('td', { colspan: '3', class: 'zero left', text: only ? 'No problems in the recent log.' : 'The log is empty.' }))]));
  }
  $('log-refresh').addEventListener('click', () => loadLogs().catch(() => {}));
  $('log-problems').addEventListener('change', () => loadLogs().catch(() => {}));

  // ---------- sections ----------
  const LOADERS = { general: loadGeneral, rates: loadRates, ai: loadAi, laundry: loadLg, alerts: loadAlerts, backup: loadBackup, device: loadDevice, logs: loadLogs };
  function show(force) {
    if (force || lastDevice !== deviceSn()) { loaded.delete('device'); lastDevice = deviceSn(); }
    $('settings-sub').textContent = window.V2.status()?.device?.name || '';
    for (const node of document.querySelectorAll('#page-settings > [data-sec]')) node.hidden = node.dataset.sec !== section;
    for (const tab of document.querySelectorAll('#set-tabs button')) tab.setAttribute('aria-selected', String(tab.dataset.sec === section));
    if (loaded.has(section) || (section === 'device' && !deviceSn())) return;
    loaded.add(section);
    LOADERS[section]().catch((e) => {
      loaded.delete(section);
      const card = document.querySelector('#page-settings > [data-sec="' + section + '"] .card');
      card.append(el('p', { class: 'form-msg bad', text: 'Could not load this section: ' + e.message }));
    });
  }
  for (const tab of document.querySelectorAll('#set-tabs button')) tab.addEventListener('click', () => { section = tab.dataset.sec; show(); });

  window.V2.register('settings', { show });
})();
