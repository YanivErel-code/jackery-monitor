(() => {
  const element = (id) => document.getElementById(id);
  let snapshot = { configured: false, devices: [] };
  let selected = new Set();
  let countryHydrated = false;
  let loading = false;

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
    } finally {
      button.disabled = false;
    }
  });

  element('lg-disconnect')?.addEventListener('click', async () => {
    if (!confirm('Remove the saved LG token? Configured protection will continue to hold charging OFF until reconnected or explicitly disabled.')) return;
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
  setInterval(() => {
    if (!document.hidden && (!element('tab-settings')?.hidden || !element('tab-automation')?.hidden)) load();
  }, 10000);
})();
