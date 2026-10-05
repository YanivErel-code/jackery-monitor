'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const { createKeyedLoader, createStatusFallback } = require('../web/dashboard_requests.js');

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

test('same-device forecasts share a request and reuse a result for only 30 seconds', async () => {
  let now = 1000;
  const requests = [];
  const loader = createKeyedLoader({ now: () => now, ttlMs: 30_000,
    load: key => { const request = deferred(); requests.push({ key, ...request }); return request.promise; },
  });
  const hero = loader.get('A');
  const tab = loader.get('A');
  const otherDevice = loader.get('B');
  await Promise.resolve();
  assert.deepEqual(requests.map(r => r.key), ['A', 'B']);
  requests[0].resolve({ device_sn: 'A' });
  requests[1].resolve({ device_sn: 'B' });
  assert.equal(await hero, await tab);
  assert.equal((await otherDevice).device_sn, 'B');
  await loader.get('A');
  assert.equal(requests.length, 2);
  now += 30_000;
  const expired = loader.get('A');
  await Promise.resolve();
  assert.equal(requests.length, 3);
  requests[2].resolve({ device_sn: 'A' });
  await expired;
});

test('errors retry and invalidation cannot repopulate a cache with an old response', async () => {
  let calls = 0;
  const pending = deferred();
  const loader = createKeyedLoader({ ttlMs: 30_000,
    load: () => { calls++; if (calls === 1) throw new Error('offline'); return calls === 2 ? pending.promise : { fresh: true }; },
  });
  await assert.rejects(loader.get('A'), /offline/);
  const old = loader.get('A');
  await Promise.resolve();
  loader.clear();
  assert.deepEqual(await loader.get('A'), { fresh: true });
  pending.resolve({ fresh: false });
  await old;
  assert.deepEqual(await loader.get('A'), { fresh: true });
  assert.equal(calls, 3);
});

test('healthy WS emits zero two-second REST polls; stale fallback never overlaps', async () => {
  let now = 0;
  let calls = 0;
  const pending = deferred();
  const rendered = [];
  const fallback = createStatusFallback({ now: () => now, staleMs: 6000,
    load: () => { calls++; return pending.promise; }, onStatus: s => rendered.push(s),
  });
  fallback.markWsStatus();
  for (now = 2000; now <= 10_000; now += 2000) {
    fallback.markWsStatus();
    await fallback.tick();
  }
  assert.equal(calls, 0);
  now += 6000;
  const first = fallback.tick();
  const overlapping = fallback.tick();
  await Promise.resolve();
  assert.equal(calls, 1);
  pending.resolve({ device: 'A' });
  await Promise.all([first, overlapping]);
  assert.deepEqual(rendered, [{ device: 'A' }]);
});

test('new WS data supersedes an in-flight REST snapshot and REST errors can retry', async () => {
  let calls = 0;
  const pending = deferred();
  const rendered = [];
  const fallback = createStatusFallback({
    load: () => { calls++; if (calls === 1) return pending.promise; if (calls === 2) throw new Error('offline'); return { fresh: true }; },
    onStatus: s => rendered.push(s),
  });
  const old = fallback.tick();
  await Promise.resolve();
  fallback.markWsStatus();
  pending.resolve({ stale: true });
  await old;
  assert.equal(rendered.length, 0);
  fallback.markDisconnected();
  await fallback.tick();
  await fallback.tick();
  assert.deepEqual(rendered, [{ fresh: true }]);
});

test('repeated WS reconnect failures do not discard a valid slow REST fallback', async () => {
  const pending = deferred();
  const rendered = [];
  const fallback = createStatusFallback({ load: () => pending.promise, onStatus: value => rendered.push(value) });
  const request = fallback.tick();
  await Promise.resolve();
  fallback.markDisconnected();
  fallback.markDisconnected();
  pending.resolve({ fresh: true });
  await request;
  assert.deepEqual(rendered, [{ fresh: true }]);
});

