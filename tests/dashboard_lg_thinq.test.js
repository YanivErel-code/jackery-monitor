'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../web/lg_thinq.js'), 'utf8');

function harness() {
  function makeElement() {
    return { children: [], listeners: {}, checked: false, value: '', hidden: true,
      textContent: '', disabled: false,
      addEventListener(name, handler) { this.listeners[name] = handler; },
      replaceChildren() { this.children = []; },
      append(...children) { this.children.push(...children); },
      appendChild(child) { this.children.push(child); },
      get childElementCount() { return this.children.length; },
    };
  }
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, makeElement());
    return elements.get(id);
  };
  const requests = [];
  const network = { unavailable: false };
  const lifecycle = { startup: null, intervals: [] };
  const snapshot = { configured: true, country_code: 'US', devices: [
    { device_id: 'oven', alias: 'Kitchen oven', device_type: 'DEVICE_OVEN', activity: 'off', fresh: true },
    { device_id: 'ac', alias: 'Upstairs AC', device_type: 'DEVICE_AIR_CONDITIONER', activity: 'off', fresh: true },
    { device_id: 'dryer', alias: 'Laundry dryer', device_type: 'DEVICE_DRYER', activity: 'active', fresh: true },
  ] };
  const context = vm.createContext({
    window: { addEventListener(name, handler) { if (name === 'load') lifecycle.startup = handler; } },
    document: { hidden: false, getElementById: element, createElement: makeElement },
    setInterval(handler) { lifecycle.intervals.push(handler); }, confirm: () => true,
    fetch: async (url, options) => {
      requests.push({ url, options });
      if (network.unavailable) throw new Error('synthetic-private-response');
      return { ok: true, json: async () => snapshot };
    },
  });
  vm.runInContext(source, context);
  const config = () => JSON.parse(JSON.stringify(context.window.LGThinQ.config()));
  return { api: context.window.LGThinQ, element, requests, config, snapshot, network, lifecycle };
}

test('LG mapping is explicit, generic and separate for each Jackery', async () => {
  const page = harness();
  page.api.hydrate({});
  await new Promise(setImmediate);
  assert.equal(page.element('sc-lg-devices').children.length, 3);
  assert.deepEqual(page.config().lg_device_ids, []);
  const choices = page.element('sc-lg-devices').children;
  for (const index of [0, 1]) {
    const checkbox = choices[index].children[0];
    checkbox.checked = true;
    checkbox.listeners.change();
  }
  assert.deepEqual(page.config().lg_device_ids, ['oven', 'ac']);
  await page.api.load();
  assert.deepEqual(page.config().lg_device_ids, ['oven', 'ac']);
  page.api.hydrate({ lg_device_ids: ['dryer'], lg_interlock_enabled: true });
  assert.deepEqual(page.config().lg_device_ids, ['dryer']);
  assert.equal(page.config().lg_interlock_enabled, true);
  page.api.hydrate({ lg_device_ids: ['oven', 'ac'] });
  assert.deepEqual(page.config().lg_device_ids, ['oven', 'ac']);
});

test('missing selected appliances stay mapped instead of silently releasing protection', async () => {
  const page = harness();
  page.api.hydrate({ lg_device_ids: ['missing-oven'], lg_interlock_enabled: true });
  await new Promise(setImmediate);
  assert.deepEqual(page.config().lg_device_ids, ['missing-oven']);
  const missing = page.element('sc-lg-devices').children.at(-1);
  assert.equal(missing.children[0].checked, true);
  assert.match(missing.children[1].textContent, /unavailable/);
});

test('explicit offline assumptions are labelled without calling expired readings OFF', async () => {
  const page = harness();
  const dryer = page.snapshot.devices[2];
  Object.assign(dryer, { activity: 'off', assumed_off: true, disconnected: true, fresh: true });
  await page.api.load();
  assert.match(page.element('sc-lg-devices').children[2].children[1].textContent, /offline.*treated as OFF/);
  dryer.fresh = false;
  await page.api.load();
  assert.doesNotMatch(page.element('sc-lg-devices').children[2].children[1].textContent, /treated as OFF/);
});

