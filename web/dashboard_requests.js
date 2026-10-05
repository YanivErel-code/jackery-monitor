/* Compatibility for cached app builds that still expect window.DashboardRequests.
 * Current app.js embeds its coordinators so startup never depends on this asset.
 * Keep this file available while older HTML/app scripts may remain cached.
 */
(function (root) {
  'use strict';

  function createKeyedLoader({ load, ttlMs = 0, now = Date.now, cacheable = () => true }) {
    const pending = new Map();
    const cached = new Map();
    let generation = 0;
    return {
      get(key) {
        const previous = cached.get(key);
        if (previous && now() - previous.at < ttlMs) return Promise.resolve(previous.value);
        if (pending.has(key)) return pending.get(key);
        const startedGeneration = generation;
        const request = Promise.resolve().then(() => load(key)).then(value => {
          if (generation === startedGeneration && ttlMs > 0 && cacheable(value)) {
            cached.set(key, { value, at: now() });
          }
          return value;
        }).finally(() => {
          if (pending.get(key) === request) pending.delete(key);
        });
        pending.set(key, request);
        return request;
      },
      clear() { generation++; cached.clear(); pending.clear(); },
    };
  }

  function createStatusFallback({ load, onStatus, staleMs = 6000, now = Date.now }) {
    let lastWsAt = null;
    let wsGeneration = 0;
    let pending = null;
    return {
      markWsStatus() { lastWsAt = now(); wsGeneration++; },
      markDisconnected() { lastWsAt = null; },
      markViewChanged() { lastWsAt = null; wsGeneration++; },
      tick() {
        if (pending) return pending;
        if (lastWsAt !== null && now() - lastWsAt < staleMs) return Promise.resolve();
        const startedGeneration = wsGeneration;
        pending = Promise.resolve().then(load).then(status => {
          // A push received after this read began is newer than the HTTP snapshot.
          if (startedGeneration === wsGeneration) onStatus(status);
        }).catch(() => { /* A later tick retries after a transient failure. */ })
          .finally(() => { pending = null; });
        return pending;
      },
    };
  }

  const api = { createKeyedLoader, createStatusFallback };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.DashboardRequests = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
