'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');

function harness(save = async () => ({})) {
  const start = source.indexOf('function createControllerAutosave(');
  assert.ok(start >= 0, 'controller auto-save coordinator exists');
  const end = source.indexOf('\n}', start) + 2;
  const context = vm.createContext({ setTimeout, clearTimeout });
  vm.runInContext(source.slice(start, end), context);
  const timers = new Map();
  const notices = [];
  const saved = [];
  let timerId = 0;
  const controller = context.createControllerAutosave({ save,
    notify: (...args) => notices.push(args), onSaved: (...args) => saved.push(args),
    setTimer: callback => { timers.set(++timerId, callback); return timerId; },
    clearTimer: id => timers.delete(id),
  });
  const flush = async () => {
    for (const [id, callback] of [...timers]) { timers.delete(id); await callback(); }
    await new Promise(setImmediate);
  };
  return { controller, flush, notices, saved };
}

test('typing coalesces to the latest settings without a Save click', async () => {
  const calls = [];
  const page = harness(async (...args) => { calls.push(args); return {}; });
  page.controller.update('A', { reserve_soc_pct: 1 });
  page.controller.update('A', { reserve_soc_pct: 15 });
  assert.equal(calls.length, 0);
  await page.flush();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][1].reserve_soc_pct, 15);
  assert.equal(page.controller.pending(), false);
});

test('toggles and selections save immediately and retain zero values', async () => {
  const calls = [];
  const page = harness(async (...args) => { calls.push(args); return {}; });
  await page.controller.update('A', { balance_every_days: 0, mode: 'off' }, { immediate: true });
  assert.equal(calls.length, 1);
  assert.equal(calls[0][1].balance_every_days, 0);
});

test('in-flight saves serialize and never acknowledge a newer draft prematurely', async () => {
  const calls = [];
  let release;
  const page = harness((device, config) => {
    calls.push(config.reserve_soc_pct);
    return calls.length === 1 ? new Promise(resolve => { release = resolve; }) : Promise.resolve({});
  });
  const first = page.controller.update('A', { reserve_soc_pct: 10 }, { immediate: true });
  page.controller.update('A', { reserve_soc_pct: 20 }, { immediate: true });
  assert.deepEqual(calls, [10]);
  assert.equal(page.saved.length, 0);
  release({});
  await first;
  await new Promise(setImmediate);
  assert.deepEqual(calls, [10, 20]);
  assert.equal(page.saved.length, 1);
  assert.equal(page.controller.pending(), false);
});

test('device switch cannot redirect a queued save to the newly selected Jackery', async () => {
  const calls = [];
  const page = harness(async (device, config) => { calls.push([device, config.mode]); return {}; });
  page.controller.update('OLD', { mode: 'test' });
  page.controller.update('NEW', { mode: 'off' });
  await page.flush();
  assert.deepEqual(calls, [['OLD', 'test'], ['NEW', 'off']]);
});

test('failures retain the draft and require an explicit retry or new edit', async () => {
  let fail = true;
  let calls = 0;
  const page = harness(async () => { calls++; if (fail) throw new Error('HTTP 503'); return {}; });
  await page.controller.update('A', { reserve_soc_pct: 21 }, { immediate: true });
  assert.equal(page.controller.draft('A').reserve_soc_pct, 21);
  assert.equal(page.controller.pending(), true);
  assert.equal(calls, 1);
  assert.equal(page.notices.at(-1)[2], true);
  fail = false;
  await page.controller.retry('A');
  assert.equal(calls, 2);
  assert.equal(page.controller.pending(), false);
});

test('invalid edits cancel queued writes and cannot be reported as saved', async () => {
  const calls = [];
  const page = harness(async (...args) => { calls.push(args); return {}; });
  page.controller.update('A', { reserve_soc_pct: 10 });
  page.controller.invalidate('A', 'Invalid reserve — not saved');
  await page.flush();
  assert.equal(calls.length, 0);
  assert.equal(page.controller.pending(), true);
  assert.match(page.notices.at(-1)[1], /Invalid reserve/);
});