// Exercise the real page loaders against delayed synthetic API responses.
// No browser or device/cloud connection is needed for these network guards.
const appSource = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
function pageHarness() {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, { textContent: 'old', hidden: false,
      querySelector: selector => element(id + selector),
      classList: { remove() {}, add() {}, toggle() {} },
    });
    return elements.get(id);
  }
  const requests = [];
  const context = vm.createContext({
    Date, console: { warn() {}, debug() {} }, document: { visibilityState: 'visible' },
    activeTab: 'live', currentSn: null, home: false, lastStatus: null,
    _pendingViewSwitch: null, _forecastGeneration: 0, energyRangeHours: 6,
    _eodLastFetchAt: 0, energyHistoryCache: null, elements,
    $: element, show: (el, on) => { if (el) el.hidden = !on; },
    drawEnergyChart: value => { context.chart = value; },
  });
  context.activeJackeryDevice = () => context.currentSn ? { device_sn: context.currentSn } : null;
  context.isHomeMonitoring = () => context.home;
  const load = key => { const pending = deferred(); requests.push({ key, ...pending }); return pending.promise; };
  context._dashboardReads = createKeyedLoader({ load });
  context._forecastReads = createKeyedLoader({ load, ttlMs: 30_000 });
  for (const name of ['isTabVisible', 'isCurrentDeviceRequest', 'fetchEnergyHistory',
    'fetchEnergyAllDevices', 'fetchForecast', 'fetchEodForecast', 'fetchBatteryPacks',
    'clearEnergyKpis', 'applyStatus', 'connectWs', 'invalidateForecastRequests']) {
    const start = appSource.search(new RegExp(`^(?:async )?function ${name}\\(`, 'm'));
    assert.notEqual(start, -1, `page function ${name} exists`);
    const end = appSource.indexOf('\n}', start) + 2;
    vm.runInContext(appSource.slice(start, end), context);
  }
  return { context, requests, element };
}

test('page loaders wait for discovery and hidden Energy generates no requests', async () => {
  const { context, requests } = pageHarness();
  await context.fetchEodForecast();
  await context.fetchBatteryPacks();
  context.activeTab = 'energy';
  await context.fetchEnergyHistory();
  await context.fetchEnergyAllDevices();
  assert.equal(requests.length, 0);
  context.currentSn = 'A';
  context.activeTab = 'live';
  await context.fetchEnergyHistory();
  await context.fetchEnergyAllDevices();
  context.activeTab = 'energy';
  context.document.visibilityState = 'hidden';
  await context.fetchEnergyHistory();
  assert.equal(requests.length, 0);
});

test('real hero and Forecast loaders coalesce; Home rejects old portable responses', async () => {
  const { context, requests, element } = pageHarness();
  context.currentSn = 'A';
  const hero = context.fetchEodForecast();
  context.activeTab = 'forecast';
  const tab = context.fetchForecast();
  await Promise.resolve();
  assert.equal(requests.length, 1);
  context.home = true;
  requests[0].resolve({ configured: false });
  await Promise.all([hero, tab]);
  assert.equal(element('forecast-needs-config').hidden, false);
  assert.equal(element('forecast-needs-configh2').textContent, 'old');
  await context.fetchForecast();
  await context.fetchEodForecast();
  assert.equal(requests.length, 1);
});

test('Energy responses cannot paint an old range or previous device', async () => {
  const { context, requests } = pageHarness();
  context.currentSn = 'A';
  context.activeTab = 'energy';
  const oldRange = context.fetchEnergyHistory();
  await Promise.resolve();
  context.energyRangeHours = 24;
  const newRange = context.fetchEnergyHistory();
  await Promise.resolve();
  requests[1].resolve({ series: ['new'] });
  await newRange;
  requests[0].resolve({ series: ['old'] });
  await oldRange;
  assert.deepEqual(context.chart, { series: ['new'] });
  const oldDevice = context.fetchEnergyHistory();
  await Promise.resolve();
  context.currentSn = 'B';
  requests[2].resolve({ series: ['A'] });
  await oldDevice;
  assert.deepEqual(context.chart, { series: ['new'] });
});

