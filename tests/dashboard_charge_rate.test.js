'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');

function render(rate) {
  const output = { textContent: '', innerHTML: '' };
  const note = { textContent: '', innerHTML: '' };
  const context = vm.createContext({ $: id => id === 'sc-charge-rate' ? output : note });
  const start = source.indexOf('function renderSmartChargeRate(');
  const end = source.indexOf('\n}', start) + 2;
  vm.runInContext(source.slice(start, end), context);
  context.renderSmartChargeRate(rate);
  assert.equal(output.innerHTML, '');
  assert.equal(note.innerHTML, '');
  return `${output.textContent} ${note.textContent}`;
}

test('charge power has no editable field and is not submitted', () => {
  assert.doesNotMatch(html, /id="sc-max-charge-w"/);
  assert.doesNotMatch(source, /\$\('sc-max-charge-w'\)/);
  const start = source.indexOf("document.getElementById('sc-form')?.addEventListener('submit'");
  const end = source.indexOf("document.getElementById('sc-evaluate')", start);
  assert.doesNotMatch(source.slice(start, end), /max_charge_w:/);
  assert.match(html, /<output id="sc-charge-rate"/);
});

test('reserve remains an editable labelled field with range and save payload', () => {
  assert.match(html, /for="sc-reserve-soc"/);
  assert.match(html, /id="sc-reserve-soc" type="number" min="0" max="95"/);
  assert.match(html, /aria-describedby="sc-reserve-help"/);
  assert.match(source, /reserve_soc_pct: Number\(\$\('sc-reserve-soc'\)/);
});

test('read-only rate distinguishes learned values and the 1000 W default', () => {
  assert.match(render({ value: 1000, source: 'default' }), /1,000 W — default while learning/);
  assert.match(render({ value: 945, source: 'fit' }), /945 W — learned automatically/);
});

test('unavailable rates do not masquerade as a measured value', () => {
  assert.match(render(null), /unavailable/);
  assert.match(render({ value: NaN }), /unavailable/);
});

test('charge-rate notes are text, never executable HTML', () => {
  assert.match(render({ value: 1000, source: 'default', note: '<img src=x onerror=alert(1)>' }), /<img/);
});

test('learned charge rate cannot be overridden from Device parameters', () => {
  const list = { innerHTML: '', querySelectorAll: () => [] };
  const context = vm.createContext({ $: () => list });
  const start = source.indexOf('function renderDeviceParams(');
  const end = source.indexOf('\n}', start) + 2;
  vm.runInContext(source.slice(start, end), context);
  context.renderDeviceParams([{ key: 'max_charge_w', value: 950, source: 'fit', read_only: true }], 'QA');
  assert.doesNotMatch(list.innerHTML, /data-action="override"/);
  assert.match(list.innerHTML, /Automatic/);
  assert.match(list.innerHTML, /data-action="debug"/);
});

test('automatic refresh changes only the read-only rate, preserving unsaved reserve', async () => {
  const fields = { 'sc-charge-rate': {}, 'sc-charge-rate-note': {}, 'sc-reserve-soc': { value: '21' } };
  const context = vm.createContext({
    $: id => fields[id], isTabVisible: () => true, isHomeMonitoring: () => false,
    activeJackeryDevice: () => ({ device_sn: 'QA' }), isCurrentDeviceRequest: () => true,
    _chargeRateRefreshPending: false,
    fetch: async () => ({ ok: true, json: async () => ({ charge_rate: { value: 940, source: 'fit' } }) }),
  });
  for (const name of ['renderSmartChargeRate', 'refreshSmartChargeRate']) {
    const start = source.indexOf(`${name === 'refreshSmartChargeRate' ? 'async ' : ''}function ${name}(`);
    const end = source.indexOf('\n}', start) + 2;
    vm.runInContext(source.slice(start, end), context);
  }
  await context.refreshSmartChargeRate();
  assert.match(fields['sc-charge-rate'].textContent, /940 W/);
  assert.equal(fields['sc-reserve-soc'].value, '21');
});

test('late charge-rate responses cannot update a different selected device', async () => {
  const rendered = [];
  const context = vm.createContext({
    isTabVisible: () => true, isHomeMonitoring: () => false,
    activeJackeryDevice: () => ({ device_sn: 'OLD' }), isCurrentDeviceRequest: () => false,
    _chargeRateRefreshPending: false, renderSmartChargeRate: rate => rendered.push(rate),
    fetch: async () => ({ ok: true, json: async () => ({ charge_rate: { value: 940 } }) }),
  });
  const start = source.indexOf('async function refreshSmartChargeRate(');
  const end = source.indexOf('\n}', start) + 2;
  vm.runInContext(source.slice(start, end), context);
  await context.refreshSmartChargeRate();
  assert.deepEqual(rendered, []);
});