test('both Controllers forms remove Save buttons but other tabs keep theirs', () => {
  for (const id of ['sc', 'solar']) {
    const form = html.match(new RegExp(`<form id="${id}-form"[\\s\\S]*?</form>`))[0];
    assert.doesNotMatch(form, /type="submit"[^>]*>Save/);
    assert.match(form, new RegExp(`id="${id}-save-status"`));
    assert.match(form, /aria-live="polite"/);
  }
  assert.match(html, /id="kasa-creds-form"[\s\S]*?Save credentials/);
  assert.match(html, /id="settings-form"[\s\S]*?type="submit">Save/);
});

function formHarness() {
  const elements = new Map();
  const timers = new Map();
  const posts = [];
  const lifecycle = {};
  const page = { deviceSn: 'A', home: false, fail: false, invalidations: 0, conflicts: [] };
  let timerId = 0;
  function element(id) {
    if (!elements.has(id)) {
      let value = '';
      const attributes = {};
      const field = { id, dataset: {}, listeners: {}, hidden: false, checked: false,
        textContent: '', options: [], children: [], innerHTML: '',
        addEventListener(name, listener) { this.listeners[name] = listener; },
        setAttribute(name, content) { attributes[name] = content; },
        getAttribute(name) { return attributes[name]; },
        checkValidity() {
          const number = Number(value);
          const minimum = Number(attributes.min || 0);
          const step = Number(attributes.step || 1);
          return Number.isFinite(number) && (!attributes.min || number >= minimum)
            && (!attributes.max || number <= Number(attributes.max))
            && Math.abs((number - minimum) / step - Math.round((number - minimum) / step)) < 1e-8;
        },
        closest: () => null,
        appendChild(child) { this.children.push(child); this.options.push(child); },
      };
      Object.defineProperty(field, 'value', { get: () => value, set: content => { value = String(content); } });
      elements.set(id, field);
    }
    return elements.get(id);
  }
  for (const match of html.matchAll(/<input\b[^>]*id="([^"]+)"[^>]*>/g)) {
    const field = element(match[1]);
    for (const attribute of match[0].matchAll(/(min|max|step|value)="([^"]+)"/g)) {
      field.setAttribute(attribute[1], attribute[2]);
      if (attribute[1] === 'value') field.value = attribute[2];
    }
  }
  for (const kind of ['sc', 'solar']) {
    element(`${kind}-form`).dataset.loadedDeviceSn = 'A';
    element(`${kind}-mode`).value = 'test';
    element(`${kind}-kasa-host`).value = '192.0.2.10';
  }
  page.lg = { lg_interlock_enabled: true, lg_manual_hold: false, lg_device_ids: ['oven'] };
  page.configs = { sc: { mode: 'off', reserve_soc_pct: 10 }, solar: { mode: 'off', car_load_w: 1400 } };
  page.fetch = async (url, options = {}) => {
    const kind = url.includes('/solar_charge/') ? 'solar' : 'sc';
    if (options.method === 'POST') {
      const config = JSON.parse(options.body);
      posts.push({ url, config });
      if (page.fail) return { ok: false, status: 503, json: async () => ({ detail: 'Unavailable' }) };
      return { ok: true, json: async () => ({ config, conflicting_rules: ['conflict'] }) };
    }
    return { ok: true, json: async () => url.includes('/config?') ? { config: page.configs[kind] } : {} };
  };
  const context = vm.createContext({ $: element, document: { getElementById: element,
    createElement: () => ({}) }, activeJackeryDevice: () => ({ device_sn: page.deviceSn }),
    isHomeMonitoring: () => page.home, AbortController,
    fetch: (...args) => page.fetch(...args), console: { error() {}, warn() {} },
    setTimeout: callback => { timers.set(++timerId, callback); return timerId; },
    clearTimeout: id => timers.delete(id),
    window: { LGThinQ: { config: () => ({ ...page.lg, lg_interlock_enabled: element('sc-lg-enabled').checked,
      lg_manual_hold: element('sc-lg-hold').checked }), hydrate() {} },
    addEventListener: (name, listener) => { lifecycle[name] = listener; } },
    renderSmartChargeConflicts: (...args) => page.conflicts.push(args),
    invalidateForecastRequests: () => { page.invalidations++; },
    renderSmartChargeRate() {}, applyClaudeToggleGate() {}, renderSmartChargeHistory() {},
    renderSmartChargeAnalytics() {}, renderCollapsedHistory() {},
  });
  const start = source.indexOf('let _chargeRateRefreshPending =');
  const end = source.indexOf('function renderSmartChargeRate(', start);
  vm.runInContext(source.slice(start, end), context);
  for (const name of ['loadSmartCharge', 'loadSolarCharge']) {
    const begin = source.indexOf(`async function ${name}(`);
    vm.runInContext(source.slice(begin, source.indexOf('\n}', begin) + 2), context);
  }
  return Object.assign(page, { context, element, posts, lifecycle,
    async edit(kind, id, value, type = 'change') {
      const field = element(id);
      if (typeof value === 'boolean') field.checked = value;
      else field.value = value;
      return element(`${kind}-form`).listeners[type]({ type, target: field });
    },
    async flush() {
      for (const [id, callback] of [...timers]) { timers.delete(id); await callback(); }
      await new Promise(setImmediate);
    },
  });
}