test('cold new-device aggregates clear visible totals and savings to unknown', () => {
  const { context, element } = pageHarness();
  context.clearEnergyKpis();
  for (const id of ['today-out-kwh', 'today-in-kwh', 'e-life-out', 'e-30d-in', 'today-net-savings']) {
    assert.equal(element(id).textContent, '—');
  }
  for (const id of ['today-savings-row', 'lifetime-savings-row', 'today-charged-split']) {
    assert.equal(element(id).hidden, true);
  }
});

test('current Forecast failures show an error while old-device failures leave the new view intact', async () => {
  const { context, requests, element } = pageHarness();
  context.currentSn = 'A';
  context.activeTab = 'forecast';
  element('forecast-needs-config').hidden = true;
  const failure = context.fetchForecast();
  await Promise.resolve();
  requests[0].reject(new Error('HTTP 503'));
  await failure;
  assert.equal(element('forecast-needs-config').hidden, false);
  assert.equal(element('forecast-needs-configh2').textContent, 'Forecast unavailable');
  const previous = context.fetchForecast();
  await Promise.resolve();
  context.currentSn = 'B';
  element('forecast-needs-configh2').textContent = 'B ready';
  requests[1].reject(new Error('HTTP 503'));
  await previous;
  assert.equal(element('forecast-needs-configh2').textContent, 'B ready');
});

test('pending device switches reject old WS freshness and old-view forecasts', async () => {
  const { context, requests } = pageHarness();
  context.currentSn = 'A';
  context.lastStatus = { cloud: { selected_device_id: 'A' } };
  context._pendingViewSwitch = { id: 'B', until: Date.now() + 8000 };
  await context.fetchEodForecast();
  assert.equal(requests.length, 0);
  let statusReads = 0;
  context._statusFallback = createStatusFallback({ load: () => { statusReads++; return {}; }, onStatus() {} });
  context.location = { protocol: 'http:', host: 'example.test' };
  context.WebSocket = class { constructor() { context.socket = this; } };
  context.connectWs();
  context.socket.onmessage({ data: JSON.stringify({ type: 'status', data: { cloud: { selected_device_id: 'A' } } }) });
  await context._statusFallback.tick();
  assert.equal(statusReads, 1, 'rejected WS frame must not mark connection fresh');
  context.applyStatus = () => true;
  context.socket.onmessage({ data: JSON.stringify({ type: 'status', data: {} }) });
  await context._statusFallback.tick();
  assert.equal(statusReads, 1, 'accepted WS frame suppresses redundant REST');
});

test('page forecast invalidation prevents old in-flight results from painting', async () => {
  const { context, requests, element } = pageHarness();
  context.currentSn = 'A';
  context.activeTab = 'forecast';
  const beforeSave = context.fetchForecast();
  await Promise.resolve();
  context.invalidateForecastRequests();
  const afterSave = context.fetchForecast();
  await Promise.resolve();
  assert.equal(requests.length, 2);
  requests[1].resolve({ configured: false });
  await afterSave;
  element('forecast-needs-configh2').textContent = 'new configuration';
  requests[0].resolve({ configured: false });
  await beforeSave;
  assert.equal(element('forecast-needs-configh2').textContent, 'new configuration');
});

test('page boot opens WS and installs recovery while bridge auth is still pending', async () => {
  const calls = [];
  const context = vm.createContext({
    applyHeroOrder() {}, isKeepAwakeOn: () => false, connectWs: () => calls.push('ws'),
    checkAuth: () => new Promise(() => {}), initHeroSortable() {},
    setInterval: () => calls.push('interval'), refreshAutomationDot() {},
    fetchEodForecast() {}, fetchBatteryPacks() {}, fetchEnergyHistory() {},
  });
  const boot = appSource.match(/\(async function boot\(\) \{[\s\S]*?\n\}\)\(\);/);
  assert.ok(boot);
  await vm.runInContext(boot[0], context);
  assert.equal(calls[0], 'ws');
  assert.ok(calls.includes('interval'));
});
