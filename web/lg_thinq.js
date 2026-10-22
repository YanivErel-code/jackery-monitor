(() => {
  const element = (id) => document.getElementById(id);
  let snapshot = { configured: false, devices: [] };
  let selected = new Set();
  let countryHydrated = false;
  let loading = false;
  let protectionEnabled = false;
  let statusRequestError = null;
  let statusLoaded = false;

  function renderWarning() {
    const banner = element('automation-lg-warning');
    const detail = element('automation-lg-warning-detail');
    const dot = element('tab-automation-lg-dot');
    const warnings = [...(snapshot.warnings || [])];
    if (statusRequestError) warnings.unshift(statusRequestError);
    if (snapshot.error) warnings.push(snapshot.error);
    if (!snapshot.configured && (protectionEnabled || snapshot.protection_enabled)) warnings.push('LG token is missing or unreadable; reconnect LG ThinQ in Settings');
    if (!snapshot.error) {
      for (const device of snapshot.devices || []) {
        if (device.error) warnings.push(`${device.alias}: ${device.error}`);
        else if (!device.fresh) warnings.push(`${device.alias}: status is stale or unavailable`);
        else if (device.activity === 'unknown') warnings.push(`${device.alias}: appliance state is not recognized`);
      }
    }
    if (protectionEnabled) {
      const known = new Set((snapshot.devices || []).map(device => device.device_id));
      if (!selected.size) warnings.push('No LG appliances are selected for this Jackery');
      else if (snapshot.configured && !snapshot.error && [...selected].some(deviceId => !known.has(deviceId))) {
        warnings.push('A selected LG appliance is missing from the LG device list');
      }
    }
    const visible = (statusLoaded || statusRequestError) && (snapshot.configured || snapshot.protection_enabled || protectionEnabled || selected.size || statusRequestError) && warnings.length > 0;
    if (banner) banner.hidden = !visible;
    if (dot) dot.hidden = !visible;
    if (detail) detail.textContent = visible ? [...new Set(warnings)].join('; ') : '';
  }

  function describe(device) {
    const activity = device.fresh
      ? (device.assumed_off ? 'offline — treated as OFF' : device.activity)
      : (device.error ? `unknown · ${device.error}` : 'unknown / stale');
    const remaining = !device.fresh || device.remaining_minutes == null ? '' : ` · ${device.remaining_minutes} min remaining`;
    const finish = device.fresh && device.remaining_minutes > 0 && device.observed_at
      ? ` · estimated finish ${new Date((device.observed_at + device.remaining_minutes * 60) * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}` : '';
    const states = device.fresh && !device.assumed_off ? (device.states || []).join(', ') : '';
    return `${device.alias} · ${device.device_type || 'LG appliance'} · ${activity}${remaining}${finish}${states ? ` · ${states}` : ''}`;
  }

  function render() {
    renderWarning();
    const status = element('lg-connection-status');
    if (status) status.textContent = snapshot.configured ? (snapshot.error || 'Token saved · read-only') : 'Not connected';
    if (!countryHydrated && snapshot.country_code) {
      element('lg-country').value = snapshot.country_code;
      countryHydrated = true;
    }
    const appliances = element('lg-appliances');
    if (appliances) {
      appliances.replaceChildren();
      for (const device of snapshot.devices || []) {
        const row = document.createElement('div');
        row.className = 'lg-device-row';
        row.textContent = describe(device);
        appliances.appendChild(row);
      }
      if (!snapshot.devices?.length) appliances.textContent = 'Connect or refresh to discover your LG appliances.';
    }
    const list = element('sc-lg-devices');
    if (!list) return;
    list.replaceChildren();
    const known = new Set();
    for (const device of snapshot.devices || []) {
      known.add(device.device_id);
      addChoice(device.device_id, describe(device));
    }
    for (const deviceId of selected) {
      if (!known.has(deviceId)) addChoice(deviceId, 'Previously selected appliance · unavailable / unknown');
    }
    if (!list.childElementCount) list.textContent = 'No appliances discovered. Connect LG ThinQ in Settings → General.';

    function addChoice(deviceId, text) {
      const label = document.createElement('label');
      label.className = 'lg-device-row keep-awake-row';
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.checked = selected.has(deviceId);
      checkbox.addEventListener('change', () => {
        if (checkbox.checked) selected.add(deviceId);
        else selected.delete(deviceId);
      });
      const description = document.createElement('span');
      description.textContent = text;
      label.append(checkbox, description);
      list.appendChild(label);
    }
  }

  async function request(path, options = {}) {
    const response = await fetch(`/api/lg_thinq/${path}`, options);
    const payload = await response.json();
    if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'LG connection request failed');
    snapshot = payload;
    statusRequestError = null;
    statusLoaded = true;
    render();
    return payload;
  }

  async function load() {
    if (loading) return;
    loading = true;
    try {
      await request('status');
    } catch {
      element('lg-connection-status').textContent = 'LG status unavailable';
      statusRequestError = 'Cannot refresh LG monitoring status; the monitoring API or network is unavailable';
      renderWarning();
    } finally {
      loading = false;
    }
  }

  element('lg-credentials-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const tokenInput = element('lg-token');
    const token = tokenInput.value.trim();
    tokenInput.value = '';
    const button = element('lg-connect');
    button.disabled = true;
    element('lg-message').textContent = 'Verifying read-only access…';
    try {
      await request('credentials', { method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ token, country_code: element('lg-country').value.trim().toUpperCase() }) });
      element('lg-message').textContent = 'Connected. Token stored encrypted; no appliance controls were requested.';
    } catch (error) {
      element('lg-message').textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });

  element('lg-refresh')?.addEventListener('click', async () => {
    const button = element('lg-refresh');
    button.disabled = true;
    try {
      await request('refresh', { method: 'POST' });
      element('lg-message').textContent = snapshot.error || 'Appliance status refreshed.';
    } catch (error) {
      element('lg-message').textContent = error.message;
      statusRequestError = 'LG status refresh failed; monitoring is unavailable';
      renderWarning();
    } finally {
      button.disabled = false;
    }
  });

  element('lg-disconnect')?.addEventListener('click', async () => {
    if (!confirm('Remove the saved LG token? Automatic LG checks will be unavailable and will not block charging. Manual reservations still apply.')) return;
    try {
      await request('credentials', { method: 'DELETE' });
      element('lg-message').textContent = 'Token removed. Appliance protection settings are unchanged.';
    } catch (error) {
      element('lg-message').textContent = error.message;
    }
  });

  window.LGThinQ = {
    load,
    hydrate(config) {
      selected = new Set(config.lg_device_ids || []);
      protectionEnabled = config.lg_interlock_enabled === true;
      element('sc-lg-enabled').checked = config.lg_interlock_enabled === true;
      element('sc-lg-hold').checked = config.lg_manual_hold === true;
      render();
      load();
    },
    config() {
      return { lg_interlock_enabled: element('sc-lg-enabled').checked,
        lg_device_ids: Array.from(selected), lg_manual_hold: element('sc-lg-hold').checked };
    },
  };
  window.addEventListener?.('load', load, { once: true });
  setInterval(() => {
    if (!document.hidden && (snapshot.configured || snapshot.protection_enabled || protectionEnabled || selected.size || !element('tab-settings')?.hidden || !element('tab-automation')?.hidden)) return load();
  }, 10000);
})();
