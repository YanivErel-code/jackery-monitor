'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const web = path.join(__dirname, '../web');
const html = fs.readFileSync(path.join(web, 'index.html'), 'utf8');

async function bootHtml(source, { failHelper = false } = {}) {
  const elements = new Map();
  const queued = [];
  const intervals = [];
  const errors = [];
  const raf = [];
  let clock = 0;
  const makeElement = (tag = 'div', attributes = {}) => {
    const classes = new Set((attributes.class || '').split(/\s+/));
    const element = {
      tagName: tag.toUpperCase(), id: attributes.id || '', value: attributes.value || '',
      textContent: '—', innerHTML: '', hidden: Object.hasOwn(attributes, 'hidden'),
      style: {}, dataset: {}, children: [], options: [], checked: false, disabled: false,
      classList: { contains: name => classes.has(name), add: (...names) => names.forEach(name => classes.add(name)),
        remove: (...names) => names.forEach(name => classes.delete(name)),
        toggle: (name, force) => { if (force ?? !classes.has(name)) classes.add(name); else classes.delete(name); },
      },
      addEventListener() {}, removeEventListener() {}, appendChild() {}, append() {},
      replaceChildren() {}, remove() {}, focus() {}, querySelectorAll: () => [], querySelector: () => null,
      closest: () => null,
      getAttribute: name => attributes[name] ?? null, setAttribute: (name, value) => { attributes[name] = value; },
      removeAttribute: name => { delete attributes[name]; }, toggleAttribute() {},
      getBoundingClientRect: () => ({ width: 800, height: 200, top: 0, left: 0 }),
      getContext: () => new Proxy({}, { get: (_, key) => key === 'createLinearGradient'
        ? () => ({ addColorStop() {} }) : () => {} }),
    };
    for (const [name, value] of Object.entries(attributes)) {
      if (name.startsWith('data-')) element.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = value;
    }
    return element;
  };
  for (const tag of source.matchAll(/<([a-zA-Z][\w:-]*)\b([^>]*)>/g)) {
    const attributes = {};
    for (const attribute of tag[2].matchAll(/([\w-]+)(?:="([^"]*)")?/g)) attributes[attribute[1]] = attribute[2] ?? '';
    if (attributes.id) elements.set(attributes.id, makeElement(tag[1], attributes));
  }
  const document = {
    getElementById: id => elements.get(id) || null, querySelectorAll: () => [], querySelector: () => null,
    addEventListener() {}, removeEventListener() {}, body: makeElement('body'),
    documentElement: makeElement('html'), visibilityState: 'visible',
    createElement: tag => makeElement(tag), createElementNS: (_, tag) => makeElement(tag),
  };
  const storage = { getItem: () => null, setItem() {}, removeItem() {} };
  const fetches = [];
  let sockets = 0, socket;
  const context = vm.createContext({
    document, navigator: {}, localStorage: storage, sessionStorage: storage,
    location: { protocol: 'http:', host: 'example.test', hash: '', pathname: '/', search: '' },
    history: { pushState() {}, replaceState() {} }, URLSearchParams, Intl, Date,
    console: { warn() {}, debug() {}, error: error => errors.push(String(error)), log() {} },
    performance: { now: () => clock }, devicePixelRatio: 1,
    queueMicrotask: callback => queued.push(callback),
    setTimeout: callback => { queued.push(callback); return queued.length; }, clearTimeout() {},
    setInterval: (callback, delay) => { intervals.push({ callback, delay }); return intervals.length; }, clearInterval() {},
    requestAnimationFrame: callback => { raf.push(callback); return raf.length; }, cancelAnimationFrame() {},
    addEventListener() {}, matchMedia: () => ({ matches: false, addEventListener() {} }),
    Sortable: { create() {} },
    WebSocket: class { constructor() { sockets++; socket = this; } close() {} },
    fetch: async url => {
      fetches.push(url);
      const body = url === '/api/auth/status' ? { has_credentials: true, api_family: 'portable' }
        : url === '/api/location' ? { latitude: 1, longitude: 1 }
          : url.startsWith('/api/forecast') ? { configured: false } : {};
      return { ok: true, json: async () => body };
    },
  });
  context.window = context;
  for (const script of source.matchAll(/<script\b[^>]*src="([^"]+)"[^>]*><\/script>/g)) {
    const filename = path.basename(script[1]);
    // Third-party dragging is unrelated to startup; its browser API is stubbed.
    if (filename === 'sortable.min.js' || (failHelper && filename === 'dashboard_requests.js')) continue;
    vm.runInContext(fs.readFileSync(path.join(web, filename), 'utf8'), context, { filename });
  }
  while (queued.length) queued.shift()();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(sockets, 1, 'full page opened its monitoring connection');
  assert.ok(intervals.some(interval => interval.delay === 2000), 'recovery polling installed');
  socket.onmessage({ data: JSON.stringify({ type: 'snapshot', data: {
    connection_status: 'connected', device: { name: 'QA', device_sn: 'QA', model_code: 13 },
    cloud: { api_family: 'portable', selected_device_id: 'QA', devices: [{ device_id: 'QA', device_sn: 'QA' }] },
    telemetry: { battery_percent: 53, input_power_w: 0, output_power_w: 100 },
    battery_packs: [], history: [], energy: null,
  } }) });
  while (raf.length) { clock += 500; raf.shift()(clock); }
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements.get('battery-pct').textContent, '53');
  assert.equal(elements.get('conn-text').textContent, 'connected');
  assert.equal(elements.get('dev-sn').textContent, 'QA');
  assert.equal(elements.get('today-out-kwh').textContent, '—');
  assert.deepEqual(errors, []);
  return { fetches, sockets };
}

test('complete cached legacy HTML boots the current app without a helper script', async () => {
  const legacy = html.replace(/\s*<script src="\/static\/dashboard_requests\.js"><\/script>/, '');
  assert.ok(!legacy.includes('<script src="/static/dashboard_requests.js">'));
  await bootHtml(legacy);
});

test('complete current HTML boots with its compatibility helper', async () => {
  await bootHtml(html);
});

test('complete current HTML keeps monitoring when the helper fails to load', async () => {
  await bootHtml(html, { failHelper: true });
});