test('every numeric Controller setting is wired to auto-save, preserving zero', async () => {
  const page = formHarness();
  const fields = vm.runInContext('_controllerNumericFields', page.context);
  for (const [kind, mapping] of Object.entries(fields)) {
    for (const [key, id] of Object.entries(mapping)) {
      const field = page.element(id);
      const value = Number(field.getAttribute('min'));
      await page.edit(kind, id, value, 'input');
      await page.flush();
      assert.equal(page.posts.at(-1).config[key], value, `${id} saves without submitting`);
      assert.match(page.posts.at(-1).url, new RegExp(`/api/${kind === 'sc' ? 'smart' : 'solar'}_charge/config\\?device_sn=A`));
      assert.equal(page.element(`${kind}-save-status`).textContent, 'Saved automatically.');
    }
  }
});

test('mode, plug, narration and LG changes save immediately from form change events', async () => {
  const page = formHarness();
  for (const kind of ['sc', 'solar']) {
    await page.edit(kind, `${kind}-mode`, 'active');
    assert.equal(page.posts.at(-1).config.mode, 'active');
    await page.edit(kind, `${kind}-kasa-host`, '192.0.2.11');
    assert.equal(page.posts.at(-1).config.kasa_device_host, '192.0.2.11');
  }
  await page.edit('sc', 'sc-claude-toggle', true);
  assert.equal(page.posts.at(-1).config.claude_enabled, true);
  await page.edit('sc', 'sc-lg-enabled', true);
  assert.equal(page.posts.at(-1).config.lg_interlock_enabled, true);
  await page.edit('sc', 'sc-lg-hold', true);
  assert.equal(page.posts.at(-1).config.lg_manual_hold, true);
  page.element('appliance-choice').closest = selector => selector === '#sc-lg-devices';
  page.lg.lg_device_ids = ['oven', 'air-conditioner'];
  await page.edit('sc', 'appliance-choice', true);
  assert.deepEqual(page.posts.at(-1).config.lg_device_ids, ['oven', 'air-conditioner']);
  assert.equal(page.conflicts.at(-1)[1], 'active');
  assert.equal(page.invalidations, page.posts.length);
});

test('backtest fields and submit events do not save Controller configuration', async () => {
  const page = formHarness();
  await page.edit('sc', 'sc-backtest-days', 3);
  await page.edit('sc', 'sc-backtest-target', 44);
  let prevented = false;
  page.element('sc-form').listeners.submit({ preventDefault() { prevented = true; } });
  await page.flush();
  assert.equal(prevented, true);
  assert.equal(page.posts.length, 0);
});

test('blank, out-of-range and step-mismatched settings never replace a saved value', async () => {
  const page = formHarness();
  for (const value of ['', 96, 'not-a-number', 12.5]) {
    await page.edit('sc', 'sc-reserve-soc', value, 'input');
    await page.flush();
    assert.equal(page.posts.length, 0);
    assert.equal(page.element('sc-reserve-soc').getAttribute('aria-invalid'), 'true');
    assert.match(page.element('sc-save-status').textContent, /not saved/);
  }
  await page.edit('sc', 'sc-reserve-soc', 0);
  assert.equal(page.posts[0].config.reserve_soc_pct, 0);
  assert.equal(page.element('sc-reserve-soc').getAttribute('aria-invalid'), 'false');
});

