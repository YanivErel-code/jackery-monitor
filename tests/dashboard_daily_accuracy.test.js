'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const appSource = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');

function accuracyHarness(rows, cutoff = 0) {
  const elements = new Map();
  for (const id of ['daily-accuracy-card', 'daily-accuracy-days', 'daily-accuracy-tbody',
    'daily-accuracy-table', 'daily-accuracy-empty', 'daily-acc-sunset-mae',
    'daily-acc-sunset-n', 'daily-acc-sunrise-mae', 'daily-acc-sunrise-n',
    'daily-acc-hitrate', 'daily-acc-hitrate-sub']) {
    elements.set(id, { hidden: true, innerHTML: '', textContent: '', value: '14' });
  }
  const context = vm.createContext({
    $: id => elements.get(id),
    _forecastGeneration: 0,
    activeJackeryDevice: () => ({ device_sn: 'TEST-A' }),
    fetch: async () => ({ ok: true, json: async () => ({ rows, cutoff_ts: cutoff }) }),
  });
  for (const name of ['escapeHtml', 'renderDailyAccuracy', '_fmtPct', '_fmtErrCell',
    '_renderAccuracySummary', '_fmtForecastTime', '_fmtSunsetForecast']) {
    const start = appSource.search(new RegExp(`^(?:async )?function ${name}\\(`, 'm'));
    if (start < 0) continue;
    const end = appSource.indexOf('\n}', start) + 2;
    vm.runInContext(appSource.slice(start, end), context);
  }
  return { elements, context };
}

const snapshot = {
  date: '2026-10-14', predicted_sunrise_soc_pct: 57, actual_sunrise_soc_pct: 56,
  predictions_made_at: 2000, sunrise_ts: 6000,
  sunset_forecast_soc_pct: 20, sunset_forecast_made_at: 1000,
  sunset_forecast_sunrise_ts: 5000, sunset_forecast_actual_soc_pct: 56,
  sunset_forecast_error_pp: 36,
};

test('sunrise error and MAE use the sunset snapshot, not the latest estimate', async () => {
  const page = accuracyHarness([snapshot]);
  await page.context.renderDailyAccuracy();
  const html = page.elements.get('daily-accuracy-tbody').innerHTML;
  assert.match(html, /20%/);
  assert.match(html, /57%/);
  assert.match(html, /Saved/);
  assert.match(html, /Target/);
  assert.match(html, /\+36\.0/);
  assert.equal(page.elements.get('daily-acc-sunrise-mae').textContent, '36.0');
});

test('legacy rolling predictions remain visible but never count as sunset snapshots', async () => {
  const page = accuracyHarness([{
    date: '2026-10-13', predicted_sunrise_soc_pct: 50,
    actual_sunrise_soc_pct: 51, predictions_made_at: 2000,
    predicted_sunset_soc_pct: 60, actual_sunset_soc_pct: 60,
  }]);
  await page.context.renderDailyAccuracy();
  const html = page.elements.get('daily-accuracy-tbody').innerHTML;
  assert.match(html, /Not captured/);
  assert.match(html, /50%/);
  assert.doesNotMatch(html, /51%/);
  assert.doesNotMatch(html, /\+1\.0/);
  assert.equal(page.elements.get('daily-acc-sunrise-mae').textContent, '—');
  assert.equal(page.elements.get('daily-acc-hitrate').textContent, '—');
});

test('sunrise hit rate is not inflated by rolling sunset hits', async () => {
  const page = accuracyHarness([{
    ...snapshot, predicted_sunset_soc_pct: 60, actual_sunset_soc_pct: 60,
  }]);
  await page.context.renderDailyAccuracy();
  assert.equal(page.elements.get('daily-acc-hitrate').textContent, 0);
});

test('the debug view distinguishes the original from the rolling estimate', async () => {
  const page = accuracyHarness([snapshot]);
  const button = { addEventListener(event, handler) { this.handler = handler; } };
  page.elements.set('dbg-daily-summary', button);
  page.context._dbgFetch = async () => ({ rows: [snapshot] });
  let output;
  page.context._dbgRender = (title, rows) => { output = rows; };
  const start = appSource.indexOf("$('dbg-daily-summary')?.addEventListener");
  const end = appSource.indexOf("$('dbg-battery-packs')", start);
  vm.runInContext(appSource.slice(start, end), page.context);
  await button.handler();
  assert.equal(output[0].sunrise_original, 20);
  assert.equal(output[0].sunrise_rolling, 57);
  assert.equal(output[0].original_error_pp, '36.0');
});

test('zero percent snapshot is measured, not treated as missing', async () => {
  const page = accuracyHarness([{ ...snapshot, sunset_forecast_soc_pct: 0 }]);
  await page.context.renderDailyAccuracy();
  assert.match(page.elements.get('daily-accuracy-tbody').innerHTML, /0%/);
  assert.equal(page.elements.get('daily-acc-sunrise-mae').textContent, '56.0');
});

test('sunrise post-fix filtering uses capture time, not the rolling update time', async () => {
  const page = accuracyHarness([
    snapshot,
    { ...snapshot, date: '2026-10-15', sunset_forecast_made_at: 3000,
      sunset_forecast_soc_pct: 50 },
  ], 1500);
  await page.context.renderDailyAccuracy();
  assert.equal(page.elements.get('daily-acc-sunrise-mae').textContent, '6.0');
  assert.match(page.elements.get('daily-acc-sunrise-n').textContent, /1 day measured/);
});

test('fixed actual is not substituted with a rolling actual at a different target', async () => {
  const page = accuracyHarness([{ ...snapshot, sunset_forecast_actual_soc_pct: null }]);
  await page.context.renderDailyAccuracy();
  assert.equal(page.elements.get('daily-acc-sunrise-mae').textContent, '—');
});

test('an account switch discards delayed accuracy data', async () => {
  const page = accuracyHarness([snapshot]);
  page.context.fetch = async () => {
    page.context._forecastGeneration++;
    return { ok: true, json: async () => ({ rows: [snapshot] }) };
  };
  await page.context.renderDailyAccuracy();
  assert.equal(page.elements.get('daily-accuracy-tbody').innerHTML, '');
});
