'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const appSource = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');

function conflictHarness({ failDisable = false } = {}) {
  const elements = new Map();
  const requests = [];
  const device = { device_sn: 'device-A', name: 'Battery A' };
  const rules = ['low', 'high'].map((id, index) => ({
    id, name: id, enabled: true, jackery_device_sn: device.device_sn,
    operator: index ? '>=' : '<=', value: index ? 20 : 5,
    action: index ? 'off' : 'on', kasa_host: '192.0.2.1',
  }));
  for (const id of ['sc-conflict-banner', 'sc-conflict-text', 'sc-conflict-list',
    'sc-conflict-disable', 'sc-status', 'auto-rules', 'auto-rules-filter-name',
    'auto-rules-filter-toggle']) {
    elements.set(id, {
      hidden: true, disabled: false, textContent: '', innerHTML: '', dataset: {},
      listeners: {}, querySelectorAll: () => [],
      addEventListener(event, handler) { this.listeners[event] = handler; },
    });
  }
  elements.get('sc-conflict-disable').textContent = 'Disable them';
  const context = vm.createContext({
    $: id => elements.get(id),
    document: { getElementById: id => elements.get(id) },
    activeJackeryDevice: () => device,
    fetch: async (url, options) => {
      requests.push({ url, options });
      if (url === '/api/automation/rules/disable') {
        if (failDisable) return { ok: false, status: 503 };
        const ids = JSON.parse(options.body).rule_ids;
        const disabled = rules.filter(rule => ids.includes(rule.id) && rule.enabled);
        disabled.forEach(rule => { rule.enabled = false; });
        return { ok: true, json: async () => ({ ok: true, disabled: disabled.map(rule => rule.id) }) };
      }
      assert.equal(url, '/api/automation/rules');
      return { ok: true, json: async () => ({ rules: JSON.parse(JSON.stringify(rules)) }) };
    },
    loadSmartCharge: async () => context.renderSmartChargeConflicts(
      rules.filter(rule => rule.enabled), 'active'),
  });
  vm.runInContext("let _allRules = []; let _ruleFilterMode = 'active';", context);
  for (const name of ['escapeHtml', 'loadRules', 'renderRulesWithFilter',
    'renderAutomationRules', 'renderSmartChargeConflicts']) {
    const start = appSource.search(new RegExp(`^(?:async )?function ${name}\\(`, 'm'));
    const end = appSource.indexOf('\n}', start) + 2;
    assert.ok(start >= 0 && end > start, `missing ${name}`);
    vm.runInContext(appSource.slice(start, end), context);
  }
  const start = appSource.indexOf("document.getElementById('sc-conflict-disable')?.addEventListener");
  const end = appSource.indexOf("document.getElementById('sc-conflict-dismiss')", start);
  assert.ok(start >= 0 && end > start);
  vm.runInContext(appSource.slice(start, end), context);
  const button = elements.get('sc-conflict-disable');
  const click = () => button.listeners.click({ currentTarget: button });
  return { context, elements, rules, requests, button, click };
}

test('disabling conflicts refreshes the displayed rule switches and clears the warning', async () => {
  const page = conflictHarness();
  await page.context.loadRules();
  await page.context.loadSmartCharge();
  assert.equal((page.elements.get('auto-rules').innerHTML.match(/ checked/g) || []).length, 2);
  assert.equal(page.elements.get('sc-conflict-banner').hidden, false);

  await page.click();

  assert.ok(page.rules.every(rule => !rule.enabled));
  assert.doesNotMatch(page.elements.get('auto-rules').innerHTML, / checked/);
  assert.equal((page.elements.get('auto-rules').innerHTML.match(/paused/g) || []).length, 2);
  assert.equal(page.elements.get('sc-conflict-banner').hidden, true);
  const posted = page.requests.find(request => request.options?.method === 'POST');
  assert.deepEqual(JSON.parse(posted.options.body), { rule_ids: ['low', 'high'] });
});

test('successful disable confirms the result and restores the button for future conflicts', async () => {
  const page = conflictHarness();
  await page.context.loadSmartCharge();
  await page.click();

  assert.equal(page.elements.get('sc-status').hidden, false);
  assert.equal(page.elements.get('sc-status').textContent, 'Conflicting rules disabled.');
  assert.equal(page.button.disabled, false);
  assert.equal(page.button.textContent, 'Disable them');

  page.rules[0].enabled = true;
  await page.context.loadSmartCharge();
  assert.equal(page.elements.get('sc-conflict-banner').hidden, false);
  assert.equal(page.button.disabled, false);
  await page.click();
  assert.equal(page.rules[0].enabled, false);
  assert.equal(page.elements.get('sc-conflict-banner').hidden, true);
});

test('a rejected disable keeps rules enabled and allows retry with an error message', async () => {
  const page = conflictHarness({ failDisable: true });
  await page.context.loadRules();
  await page.context.loadSmartCharge();
  await page.click();

  assert.ok(page.rules.every(rule => rule.enabled));
  assert.equal(page.elements.get('sc-conflict-banner').hidden, false);
  assert.equal(page.button.disabled, false);
  assert.equal(page.button.textContent, 'Disable them');
  assert.equal(page.elements.get('sc-status').hidden, false);
  assert.match(page.elements.get('sc-status').textContent, /Disable failed: HTTP 503/);
});

test('no listed conflicts do not send a disable request', async () => {
  const page = conflictHarness();
  await page.click();
  assert.equal(page.requests.length, 0);
  page.button.dataset.ruleIds = 'not JSON';
  await page.click();
  assert.equal(page.requests.length, 0);
});
