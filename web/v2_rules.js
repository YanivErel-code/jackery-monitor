/* Jackery Monitor — V2 Rules & plugs page: grid charging, excess solar,
 * smart plugs, battery rules and AI insights. Same endpoints as the classic
 * Automation tab. Nothing saves until its Save button is pressed, and every
 * save shows what the server accepted. */
(function () {
  'use strict';
  const { $, el, num, pct, getJson, deviceSn } = window.V2;

  let smart = null;      // config as the server last returned it
  let solar = null;
  let plugs = [];
  let appliances = [];
  let rules = [];
  let editingRule = null;
  let reviewTimer = null;
  let loadedFor = null;

  const MODES = [['off', 'Off'], ['test', 'Test: log decisions, never switch the plug'], ['active', 'On']];
  const q = () => '?device_sn=' + encodeURIComponent(deviceSn());
  const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : 'never');
  const plugName = (host) => (plugs.find((p) => p.host === host)?.alias || host || 'no plug');

  async function send(method, path, body) {
    const r = await fetch(path, { method, headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (r.status === 401) { location.href = '/login'; throw new Error('signed out'); }
    if (!r.ok) throw new Error(typeof j.detail === 'string' ? j.detail : 'HTTP ' + r.status);
    return j;
  }
  function say(id, text, tone) {
    const node = $(id);
    node.textContent = text;
    node.className = 'form-msg ' + (tone || '');
  }
  async function run(msgId, working, fn, done) {
    say(msgId, working);
    try { const out = await fn(); say(msgId, typeof done === 'function' ? done(out) : done, 'ok'); return out; }
    catch (e) { say(msgId, 'Not saved: ' + e.message, 'bad'); return null; }
  }

  // ---------- form building ----------
  function control(spec, value) {
    const id = 'f-' + spec.form + '-' + spec.key;
    let input;
    if (spec.type === 'select') {
      input = el('select', { id });
      for (const [v, label] of spec.options()) input.append(el('option', { value: v, text: label }));
      input.value = value == null ? '' : String(value);
    } else if (spec.type === 'check') {
      input = el('input', { id, type: 'checkbox' });
      input.checked = !!value;
    } else {
      input = el('input', { id, type: 'number', inputmode: 'decimal', step: spec.step || '1' });
      if (spec.min != null) input.min = spec.min;
      if (spec.max != null) input.max = spec.max;
      const shown = value == null ? '' : spec.scale ? value / spec.scale : value;
      input.value = shown === '' ? '' : String(shown);
    }
    input.dataset.key = spec.key;
    const text = el('label', { class: 'f-label', for: id }, el('b', { text: spec.label }), spec.hint ? el('span', { text: spec.hint }) : null);
    const side = el('div', { class: 'f-control' }, input, spec.unit ? el('span', { class: 'f-unit', text: spec.unit }) : null);
    return el('div', { class: 'f-row' + (spec.type === 'check' ? ' check' : '') }, text, side);
  }
  function readForm(root, specs) {
    const out = {};
    for (const spec of specs) {
      const input = root.querySelector('[data-key="' + spec.key + '"]');
      if (!input) continue;
      if (spec.type === 'check') out[spec.key] = input.checked;
      else if (spec.type === 'select') out[spec.key] = input.value === '' ? null : input.value;
      else if (input.value.trim() === '') { if (spec.blank) out[spec.key] = null; else throw new Error(spec.label + ' needs a number'); }
      else {
        const n = Number(input.value);
        if (!isFinite(n)) throw new Error(spec.label + ' needs a number');
        if ((spec.min != null && n < spec.min) || (spec.max != null && n > spec.max)) throw new Error(spec.label + ' must be between ' + spec.min + ' and ' + spec.max);
        out[spec.key] = spec.scale ? Math.round(n * spec.scale) : n;
      }
    }
    return out;
  }
  const plugOptions = () => [['', 'No plug chosen'], ...plugs.map((p) => [p.host, p.alias + ' (' + p.host + ')'])];

  // ---------- grid charging ----------
  const SMART = [
    { key: 'mode', type: 'select', label: 'Automatic grid charging', options: () => MODES },
    { key: 'kasa_device_host', type: 'select', label: 'Charger plug', hint: 'The smart plug the Jackery charger is plugged into.', options: plugOptions },
    { key: 'target_sunrise_soc_pct', label: 'Battery level wanted at sunrise', unit: '%', min: 0, max: 100 },
    { key: 'reserve_soc_pct', label: 'Never go below', hint: 'The plan charges at any rate if the battery would otherwise fall under this.', unit: '%', min: 0, max: 95 },
    { key: 'ac_supply_limit_w', label: 'Circuit limit', hint: 'Most the wall circuit should carry. Leave empty for no limit.', unit: 'W', min: 50, max: 20000, blank: true },
    { key: 'max_on_duration_minutes', label: 'Most charging in one day', unit: 'hours', scale: 60, step: '0.5', min: 0.5, max: 24 },
    { key: 'ac_input_excludes_load', type: 'check', label: 'Grid power goes to the battery only', hint: 'On when the home runs from the Jackery while it charges, so grid watts do not include the home.' },
    { key: 'claude_enabled', type: 'check', label: 'AI explanation on each decision', hint: 'Uses the AI provider chosen in Settings.' },
    { key: 'lg_interlock_enabled', type: 'check', label: 'Hold charging during laundry', hint: 'Keeps the charger off while a chosen washer or dryer is running.' },
    { key: 'lg_manual_hold', type: 'check', label: 'Hold charging now', hint: 'A manual hold. Charging stays off until you turn this off.' },
  ].map((s) => ({ ...s, form: 'smart' }));

  function renderSmart() {
    const box = $('smart-form');
    if (!smart) { box.replaceChildren(el('p', { class: 'muted', text: 'Loading…' })); return; }
    const picked = new Set(smart.lg_device_ids || []);
    const list = el('div', { class: 'f-row' },
      el('div', { class: 'f-label' }, el('b', { text: 'Appliances that hold charging' }),
        el('span', { text: appliances.length ? 'Charging waits while any ticked appliance runs.' : 'No LG appliances found. Connect LG ThinQ under Settings.' })),
      el('div', { class: 'f-checks', id: 'smart-appliances' }, ...appliances.map((a) => {
        const box2 = el('input', { type: 'checkbox', value: a.device_id, id: 'lg-' + a.device_id.slice(0, 8) });
        box2.checked = picked.has(a.device_id);
        return el('label', { for: box2.id }, box2, ' ' + (a.alias || 'Appliance'));
      })));
    box.replaceChildren(...SMART.map((s) => control(s, smart[s.key])), list);
    $('smart-rate').textContent = num(smart.max_charge_w) != null ? 'Measured charging power: ' + Math.round(smart.max_charge_w).toLocaleString('en-US') + ' W (learned automatically).' : '';
  }
  async function saveSmart() {
    let edits;
    try { edits = readForm($('smart-form'), SMART); } catch (e) { say('smart-msg', e.message, 'bad'); return; }
    edits.lg_device_ids = [...$('smart-appliances').querySelectorAll('input:checked')].map((i) => i.value);
    if (edits.mode === 'active' && smart.mode !== 'active' &&
        !confirm('Turn on automatic grid charging? The charger plug will be switched on and off for you.')) return;
    // The server replaces the stored config with what it is sent, so send every key.
    const body = { ...smart, ...edits };
    delete body.max_charge_w;
    const out = await run('smart-msg', 'Saving…', () => send('POST', '/api/smart_charge/config' + q(), body), (j) => {
      const clash = j.conflicting_rules || [];
      return clash.length ? 'Saved. Note: ' + clash.length + ' battery rule(s) also switch this plug and may fight the plan.' : 'Saved.';
    });
    if (out) { smart = out.config; renderSmart(); }
  }

  // ---------- excess solar ----------
  const SOLAR_MAIN = [
    { key: 'mode', type: 'select', label: 'Send spare solar to a plug', options: () => MODES },
    { key: 'kasa_device_host', type: 'select', label: 'Plug for the extra load', hint: 'Must be plugged into the Jackery, so the load runs from the battery.', options: plugOptions },
    { key: 'car_load_w', label: 'Power the load draws', hint: 'A level 1 car charger is about 1,400 W.', unit: 'W', min: 0 },
    { key: 'comfort_high_pct', label: 'Battery needed to start', hint: 'A stopped session will not restart until the battery is back at this level.', unit: '%', min: 0, max: 100 },
    { key: 'comfort_low_pct', label: 'Always stop at', hint: 'Turns the plug off at this battery level whatever the forecast says.', unit: '%', min: 0, max: 100 },
  ];
  const SOLAR_ADV = [
    { key: 'safety_margin_pp', label: 'Sunrise safety margin', hint: 'Forecast sunrise level must stay this far above your target while diverting.', unit: 'points', min: 0 },
    { key: 'on_hysteresis_pp', label: 'Extra margin before turning on', hint: 'Stops the plug flipping when the forecast wobbles.', unit: 'points', min: 0 },
    { key: 'min_hold_s', label: 'Shortest time between switches', unit: 'seconds', min: 0 },
    { key: 'max_system_load_w', label: 'Do not start above this home load', unit: 'W', min: 0 },
    { key: 'inverter_protect_load_w', label: 'Overload cut-off', hint: 'Plug goes off at once if total output reaches this.', unit: 'W', min: 0 },
    { key: 'inverter_protect_cooldown_s', label: 'Wait after an overload', unit: 'seconds', min: 0 },
    { key: 'no_load_threshold_w', label: 'Load counts as running above', unit: 'W', min: 0 },
    { key: 'no_load_verify_delay_s', label: 'Time to wait for the load to start', unit: 'seconds', min: 0 },
    { key: 'no_load_cooldown_s', label: 'Wait after the load fails to start', unit: 'seconds', min: 0 },
    { key: 'balance_spread_trigger_pp', label: 'Balance packs when they differ by', hint: 'Diversion pauses so solar can take the main unit to 100% and the packs can balance.', unit: 'points', min: 0 },
    { key: 'balance_every_days', label: 'Balance at least every', hint: '0 turns this off.', unit: 'days', min: 0 },
    { key: 'balance_max_window_days', label: 'Give up balancing after', unit: 'days', min: 0 },
    { key: 'balance_target_main_pct', label: 'Main unit level that counts as full', unit: '%', min: 0, max: 100 },
  ];
  const SOLAR = [...SOLAR_MAIN, ...SOLAR_ADV].map((s) => ({ ...s, form: 'solar' }));

  function renderSolar(statusPayload) {
    if (!solar) { $('solar-form').replaceChildren(el('p', { class: 'muted', text: 'Loading…' })); return; }
    const main = SOLAR.slice(0, SOLAR_MAIN.length), adv = SOLAR.slice(SOLAR_MAIN.length);
    const open = $('solar-form').querySelector('details')?.open;
    const more = el('details', {}, el('summary', { text: 'Advanced settings' }), ...adv.map((s) => control(s, solar[s.key])));
    more.open = !!open;
    $('solar-form').replaceChildren(...main.map((s) => control(s, solar[s.key])), more);
    if (statusPayload) {
      const last = (statusPayload.recent_decisions || [])[0];
      $('solar-status').textContent = 'Plug is ' + (statusPayload.runtime?.plug_is_on ? 'on' : 'off') + ' · ' +
        ((statusPayload.diverted_wh_today || 0) / 1000).toFixed(1) + ' kWh diverted today' + (last ? ' · Latest: ' + last.reason : '');
    }
  }
  async function saveSolar() {
    let edits;
    try { edits = readForm($('solar-form'), SOLAR); } catch (e) { say('solar-msg', e.message, 'bad'); return; }
    if (edits.mode === 'active' && solar.mode !== 'active' &&
        !confirm('Turn on excess-solar diversion? The chosen plug will be switched on and off for you.')) return;
    const out = await run('solar-msg', 'Saving…', () => send('POST', '/api/solar_charge/config' + q(), edits), 'Saved.');
    if (out) { solar = out.config; renderSolar(); }
  }

  // ---------- plugs ----------
  function renderPlugs() {
    const used = (host) => [smart?.kasa_device_host === host ? 'grid charging' : null, solar?.kasa_device_host === host ? 'excess solar' : null].filter(Boolean);
    $('plug-list').replaceChildren(...(plugs.length ? plugs.map((p) => {
      const roles = used(p.host);
      const state = p.online === false ? 'Offline' : p.is_on ? 'On' : 'Off';
      const toggle = el('button', { class: 'btn small', type: 'button', text: p.is_on ? 'Turn off' : 'Turn on' });
      toggle.disabled = p.online === false;
      toggle.addEventListener('click', async () => {
        const on = !p.is_on;
        const warn = roles.length ? ' This plug is used for ' + roles.join(' and ') + ', so the controller may switch it back, and turning it on by hand ignores the laundry hold.' : '';
        if (!confirm('Turn ' + p.alias + (on ? ' on?' : ' off?') + warn)) return;
        if (await run('plug-msg', 'Switching ' + p.alias + '…', () => send('POST', '/api/kasa/test', { host: p.host, on }), p.alias + ' is now ' + (on ? 'on.' : 'off.'))) loadPlugs(true);
      });
      const remove = el('button', { class: 'btn small', type: 'button', text: 'Remove' });
      remove.addEventListener('click', async () => {
        if (!confirm('Remove ' + p.alias + ' from this app?' + (roles.length ? ' It is used for ' + roles.join(' and ') + '.' : ''))) return;
        if (await run('plug-msg', 'Removing…', () => send('DELETE', '/api/kasa/saved/' + encodeURIComponent(p.host)), 'Removed.')) loadPlugs();
      });
      return el('div', { class: 'item' },
        el('div', {}, el('b', { text: p.alias || p.host }),
          el('span', { text: [p.model, p.host, roles.length ? 'Used for ' + roles.join(' and ') : 'Not used by a controller'].filter(Boolean).join(' · ') })),
        el('div', { class: 'item-side' }, el('span', { class: 'pill ' + (p.online === false ? 'bad' : p.is_on ? 'on' : ''), text: state }), toggle, remove));
    }) : [el('p', { class: 'muted', text: 'No plugs saved yet.' })]));
  }
  async function loadPlugs(refresh) {
    try { plugs = (await getJson('/api/kasa/saved' + (refresh ? '?refresh=true' : ''))).devices || []; } catch (e) { say('plug-msg', 'Could not load plugs: ' + e.message, 'bad'); }
    renderPlugs(); renderSmart(); renderSolar(); renderRules();
  }
  async function addPlug(host, alias) {
    if (!host) { say('plug-msg', 'Enter the plug\'s IP address.', 'bad'); return; }
    if (await run('plug-msg', 'Checking the plug…', () => send('POST', '/api/kasa/saved', { host, alias, jackery_device_sn: deviceSn() }), 'Plug added.')) {
      $('plug-host').value = ''; $('plug-alias').value = ''; $('plug-found').replaceChildren(); loadPlugs();
    }
  }
  async function findPlugs() {
    const found = await run('plug-msg', 'Searching the network (this can take a few seconds)…', () => getJson('/api/kasa/devices'), (j) => (j.devices || []).length + ' plug(s) found.');
    if (!found) return;
    const known = new Set(plugs.map((p) => p.host));
    $('plug-found').replaceChildren(...(found.devices || []).filter((d) => !known.has(d.host)).map((d) => {
      const add = el('button', { class: 'btn small', type: 'button', text: 'Add' });
      add.addEventListener('click', () => addPlug(d.host, d.alias || ''));
      return el('div', { class: 'item' }, el('div', {}, el('b', { text: d.alias || d.host }), el('span', { text: [d.model, d.host].filter(Boolean).join(' · ') })), el('div', { class: 'item-side' }, add));
    }));
  }
  async function loadAccount() {
    try {
      const j = await getJson('/api/kasa/credentials');
      $('kasa-account').textContent = j.has_credentials ? 'Signed in to Kasa as ' + j.email + '.' : 'No Kasa account saved. Newer plugs need one.';
      $('kasa-forget').hidden = !j.has_credentials;
    } catch (e) { $('kasa-account').textContent = ''; }
  }

  // ---------- battery rules ----------
  function ruleText(r) {
    return 'When the battery ' + (r.operator === '<=' ? 'falls to ' : 'rises to ') + Math.round(r.value) + '%, turn ' + (r.kasa_alias || plugName(r.kasa_host)) + ' ' + r.action;
  }
  function renderRules() {
    $('rule-list').replaceChildren(...(rules.length ? rules.map((r) => {
      const sw = el('button', { class: 'switch', type: 'button', role: 'switch', 'aria-checked': String(!!r.enabled), 'aria-label': 'Rule ' + r.name + ' enabled' });
      sw.addEventListener('click', async () => {
        if (await run('rule-msg', 'Saving…', () => send('POST', '/api/automation/rules', { ...r, enabled: !r.enabled }), r.enabled ? 'Rule turned off.' : 'Rule turned on.')) loadRules();
      });
      const edit = el('button', { class: 'btn small', type: 'button', text: 'Edit' });
      edit.addEventListener('click', () => { editingRule = r; renderRuleEditor(); $('rule-editor').scrollIntoView({ block: 'nearest' }); });
      const del = el('button', { class: 'btn small', type: 'button', text: 'Delete' });
      del.addEventListener('click', async () => {
        if (!confirm('Delete the rule "' + r.name + '"?')) return;
        if (await run('rule-msg', 'Deleting…', () => send('DELETE', '/api/automation/rules/' + encodeURIComponent(r.id)), 'Rule deleted.')) loadRules();
      });
      return el('div', { class: 'item' },
        el('div', {}, el('b', { text: r.name || ruleText(r) }), el('span', { text: ruleText(r) + ' · Last ran ' + when(r.last_fired) + (r.last_error ? ' · Error: ' + r.last_error : '') })),
        el('div', { class: 'item-side' }, sw, edit, del));
    }) : [el('p', { class: 'muted', text: 'No rules yet.' })]));
  }
  function renderRuleEditor() {
    const r = editingRule;
    $('rule-editor').hidden = !r;
    if (!r) return;
    $('rule-editor-title').textContent = r.id ? 'Edit rule' : 'New rule';
    $('rule-name').value = r.name || '';
    $('rule-op').value = r.operator || '<=';
    $('rule-value').value = r.value ?? 20;
    $('rule-action').value = r.action || 'on';
    $('rule-plug').replaceChildren(...plugs.map((p) => el('option', { value: p.host, text: p.alias })));
    if (r.kasa_host) $('rule-plug').value = r.kasa_host;
  }
  async function saveRule() {
    const value = Number($('rule-value').value);
    if ($('rule-value').value.trim() === '' || !(value >= 0 && value <= 100)) { say('rule-msg', 'Battery level must be between 0 and 100.', 'bad'); return; }
    if (!$('rule-plug').value) { say('rule-msg', 'Add a plug first.', 'bad'); return; }
    const body = { ...editingRule, name: $('rule-name').value.trim(), trigger: 'battery_percent', operator: $('rule-op').value, value,
      action: $('rule-action').value, kasa_host: $('rule-plug').value, kasa_alias: plugName($('rule-plug').value),
      jackery_device_sn: editingRule.jackery_device_sn || deviceSn(), enabled: editingRule.enabled ?? true };
    if (!body.name) body.name = ruleText(body);
    if (await run('rule-msg', 'Saving…', () => send('POST', '/api/automation/rules', body), 'Rule saved.')) { editingRule = null; renderRuleEditor(); loadRules(); }
  }
  async function loadRules() {
    try { rules = (await getJson('/api/automation/rules')).rules || []; } catch (e) { say('rule-msg', 'Could not load rules: ' + e.message, 'bad'); }
    renderRules();
  }

  // ---------- AI insights ----------
  const show = (v) => (v === null || v === undefined ? 'not set' : typeof v === 'boolean' ? (v ? 'on' : 'off') : String(v));
  async function loadInsights() {
    try {
      const [s, c] = await Promise.all([getJson('/api/algorithm/suggestions' + q()), getJson('/api/algorithm/changes' + q())]);
      const list = s.suggestions || [];
      $('ai-suggestions').replaceChildren(...(list.length ? list.map((x) => {
        const side = el('div', { class: 'item-side' });
        if (x.kind !== 'anomaly') {
          const apply = el('button', { class: 'btn small', type: 'button', text: 'Apply' });
          apply.addEventListener('click', async () => {
            if (!confirm('Change ' + x.target + ' from ' + show(x.current_value) + ' to ' + show(x.proposed_value) + '?')) return;
            if (await run('ai-msg', 'Applying…', () => send('POST', '/api/algorithm/suggestions/' + x.id + '/apply'), 'Applied.')) { loadInsights(); load(true); }
          });
          side.append(apply);
        }
        const dismiss = el('button', { class: 'btn small', type: 'button', text: 'Dismiss' });
        dismiss.addEventListener('click', async () => { if (await run('ai-msg', 'Dismissing…', () => send('POST', '/api/algorithm/suggestions/' + x.id + '/dismiss'), 'Dismissed.')) loadInsights(); });
        side.append(dismiss);
        const head = x.kind === 'anomaly' ? 'Something to look at' : 'Suggested: ' + x.target + ' → ' + show(x.proposed_value);
        return el('div', { class: 'item' }, el('div', {}, el('b', { text: head }), el('span', { text: x.reasoning || '' })), side);
      }) : [el('p', { class: 'muted', text: 'No suggestions waiting.' })]));
      const changes = (c.changes || []).slice().sort((a, b) => b.applied_at - a.applied_at).slice(0, 5);
      $('ai-changes').replaceChildren(...(changes.length ? changes.map((x) => el('div', { class: 'item' },
        el('div', {}, el('b', { text: x.target + ': ' + show(x.old_value) + ' → ' + show(x.new_value) }), el('span', { text: when(x.applied_at) + (x.reasoning ? ' · ' + x.reasoning : '') }))))
        : [el('p', { class: 'muted', text: 'No changes have been applied.' })]));
    } catch (e) { say('ai-msg', 'Could not load insights: ' + e.message, 'bad'); }
  }
  async function pollReview() {
    clearTimeout(reviewTimer);
    let j;
    try { j = await getJson('/api/algorithm/review_status' + q()); } catch (e) { return; }
    $('ai-run').disabled = j.status === 'running';
    if (j.status === 'running') { say('ai-msg', 'Review running…'); reviewTimer = setTimeout(pollReview, 5000); }
    else if (j.status === 'error') say('ai-msg', 'The last review failed: ' + (j.error || 'unknown error') + ' You can change the AI provider under Settings.', 'bad');
    else if (j.status === 'done') { say('ai-msg', 'Last review finished ' + when(j.finished_at || j.started_at) + '.', 'ok'); loadInsights(); }
  }

  // ---------- wiring ----------
  $('smart-save').addEventListener('click', saveSmart);
  $('solar-save').addEventListener('click', saveSolar);
  $('plug-add').addEventListener('click', () => addPlug($('plug-host').value.trim(), $('plug-alias').value.trim()));
  $('plug-find').addEventListener('click', findPlugs);
  $('plug-refresh').addEventListener('click', () => loadPlugs(true));
  $('kasa-save').addEventListener('click', async () => {
    const email = $('kasa-email').value.trim(), password = $('kasa-password').value;
    if (!email || !password) { say('plug-msg', 'Enter the Kasa email and password.', 'bad'); return; }
    if (await run('plug-msg', 'Saving the account…', () => send('POST', '/api/kasa/credentials', { email, password }), 'Kasa account saved.')) { $('kasa-password').value = ''; loadAccount(); }
  });
  $('kasa-forget').addEventListener('click', async () => {
    if (!confirm('Remove the saved Kasa account? Newer plugs will stop responding until you sign in again.')) return;
    if (await run('plug-msg', 'Removing…', () => send('DELETE', '/api/kasa/credentials'), 'Kasa account removed.')) loadAccount();
  });
  $('rule-new').addEventListener('click', () => { editingRule = { enabled: true }; renderRuleEditor(); });
  $('rule-save').addEventListener('click', saveRule);
  $('rule-cancel').addEventListener('click', () => { editingRule = null; renderRuleEditor(); });
  $('ai-run').addEventListener('click', async () => {
    if (await run('ai-msg', 'Starting a review…', () => send('POST', '/api/algorithm/review_now' + q()), 'Review running…')) pollReview();
  });

  async function load(force) {
    const sn = deviceSn();
    if (!sn || (!force && loadedFor === sn)) return;
    loadedFor = sn;
    $('rules-sub').textContent = window.V2.status()?.device?.name || '';
    getJson('/api/lg_thinq/status').then((j) => {
      appliances = j.devices || [];
      renderSmart();
    }).catch(() => {});
    getJson('/api/smart_charge/config' + q()).then((j) => { smart = j.config; renderSmart(); renderPlugs(); }).catch((e) => say('smart-msg', 'Could not load: ' + e.message, 'bad'));
    getJson('/api/solar_charge/status' + q()).then((j) => { solar = j.config; renderSolar(j); renderPlugs(); }).catch((e) => say('solar-msg', 'Could not load: ' + e.message, 'bad'));
    loadPlugs(); loadAccount(); loadRules(); loadInsights(); pollReview();
  }

  window.V2.register('rules', { show: load });
})();
