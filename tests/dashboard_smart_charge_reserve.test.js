'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');

function render(plan) {
  const element = { innerHTML: '', hidden: true };
  const context = vm.createContext({ $: () => element });
  for (const name of ['escapeHtml', 'renderSmartChargePlan']) {
    const start = source.search(new RegExp(`^function ${name}\\(`, 'm'));
    const end = source.indexOf('\n}', start) + 2;
    assert.ok(start >= 0 && end > start);
    vm.runInContext(source.slice(start, end), context);
  }
  context.renderSmartChargePlan(plan);
  assert.equal(element.hidden, false);
  return element.innerHTML;
}

function plan() {
  return { mode: 'test', action: 'on', reason: 'charging', current_soc_pct: 16,
    predicted_sunrise_soc_pct: 0, target_sunrise_soc_pct: 30, deficit_kwh: 14,
    scheduled_sunrise_soc_pct: 31.2, scheduled_min_soc_pct: 10.3, reserve_soc_pct: 10,
    planned_intervals: [{ start: 1800000000, end: 1800000900 }], warnings: [] };
}

test('baseline and planned sunrise SOC are displayed separately', () => {
  const html = render(plan());
  assert.match(html, /Sunrise without grid/);
  assert.match(html, /With planned charging/);
  assert.match(html, /31\.2%/);
  assert.match(html, /Minimum planned SOC/);
  assert.match(html, /10\.3%/);
});

test('partial charge intervals do not display a fictional full hour', () => {
  const data = plan();
  const format = timestamp => new Date(timestamp * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const html = render(data);
  assert.ok(html.includes(`${format(data.planned_intervals[0].start)}–${format(data.planned_intervals[0].end)}`));
});

test('infeasible-plan warnings are visible and escaped', () => {
  const data = plan();
  data.warnings = ['Target cannot be reached <img src=x onerror=alert(1)>'];
  const html = render(data);
  assert.match(html, /role="alert"/);
  assert.match(html, /Target cannot be reached &lt;img/);
  assert.doesNotMatch(html, /<img/);
});

test('historical plans without projection metadata remain renderable', () => {
  const html = render({ mode: 'test', action: 'off', target_sunrise_soc_pct: 30, reason: 'old snapshot' });
  assert.match(html, /old snapshot/);
  assert.doesNotMatch(html, /With planned charging/);
});
