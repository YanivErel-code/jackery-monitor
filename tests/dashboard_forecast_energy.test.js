'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
process.env.TZ = 'America/Los_Angeles';

class ForecastDate extends Date {
  constructor(...args) { super(...(args.length ? args : ['2026-11-06T12:45:00-08:00'])); }
}

function page() {
  const elements = new Map();
  const context = vm.createContext({ Date: ForecastDate, $: id => {
    if (!elements.has(id)) elements.set(id, { innerHTML: '', textContent: '', hidden: true });
    return elements.get(id);
  } });
  for (const name of ['forecastIntervalHours', 'renderForecastDayStrip', 'renderTodayEnergyBudget']) {
    const start = source.indexOf(`function ${name}(`);
    const end = source.indexOf('\n}', start) + 2;
    assert.ok(start >= 0 && end > start);
    vm.runInContext(source.slice(start, end), context);
  }
  return { elements, context };
}

const ts = value => new Date(value).getTime() / 1000;
function sample() {
  return { ready: true, starting_soc_pct: 40, capacity_wh: 30240, charge_efficiency: 0.85,
    today_actual_solar_wh: 5000, forecast: [
      { ts: ts('2026-11-06T13:00:00-08:00'), duration_h: 0.25, solar_w: 4000, load_w: 1000, predicted_soc: 42.1081 },
      { ts: ts('2026-11-06T14:00:00-08:00'), duration_h: 1, solar_w: 2000, load_w: 1000, predicted_soc: 44.9190 },
      { ts: ts('2026-11-06T15:00:00-08:00'), duration_h: 1, solar_w: 0, load_w: 1000, predicted_soc: 41.6121 },
    ] };
}

test('today adds measured solar to remaining interval energy without double-counting elapsed time', () => {
  const app = page();
  app.context.renderForecastDayStrip(sample());
  const html = app.elements.get('forecast-day-strip').innerHTML;
  assert.match(html, /8\.0 kWh/); // 5 measured + 4kW * 0.25h + 2kW * 1h
  assert.doesNotMatch(html, /11\.0 kWh/);
});

test('energy budget uses durations for solar, load, stored energy and SOC contributions', () => {
  const app = page();
  app.context.renderTodayEnergyBudget(sample());
  assert.equal(app.elements.get('tb-solar-kwh').textContent, '3.0');
  assert.equal(app.elements.get('tb-load-kwh').textContent, '1.3');
  assert.equal(app.elements.get('tb-net-kwh').textContent, '1.5');
  assert.match(app.elements.get('tb-solar-sub').textContent, /1\.3 h remaining/);
  assert.equal(app.elements.get('tb-load-sub').textContent, 'avg 1000 W');
  const html = app.elements.get('today-budget-tbody').innerHTML;
  assert.match(html, /\+2\.11/); // 637.5Wh / 30240Wh, not a whole-hour +8.43pp.
  assert.doesNotMatch(html, /\+8\.43/);
});

test('midnight interval belongs to the day it closes and carries the correct midnight SOC', () => {
  const app = page();
  const data = { ready: true, starting_soc_pct: 40, forecast: [
    { ts: ts('2026-11-07T00:00:00-08:00'), duration_h: 0.25, solar_w: 0, predicted_soc: 30 },
    { ts: ts('2026-11-07T01:00:00-08:00'), duration_h: 1, solar_w: 0, predicted_soc: 25 },
  ] };
  app.context.renderForecastDayStrip(data);
  const html = app.elements.get('forecast-day-strip').innerHTML;
  const first = html.slice(0, html.indexOf('<div class="forecast-day-tile', 1));
  assert.match(first, /Today/);
  assert.match(first, /30<small>%/);
  assert.doesNotMatch(first, /25<small>%/);
});

test('today measured solar is not added to a future-only forecast tile', () => {
  const app = page();
  app.context.renderForecastDayStrip({ ready: true, today_actual_solar_wh: 5000, forecast: [
    { ts: ts('2026-11-07T10:00:00-08:00'), duration_h: 1, solar_w: 1000, predicted_soc: 50 },
  ] });
  const html = app.elements.get('forecast-day-strip').innerHTML;
  assert.match(html, /Tomorrow/);
  assert.match(html, /1\.0 kWh/);
  assert.doesNotMatch(html, /6\.0 kWh/);
});

test('legacy full-hour responses remain supported; invalid or zero durations invent no energy', () => {
  const app = page();
  assert.equal(app.context.forecastIntervalHours({}), 1);
  assert.equal(app.context.forecastIntervalHours({ duration_h: 0 }), 0);
  assert.equal(app.context.forecastIntervalHours({ duration_h: -1 }), 0);
  assert.equal(app.context.forecastIntervalHours({ duration_h: NaN }), 0);
  assert.equal(app.context.forecastIntervalHours({ duration_h: Infinity }), 0);
  assert.equal(app.context.forecastIntervalHours({ duration_h: 2 }), 1);
});