test('LG token errors show a persistent Automation warning and clear on recovery', async () => {
  const page = harness();
  page.snapshot.error = 'LG token is invalid or expired';
  page.snapshot.warnings = [page.snapshot.error];
  await page.api.load();
  assert.equal(page.element('automation-lg-warning').hidden, false);
  assert.equal(page.element('tab-automation-lg-dot').hidden, false);
  assert.match(page.element('automation-lg-warning-detail').textContent, /invalid or expired/);
  await page.api.load();
  assert.equal(page.element('automation-lg-warning').hidden, false);
  page.snapshot.error = null;
  page.snapshot.warnings = [];
  await page.api.load();
  assert.equal(page.element('automation-lg-warning').hidden, true);
  assert.equal(page.element('tab-automation-lg-dot').hidden, true);
});

test('the Automation red dot updates from other tabs without opening LG configuration', async () => {
  const page = harness();
  page.snapshot.error = 'LG token is invalid or expired';
  await page.lifecycle.startup();
  assert.equal(page.element('tab-settings').hidden, true);
  assert.equal(page.element('tab-automation').hidden, true);
  assert.equal(page.element('tab-automation-lg-dot').hidden, false);
  page.snapshot.error = null;
  await page.lifecycle.intervals[0]();
  assert.equal(page.element('tab-automation-lg-dot').hidden, true);
});

test('a missing token triggers the red dot on startup when any Jackery has LG protection', async () => {
  const page = harness();
  Object.assign(page.snapshot, { configured: false, protection_enabled: true, devices: [],
    warnings: ['LG token is missing or unreadable'] });
  await page.lifecycle.startup();
  assert.equal(page.element('tab-automation-lg-dot').hidden, false);
});

test('LG transport errors warn without leaking the response and recover on the next successful poll', async () => {
  const page = harness();
  await page.api.load();
  page.network.unavailable = true;
  await page.api.load();
  assert.equal(page.element('automation-lg-warning').hidden, false);
  assert.match(page.element('automation-lg-warning-detail').textContent, /API or network is unavailable/);
  assert.doesNotMatch(page.element('automation-lg-warning-detail').textContent, /synthetic-private-response/);
  page.network.unavailable = false;
  await page.api.load();
  assert.equal(page.element('automation-lg-warning').hidden, true);
});

test('a failed initial monitoring request still shows the red dot before configuration can load', async () => {
  const page = harness();
  Object.assign(page.snapshot, { configured: false, devices: [] });
  page.network.unavailable = true;
  await page.lifecycle.startup();
  assert.equal(page.element('tab-automation-lg-dot').hidden, false);
  assert.equal(page.element('automation-lg-warning').hidden, false);
});

test('stale or unknown appliance states warn without being relabelled OFF', async () => {
  const page = harness();
  Object.assign(page.snapshot.devices[2], { fresh: false, activity: 'unknown', assumed_off: false });
  await page.api.load();
  assert.equal(page.element('automation-lg-warning').hidden, false);
  assert.match(page.element('automation-lg-warning-detail').textContent, /Laundry dryer: status is stale/);
  assert.match(page.element('sc-lg-devices').children[2].children[1].textContent, /unknown/);
  assert.doesNotMatch(page.element('sc-lg-devices').children[2].children[1].textContent, /treated as OFF/);
});

test('missing LG credentials warn for configured protection but stay quiet when LG is unused', async () => {
  const page = harness();
  Object.assign(page.snapshot, { configured: false, devices: [], warnings: ['LG token is missing or unreadable'] });
  await page.api.load();
  assert.equal(page.element('automation-lg-warning').hidden, true);
  page.api.hydrate({ lg_interlock_enabled: true, lg_device_ids: ['dryer'] });
  await new Promise(setImmediate);
  assert.equal(page.element('automation-lg-warning').hidden, false);
  assert.match(page.element('automation-lg-warning-detail').textContent, /LG token is missing/);
});

