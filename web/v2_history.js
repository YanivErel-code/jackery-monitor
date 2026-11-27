/* Jackery Monitor — V2 History page: totals, energy by day, the 5-day
 * no-charge forecast and how the saved sunrise forecasts turned out — and the
 * V2 Forecast page (solar first), which shares the same data.
 * Read-only; everything comes from the same endpoints the classic Energy
 * and Forecast tabs use. */
(function () {
  'use strict';
  const { $, el, svgEl, num, pct, usd, watts, clock, tz, getJson, deviceSn, frame, axes, niceMax, xLabel, hover, nearest } = window.V2;

  let daily = [];
  let days = 30;
  let showAll = false;
  let forecast = null;
  let loadedFor = null;

  const kwh = (wh) => (num(wh) == null ? '—' : (wh / 1000).toFixed(wh >= 99950 ? 0 : 1) + ' kWh');
  const kwhDay = (v) => (num(v) == null ? '—' : v.toFixed(1));
  const dayDate = (iso) => { const [y, m, d] = iso.split('-').map(Number); return new Date(y, m - 1, d); };
  const shortDay = (date) => date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  const longDay = (date) => date.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });
  const localDay = (ts) => Math.floor((ts + tz()) / 86400);
  const dayStart = (dayIndex) => dayIndex * 86400 - tz();
  const dayClock = (ts) => tsDate(ts).toLocaleDateString('en-US', { weekday: 'short' }) + ' ' + clock(ts);
  const tsDate = (ts) => { const d = new Date((ts + tz()) * 1000); return new Date(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()); };

  function tile(label, value, note, warn) {
    return el('div', { class: 'tile' + (warn ? ' warn' : '') },
      el('span', { text: label }), el('b', { text: value }), note ? el('small', { text: note }) : null);
  }

  function renderTotals(t) {
    const periods = [['Today', t.today, t.today_savings], ['Last 7 days', t.last_7d, null],
      ['Last 30 days', t.last_30d, null], ['Lifetime', t.lifetime, t.lifetime_savings]];
    $('hist-tiles').replaceChildren(...periods.map(([label, p, money]) => el('section', { class: 'card' },
      el('h3', { text: label }),
      el('div', { class: 'mid-num', text: kwh(p?.output_wh) }),
      el('div', { class: 'muted small', text: 'used by the home' }),
      el('div', { class: 'rows' },
        el('div', { class: 'row' }, el('span', { text: 'From solar' }), el('b', { text: kwh(p?.solar_wh) })),
        el('div', { class: 'row' }, el('span', { text: 'From the grid' }), el('b', { text: kwh(p?.ac_input_wh) })),
        money ? el('div', { class: 'row' }, el('span', { text: 'Saved, after grid cost' }), el('b', { text: usd(money.net_savings) })) : null))));
  }

  function drawDaily() {
    const svg = $('chart-daily');
    const rows = daily.slice(-days);
    if (!svg.clientWidth) return;
    if (!rows.length) {
      svg.replaceChildren(svgEl('text', { class: 'empty', x: '50%', y: '50%', 'text-anchor': 'middle' }, 'No daily totals yet.'));
      return;
    }
    const peak = Math.max(1, ...rows.map((r) => Math.max((r.solar_kwh || 0) + (r.grid_kwh || 0), r.consumed_kwh || 0)));
    const f = frame(svg, 0, rows.length, niceMax(peak), (v) => v + ' kWh');
    axes(f, 0);
    const slot = (f.box.r - f.box.l) / rows.length, w = Math.max(2, slot * 0.62);
    rows.forEach((r, i) => {
      const x = f.X(i + 0.5) - w / 2, solar = r.solar_kwh || 0, grid = r.grid_kwh || 0;
      if (solar > 0) svg.append(svgEl('rect', { class: 'bar-solar', x, y: f.Y(solar), width: w, height: f.box.b - f.Y(solar) }));
      if (grid > 0) svg.append(svgEl('rect', { class: 'bar-grid', x, y: f.Y(solar + grid), width: w, height: f.Y(solar) - f.Y(solar + grid) }));
    });
    svg.append(svgEl('polyline', { class: 's-home', points: rows.map((r, i) => f.X(i + 0.5).toFixed(1) + ',' + f.Y(r.consumed_kwh || 0).toFixed(1)).join(' ') }));
    const every = Math.ceil(rows.length / Math.max(2, Math.floor((f.box.r - f.box.l) / 70)));
    rows.forEach((r, i) => {
      if ((rows.length - 1 - i) % every === 0) svg.append(xLabel(f, f.X(i + 0.5), shortDay(dayDate(r.date))));
    });
    hover(f, (x) => {
      const i = Math.min(rows.length - 1, Math.floor(x)), r = rows[i];
      return { x: i + 0.5, title: longDay(dayDate(r.date)), lines: ['Solar ' + kwhDay(r.solar_kwh || 0) + ' kWh',
        'Grid ' + kwhDay(r.grid_kwh || 0) + ' kWh', 'Used ' + kwhDay(r.consumed_kwh || 0) + ' kWh'] };
    });
  }

  function renderDaily() {
    const rows = daily.slice(-days);
    const best = (key, low) => rows.reduce((a, r) => (num(r[key]) == null ? a : !a || (low ? r[key] < a[key] : r[key] > a[key]) ? r : a), null);
    const solar = best('solar_kwh'), used = best('consumed_kwh'), low = best('min_soc', true);
    $('hist-records').replaceChildren(
      tile('Best solar day', solar ? kwhDay(solar.solar_kwh) + ' kWh' : '—', solar ? longDay(dayDate(solar.date)) : ''),
      tile('Biggest day of use', used ? kwhDay(used.consumed_kwh) + ' kWh' : '—', used ? longDay(dayDate(used.date)) : ''),
      tile('Lowest battery', low ? pct(low.min_soc) : '—', low ? longDay(dayDate(low.date)) : ''));
    const shown = [...daily].reverse().slice(0, showAll ? daily.length : 14);
    $('hist-table').tBodies[0].replaceChildren(...shown.map((r) => el('tr', {},
      el('td', { text: longDay(dayDate(r.date)) }),
      el('td', { class: r.solar_kwh ? '' : 'zero', text: kwhDay(r.solar_kwh) + ' kWh' }),
      el('td', { class: r.grid_kwh ? '' : 'zero', text: kwhDay(r.grid_kwh) + ' kWh' }),
      el('td', { text: kwhDay(r.consumed_kwh) + ' kWh' }),
      el('td', { text: num(r.min_soc) == null ? '—' : pct(r.min_soc) + ' – ' + pct(r.max_soc) }))));
    $('hist-more').hidden = daily.length <= 14;
    $('hist-more').textContent = showAll ? 'Show the last 14 days' : 'Show all ' + daily.length + ' days';
    $('hist-more').setAttribute('aria-expanded', String(showAll));
    drawDaily();
  }

  function drawForecast() {
    const svg = $('chart-forecast');
    if (!svg.clientWidth) return;
    const rows = (forecast?.forecast || []).filter((r) => num(r.ts) != null);
    if (!forecast?.ready || !rows.length) {
      svg.replaceChildren(svgEl('text', { class: 'empty', x: '50%', y: '50%', 'text-anchor': 'middle' },
        forecast && !forecast.configured ? 'Set a location in Settings to get forecasts.' : 'The forecast is not ready yet.'));
      return;
    }
    const x0 = rows[0].ts - (rows[0].duration_h || 0) * 3600, x1 = rows[rows.length - 1].ts;
    const f = frame(svg, x0, x1, 100, (v) => v + '%');
    const low = num(forecast.low_battery_threshold);
    if (low) svg.append(svgEl('rect', { class: 'zone-low', x: f.box.l, y: f.Y(low), width: f.box.r - f.box.l, height: f.box.b - f.Y(low) }));
    const solarPeak = Math.max(1, ...rows.map((r) => r.solar_w || 0));
    const area = rows.map((r) => f.X(r.ts).toFixed(1) + ',' + f.Y((r.solar_w || 0) / solarPeak * 45).toFixed(1));
    svg.append(svgEl('polygon', { class: 'area-solar', points: f.X(x0) + ',' + f.box.b + ' ' + area.join(' ') + ' ' + f.X(x1) + ',' + f.box.b }));
    axes(f, 0);
    for (let d = localDay(x0) + 1; dayStart(d) < x1; d++) {
      const x = f.X(dayStart(d));
      svg.append(svgEl('line', { class: 'gridline', x1: x, x2: x, y1: f.box.t, y2: f.box.b }));
      if (f.box.r - x > 40) svg.append(svgEl('text', { x: x + 4, y: f.H - 8 }, tsDate(dayStart(d) + 43200).toLocaleDateString('en-US', { weekday: 'short' })));
    }
    svg.append(svgEl('polyline', { class: 'measured', points: [[x0, forecast.starting_soc_pct], ...rows.map((r) => [r.ts, r.predicted_soc])]
      .filter(([, v]) => num(v) != null).map(([t, v]) => f.X(t).toFixed(1) + ',' + f.Y(v).toFixed(1)).join(' ') }));
    if (low) svg.append(svgEl('text', { x: f.box.l + 6, y: f.Y(low) - 5 }, 'Low-battery alert ' + Math.round(low) + '%'));
    svg.append(svgEl('text', { x: f.box.r - 4, y: f.box.t + 12, 'text-anchor': 'end' }, 'Solar peak ' + (solarPeak / 1000).toFixed(1) + ' kW'));
    hover(f, (ts) => {
      const r = nearest(rows.filter((k) => num(k.predicted_soc) != null), ts);
      return r && { ts: r.ts, y: f.Y(r.predicted_soc), title: dayClock(r.ts),
        lines: ['Battery ' + pct(r.predicted_soc) + ' without charging', 'Solar ' + watts(r.solar_w || 0)] };
    });
  }

  function renderForecast() {
    const rows = forecast?.forecast || [];
    const byDay = new Map();
    for (const r of rows) {
      const mid = r.ts - (r.duration_h || 0) * 1800;
      const d = byDay.get(localDay(mid)) || { solar: 0, low: 100 };
      d.solar += (r.solar_w || 0) * (r.duration_h || 0) / 1000;
      if (num(r.predicted_soc) != null) d.low = Math.min(d.low, r.predicted_soc);
      byDay.set(localDay(mid), d);
    }
    const low = num(forecast?.low_battery_threshold) ?? 0;
    $('fc-days').replaceChildren(...[...byDay.entries()].slice(0, 5).map(([d, v]) =>
      tile(longDay(tsDate(dayStart(d) + 43200)), v.solar.toFixed(1) + ' kWh solar',
        v.low <= 0.5 ? 'Would run empty without charging' : 'Low of ' + pct(v.low) + ' without charging', v.low <= low)));
    drawForecast();
    renderSolar(byDay);
  }

  // ---------- solar forecast ----------
  const sky = (cloud) => (cloud == null ? '' : cloud < 25 ? 'Mostly sunny' : cloud < 60 ? 'Partly cloudy' : cloud < 85 ? 'Mostly cloudy' : 'Overcast');
  let sunDays = [];
  function renderSolar(byDay) {
    const rows = forecast?.forecast || [];
    const today = rows.length ? localDay(rows[0].ts - (rows[0].duration_h || 0) * 1800) : null;
    const extra = new Map();
    for (const r of rows) {
      const d = localDay(r.ts - (r.duration_h || 0) * 1800), x = extra.get(d) || { cloud: 0, hours: 0, peak: 0 };
      if ((r.ghi_w_m2 || 0) > 0 && num(r.cloud_cover_pct) != null) { x.cloud += r.cloud_cover_pct * (r.duration_h || 0); x.hours += r.duration_h || 0; }
      x.peak = Math.max(x.peak, r.solar_w || 0);
      extra.set(d, x);
    }
    const soFar = (num(forecast?.today_actual_solar_wh) ?? 0) / 1000;
    sunDays = [...byDay.entries()].slice(0, 5).map(([d, v]) => {
      const x = extra.get(d) || {};
      return { day: d, isToday: d === today, forecast: v.solar, soFar: d === today ? soFar : 0, cloud: x.hours ? x.cloud / x.hours : null, peak: x.peak || 0 };
    });
    const ready = forecast?.ready && sunDays.length;
    $('forecast-sub').textContent = forecast?.weather_stale ? 'The weather data is out of date' : '';
    const t = sunDays[0], tomorrow = sunDays[1];
    const ahead = sunDays.slice(1).reduce((a, d) => (!a || d.forecast > a.forecast ? d : a), null);
    $('sun-tiles').replaceChildren(...(ready ? [
      ['Solar expected today', (t.soFar + t.forecast).toFixed(1) + ' kWh', t.soFar.toFixed(1) + ' kWh so far, ' + t.forecast.toFixed(1) + ' kWh still to come'],
      ['Tomorrow', tomorrow ? tomorrow.forecast.toFixed(1) + ' kWh' : '—', tomorrow ? sky(tomorrow.cloud) : ''],
      ['Best day ahead', ahead ? ahead.forecast.toFixed(1) + ' kWh' : '—', ahead ? longDay(tsDate(dayStart(ahead.day) + 43200)) : ''],
      ['Next 5 days in total', sunDays.reduce((a, d) => a + d.forecast, 0).toFixed(1) + ' kWh', 'Your best recent hour was ' + ((forecast.solar_recent_peak_w || 0) / 1000).toFixed(1) + ' kW'],
    ] : [['Solar forecast', 'Not ready', forecast && !forecast.configured ? 'Set a location in Settings.' : 'It needs a day or so of readings first.']]).map(([label, value, note]) =>
      el('section', { class: 'card' }, el('h3', { text: label }), el('div', { class: 'mid-num', text: value }), el('div', { class: 'muted small', text: note }))));
    $('sun-days').replaceChildren(...sunDays.map((d) => tile(longDay(tsDate(dayStart(d.day) + 43200)) + (d.isToday ? ' (rest of today)' : ''),
      d.forecast.toFixed(1) + ' kWh', [sky(d.cloud), 'peak ' + (d.peak / 1000).toFixed(1) + ' kW'].filter(Boolean).join(' · '))));
    drawSolar(); drawSunWeek();
  }
  function dayLines(svg, f, x0, x1) {
    for (let d = localDay(x0) + 1; dayStart(d) < x1; d++) {
      const x = f.X(dayStart(d));
      svg.append(svgEl('line', { class: 'gridline', x1: x, x2: x, y1: f.box.t, y2: f.box.b }));
      if (f.box.r - x > 40) svg.append(svgEl('text', { x: x + 4, y: f.H - 8 }, tsDate(dayStart(d) + 43200).toLocaleDateString('en-US', { weekday: 'short' })));
    }
  }
  function drawSolar() {
    const svg = $('chart-solar');
    if (!svg.clientWidth) return;
    const rows = (forecast?.forecast || []).filter((r) => num(r.ts) != null);
    if (!forecast?.ready || !rows.length) {
      svg.replaceChildren(svgEl('text', { class: 'empty', x: '50%', y: '50%', 'text-anchor': 'middle' }, 'The solar forecast is not ready yet.'));
      return;
    }
    const x0 = rows[0].ts - (rows[0].duration_h || 0) * 3600, x1 = rows[rows.length - 1].ts;
    const peak = Math.max(500, ...rows.map((r) => r.solar_w || 0));
    const f = frame(svg, x0, x1, niceMax(peak), (v) => (v / 1000) + ' kW');
    axes(f, 0);
    dayLines(svg, f, x0, x1);
    // Each row is stamped with the end of its hour; draw the hour's power at its middle.
    const pts = rows.map((r) => f.X(r.ts - (r.duration_h || 0) * 1800).toFixed(1) + ',' + f.Y(r.solar_w || 0).toFixed(1));
    svg.append(svgEl('polygon', { class: 'area-sun', points: f.X(x0) + ',' + f.box.b + ' ' + pts.join(' ') + ' ' + f.X(x1) + ',' + f.box.b }));
    svg.append(svgEl('polyline', { class: 'line-sun', points: pts.join(' ') }));
    hover(f, (ts) => {
      const mid = (k) => k.ts - (k.duration_h || 0) * 1800;
      const r = rows.reduce((a, k) => (!a || Math.abs(mid(k) - ts) < Math.abs(mid(a) - ts) ? k : a), null);
      return r && { ts: mid(r), x: mid(r), y: f.Y(r.solar_w || 0), title: dayClock(r.ts - (r.duration_h || 0) * 3600) + ' – ' + clock(r.ts),
        lines: ['Solar ' + watts(r.solar_w || 0)] };
    });
    $('sun-note').textContent = rows.some((r) => r.solar_capped) ? 'Some hours are capped at what your panels have actually reached.' : '';
  }
  function drawSunWeek() {
    const svg = $('chart-sunweek');
    if (!svg.clientWidth) return;
    const past = daily.slice(-8).map((r) => ({ label: shortDay(dayDate(r.date)), iso: r.date, measured: r.solar_kwh || 0, forecast: 0 }));
    const iso = (d) => { const x = tsDate(dayStart(d) + 43200); return x.getFullYear() + '-' + String(x.getMonth() + 1).padStart(2, '0') + '-' + String(x.getDate()).padStart(2, '0'); };
    const bars = past.filter((p) => !sunDays.some((d) => iso(d.day) === p.iso)).slice(-7);
    for (const d of sunDays) bars.push({ label: d.isToday ? 'Today' : shortDay(tsDate(dayStart(d.day) + 43200)), measured: d.soFar, forecast: d.forecast, today: d.isToday });
    if (!bars.length) { svg.replaceChildren(svgEl('text', { class: 'empty', x: '50%', y: '50%', 'text-anchor': 'middle' }, 'Nothing to show yet.')); return; }
    const peak = Math.max(1, ...bars.map((b) => b.measured + b.forecast));
    const f = frame(svg, 0, bars.length, niceMax(peak), (v) => v + ' kWh');
    axes(f, 0);
    const slot = (f.box.r - f.box.l) / bars.length, w = Math.max(4, slot * 0.6), skip = Math.ceil(bars.length / Math.max(2, Math.floor((f.box.r - f.box.l) / 58)));
    bars.forEach((b, i) => {
      const x = f.X(i + 0.5) - w / 2;
      if (b.measured > 0) svg.append(svgEl('rect', { class: 'bar-solar', x, y: f.Y(b.measured), width: w, height: f.box.b - f.Y(b.measured) }));
      if (b.forecast > 0) svg.append(svgEl('rect', { class: 'bar-solar-fc', x, y: f.Y(b.measured + b.forecast), width: w, height: f.Y(b.measured) - f.Y(b.measured + b.forecast) }));
      if (b.today || (bars.length - 1 - i) % skip === 0) svg.append(xLabel(f, f.X(i + 0.5), b.label, b.today ? 'today-mark' : ''));
      if (slot > 44) svg.append(svgEl('text', { x: f.X(i + 0.5), y: f.Y(b.measured + b.forecast) - 5, 'text-anchor': 'middle' }, (b.measured + b.forecast).toFixed(1)));
    });
    hover(f, (x) => {
      const i = Math.min(bars.length - 1, Math.floor(x)), b = bars[i];
      const lines = [];
      if (b.measured > 0 || !b.forecast) lines.push('Measured ' + b.measured.toFixed(1) + ' kWh');
      if (b.forecast > 0) lines.push('Forecast ' + b.forecast.toFixed(1) + ' kWh' + (b.today ? ' still to come' : ''));
      return { x: i + 0.5, title: b.label, lines };
    });
  }

  function renderAccuracy(rows) {
    const done = rows.filter((r) => num(r.sunset_forecast_soc_pct) != null && num(r.sunset_forecast_actual_soc_pct) != null);
    $('acc-table').tBodies[0].replaceChildren(...(done.length ? done.map((r) => {
      const err = r.sunset_forecast_actual_soc_pct - r.sunset_forecast_soc_pct, pts = Math.abs(Math.round(err));
      return el('tr', {},
        el('td', { text: longDay(dayDate(r.date)) }),
        el('td', { text: pct(r.sunset_forecast_soc_pct) }),
        el('td', { text: pct(r.sunset_forecast_actual_soc_pct) }),
        el('td', { text: pts === 0 ? 'Spot on' : pts + (pts === 1 ? ' point ' : ' points ') + (err > 0 ? 'too low' : 'too high') }));
    }) : [el('tr', {}, el('td', { colspan: '4', class: 'zero', text: 'No finished nights to compare yet.' }))]));
  }

  function renderDevices(list) {
    $('hist-devices-card').hidden = list.length < 2;
    $('dev-table').tBodies[0].replaceChildren(...list.map((d) => el('tr', {},
      el('td', { text: d.name || d.device_sn }),
      el('td', { text: kwh(d.today?.output_wh) }), el('td', { text: kwh(d.last_30d?.output_wh) }),
      el('td', { text: kwh(d.last_30d?.solar_wh) }), el('td', { text: kwh(d.lifetime?.solar_wh) }))));
  }

  const failed = (id, cols) => (e) => {
    const cell = el('td', { colspan: String(cols), class: 'zero', text: 'Could not load: ' + e.message });
    $(id).tBodies[0].replaceChildren(el('tr', {}, cell));
  };

  function load(force) {
    const sn = deviceSn();
    if (!sn || (!force && loadedFor === sn)) { drawAll(); return; }
    loadedFor = sn;
    const q = '?device_sn=' + encodeURIComponent(sn);
    $('history-sub').textContent = window.V2.status()?.device?.name || '';
    getJson('/api/energy/totals' + q).then(renderTotals).catch(() => {});
    getJson('/api/energy/daily' + q + '&days=90').then((j) => { daily = j.daily || []; renderDaily(); drawSunWeek(); }).catch(failed('hist-table', 5));
    getJson('/api/forecast' + q).then((j) => {
      forecast = j;
      renderForecast();
    }).catch(() => { forecast = null; renderForecast(); });
    getJson('/api/daily_summary' + q + '&days=14').then((j) => renderAccuracy(j.rows || [])).catch(failed('acc-table', 4));
    getJson('/api/energy/devices').then((j) => renderDevices(j.devices || [])).catch(() => {});
  }

  for (const btn of document.querySelectorAll('#hist-range button')) {
    btn.addEventListener('click', () => {
      days = Number(btn.dataset.days);
      for (const b of document.querySelectorAll('#hist-range button')) b.setAttribute('aria-pressed', String(b === btn));
      renderDaily();
    });
  }
  $('hist-more').addEventListener('click', () => { showAll = !showAll; renderDaily(); });

  function drawAll() { drawDaily(); drawForecast(); drawSolar(); drawSunWeek(); }
  window.V2.register('history', { show: load, draw: drawAll });
  window.V2.register('forecast', { show: load, draw: drawAll });
})();