test('diversion rejects an unreachable restart/floor pair before the API resets it', async () => {
  const page = formHarness();
  await page.edit('solar', 'solar-comfort-low', 80);
  assert.equal(page.posts.length, 0);
  assert.equal(page.element('solar-comfort-low').getAttribute('aria-invalid'), 'true');
  assert.match(page.element('solar-save-status').textContent, /floor.*restart|restart.*floor/i);
  await page.edit('solar', 'solar-comfort-high', 90);
  assert.equal(page.posts[0].config.comfort_low_pct, 80);
  assert.equal(page.posts[0].config.comfort_high_pct, 90);
});

test('both Controllers reject edits until their own selected device has loaded', async () => {
  const page = formHarness();
  page.deviceSn = 'OTHER';
  await page.edit('sc', 'sc-reserve-soc', 12);
  await page.edit('solar', 'solar-mode', 'active');
  assert.equal(page.posts.length, 0);
  for (const kind of ['sc', 'solar']) assert.match(page.element(`${kind}-save-status`).textContent, /load/);
  page.deviceSn = 'A';
  page.home = true;
  await page.edit('solar', 'solar-mode', 'active');
  assert.equal(page.posts.length, 0);
});

test('failed saves keep edits visible, expose Retry and protect pending navigation', async () => {
  const page = formHarness();
  page.fail = true;
  await page.edit('sc', 'sc-reserve-soc', 23);
  assert.equal(page.element('sc-reserve-soc').value, '23');
  assert.match(page.element('sc-save-status').textContent, /Could not confirm/);
  assert.equal(page.element('sc-save-retry').hidden, false);
  let prevented = false;
  page.lifecycle.beforeunload({ preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  page.fail = false;
  page.element('sc-save-retry').listeners.click();
  await new Promise(setImmediate);
  assert.equal(page.posts.at(-1).config.reserve_soc_pct, 23);
  assert.equal(page.element('sc-save-retry').hidden, true);
  prevented = false;
  page.lifecycle.beforeunload({ preventDefault() { prevented = true; } });
  assert.equal(prevented, false);
});

test('late configuration loads cannot overwrite newer edits in either Controller', async () => {
  for (const [kind, loader, id, value] of [
    ['sc', 'loadSmartCharge', 'sc-reserve-soc', 19],
    ['solar', 'loadSolarCharge', 'solar-car-load-w', 1500],
  ]) {
    const page = formHarness();
    const fetching = page.fetch;
    let release;
    page.fetch = (url, options = {}) => url.includes('/config?') && !options.method
      ? new Promise(resolve => { release = () => fetching(url).then(resolve); }) : fetching(url, options);
    const loading = page.context[loader]();
    await page.edit(kind, id, value);
    release();
    await loading;
    assert.equal(page.element(id).value, String(value));
    assert.equal(page.element(`${kind}-save-status`).textContent, 'Saved automatically.');
  }
});

test('a new edit is still sent after the previous in-flight save fails', async () => {
  let reject;
  const calls = [];
  const page = harness((device, config) => {
    calls.push(config.mode);
    return calls.length === 1 ? new Promise((resolve, fail) => { reject = fail; }) : Promise.resolve({});
  });
  const first = page.controller.update('A', { mode: 'active' }, { immediate: true });
  page.controller.update('A', { mode: 'off' }, { immediate: true });
  reject(new Error('HTTP 503'));
  await first;
  assert.deepEqual(calls, ['active', 'off']);
  assert.equal(page.controller.pending(), false);
});

test('a successful old save cannot acknowledge an invalid newer edit', async () => {
  let release;
  const page = harness(() => new Promise(resolve => { release = resolve; }));
  const saving = page.controller.update('A', { reserve_soc_pct: 10 }, { immediate: true });
  page.controller.invalidate('A', 'Invalid reserve — not saved');
  release({});
  await saving;
  assert.equal(page.saved.length, 0);
  assert.match(page.notices.at(-1)[1], /not saved/);
  assert.equal(page.controller.pending(), true);
});

test('malformed or normalized save responses cannot claim the draft was saved', async () => {
  for (const config of [null, [], { reserve_soc_pct: 10 }]) {
    const page = formHarness();
    page.fetch = async () => ({ ok: true, json: async () => ({ config }) });
    await page.edit('sc', 'sc-reserve-soc', 24);
    assert.match(page.element('sc-save-status').textContent, /Could not confirm/);
    assert.equal(page.element('sc-save-retry').hidden, false);
    assert.equal(page.element('sc-reserve-soc').value, '24');
  }
});

test('failed or malformed initial loads never make either Controller writable', async () => {
  for (const [kind, loader, id, value] of [
    ['sc', 'loadSmartCharge', 'sc-reserve-soc', 19],
    ['solar', 'loadSolarCharge', 'solar-car-load-w', 1500],
  ]) {
    for (const ok of [false, true]) {
      const page = formHarness();
      page.element(`${kind}-form`).dataset.loadedDeviceSn = '';
      page.fetch = async () => ({ ok, status: 503, json: async () => ({}) });
      await page.context[loader]();
      assert.equal(page.element(`${kind}-form`).dataset.loadedDeviceSn, '');
      assert.match(page.element(`${kind}-save-status`).textContent, /could not be loaded/);
      await page.edit(kind, id, value);
      assert.match(page.element(`${kind}-save-status`).textContent, /load/);
    }
  }
});

test('valid initial loads hydrate both Controllers without triggering a write', async () => {
  const page = formHarness();
  page.element('sc-form').dataset.loadedDeviceSn = '';
  page.element('solar-form').dataset.loadedDeviceSn = '';
  await page.context.loadSmartCharge();
  await page.context.loadSolarCharge();
  assert.equal(page.posts.length, 0);
  assert.equal(page.element('sc-form').dataset.loadedDeviceSn, 'A');
  assert.equal(page.element('solar-form').dataset.loadedDeviceSn, 'A');
  assert.equal(page.element('sc-mode').value, 'off');
  assert.equal(page.element('solar-mode').value, 'off');
  assert.match(page.element('sc-save-status').textContent, /save automatically/);
  assert.match(page.element('solar-save-status').textContent, /save automatically/);
});

test('reopening Controllers preserves failed drafts rather than hydrating old server values', async () => {
  const page = formHarness();
  page.fail = true;
  await page.edit('sc', 'sc-reserve-soc', 24);
  await page.edit('solar', 'solar-car-load-w', 1500);
  await page.context.loadSmartCharge();
  await page.context.loadSolarCharge();
  assert.equal(page.element('sc-reserve-soc').value, '24');
  assert.equal(page.element('solar-car-load-w').value, '1500');
  for (const kind of ['sc', 'solar']) {
    assert.match(page.element(`${kind}-save-status`).textContent, /Could not confirm/);
    assert.equal(page.element(`${kind}-save-retry`).hidden, false);
  }
});

test('a queued save uses its captured serial and cannot update another devices feedback', async () => {
  const page = formHarness();
  await page.edit('sc', 'sc-reserve-soc', 24, 'input');
  page.deviceSn = 'NEW';
  page.element('sc-form').dataset.loadedDeviceSn = 'NEW';
  page.element('sc-save-status').textContent = 'New device';
  await page.flush();
  assert.match(page.posts[0].url, /device_sn=A$/);
  assert.equal(page.element('sc-save-status').textContent, 'New device');
  assert.equal(page.invalidations, 0);
});

test('Evaluate and Backtest wait for confirmed settings instead of running an older configuration', async () => {
  const page = formHarness();
  await page.edit('sc', 'sc-reserve-soc', 24, 'input');
  assert.equal(page.element('sc-evaluate').disabled, true);
  assert.equal(page.element('sc-backtest').disabled, true);
  await page.flush();
  assert.equal(page.element('sc-evaluate').disabled, false);
  assert.equal(page.element('sc-backtest').disabled, false);
  page.fail = true;
  await page.edit('solar', 'solar-car-load-w', 1500);
  assert.equal(page.element('solar-evaluate').disabled, true);
  page.fail = false;
  page.element('solar-save-retry').listeners.click();
  await new Promise(setImmediate);
  assert.equal(page.element('solar-evaluate').disabled, false);
});