test('LG warnings render cloud-provided labels as plain text', async () => {
  const page = harness();
  page.snapshot.devices[2].alias = '<img src=x onerror=alert(1)>';
  page.snapshot.devices[2].fresh = false;
  await page.api.load();
  const detail = page.element('automation-lg-warning-detail');
  assert.match(detail.textContent, /<img/);
  assert.equal(detail.children.length, 0);
});

test('LG finish estimate is included in the smart-charge decision display', () => {
  const appSource = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
  const start = appSource.indexOf('function renderSmartChargePlan(');
  const end = appSource.indexOf('\n}', start) + 2;
  const element = { innerHTML: '', hidden: true };
  const context = vm.createContext({ $: () => element });
  const escapeStart = appSource.indexOf('function escapeHtml(');
  vm.runInContext(appSource.slice(escapeStart, appSource.indexOf('\n}', escapeStart) + 2), context);
  vm.runInContext(appSource.slice(start, end), context);
  context.renderSmartChargePlan({ action: 'off', mode: 'active', target_sunrise_soc_pct: 32,
    reason: 'LG appliance <img src=x onerror=alert(1)> is active',
    lg_protection: { expected_finish_ts: 1790001800, charge_not_before_ts: 1790002100,
      soc_at_finish_pct: 18, blocked_now: true } });
  assert.match(element.innerHTML, /LG estimated finish/);
  assert.match(element.innerHTML, /18%/);
  assert.match(element.innerHTML, /active-state and manual holds are rechecked/);
  assert.doesNotMatch(element.innerHTML, /<img/);
  context.renderSmartChargePlan({ action: 'on', mode: 'active', target_sunrise_soc_pct: 30,
    reason: 'charging now', lg_protection: { blocked_now: false, monitoring_degraded: true,
      warnings: ['LG token expired <img src=x>'] } });
  assert.match(element.innerHTML, /LG monitoring warning/);
  assert.match(element.innerHTML, /errors do not block charging/);
  assert.doesNotMatch(element.innerHTML, /All selected appliances OFF/);
  assert.doesNotMatch(element.innerHTML, /<img/);
});

test('LG token entry is cleared immediately and never rendered back into the form', async () => {
  const page = harness();
  const token = 'thinqpat_' + 'synthetic-ui-test'.repeat(3);
  page.element('lg-token').value = token;
  page.element('lg-country').value = 'us';
  const saving = page.element('lg-credentials-form').listeners.submit({ preventDefault() {} });
  assert.equal(page.element('lg-token').value, '');
  await saving;
  const request = page.requests.find(item => item.url.endsWith('/credentials'));
  assert.deepEqual(JSON.parse(request.options.body), { token, country_code: 'US' });
  assert.equal(page.element('lg-token').value, '');
  assert.ok(!page.element('lg-message').textContent.includes(token));
});

test('switching Jackery cannot save the previous devices appliance settings while loading', async () => {
  const appSource = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, { value: '', checked: false, dataset: {}, listeners: {},
      addEventListener(name, listener) { this.listeners[name] = listener; } });
    return elements.get(id);
  };
  element('sc-form').dataset.loadedDeviceSn = 'FIRST';
  const calls = [];
  const context = vm.createContext({ document: { getElementById: element }, $: element,
    activeJackeryDevice: () => ({ device_sn: 'SECOND' }), window: {},
    fetch: async (...args) => { calls.push(args); throw new Error('must not send'); },
  });
  const start = appSource.indexOf("document.getElementById('sc-form')?.addEventListener('submit'");
  const end = appSource.indexOf('\n});', start) + 4;
  vm.runInContext(appSource.slice(start, end), context);
  await element('sc-form').listeners.submit({ preventDefault() {} });
  assert.equal(calls.length, 0);
  assert.match(element('sc-status').textContent, /load/i);
});
