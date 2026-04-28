/**
 * Jackery Monitor service worker.
 *
 * Goal: make the app installable as a PWA and keep the UI shell loading
 * fast even when the NAS is briefly unreachable. We cache the static
 * shell (HTML + CSS + JS + icon) on install, and use a network-first
 * strategy for everything else (live API calls always go to the bridge).
 */

// Bump this whenever the static-shell semantics change (or whenever a
// stuck cache is suspected). The activate handler drops every cache that
// isn't the current name, so installed clients get fresh files on the
// next navigation. Don't include CSS in the shell — the browser's
// standard cache + FastAPI's Last-Modified header handle revalidation
// fine, and a stale CSS in the SW cache once cost us a "phantom Confirm
// password field on the login page" debugging spiral.
const CACHE = 'jackery-shell-v3';
const SHELL = [
  '/',
  '/static/app.js',
  '/static/icon.svg',
  '/manifest.webmanifest',
];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  // Drop any older caches so an updated shell wins immediately.
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  // Don't intercept WebSocket upgrades, /api/* (live data), or /ws.
  if (url.pathname.startsWith('/api/') || url.pathname === '/ws') return;

  // Network-first for everything else — falls back to cached shell when
  // offline so the dashboard still loads its frame and shows the last
  // telemetry the WS pushed before the connection dropped.
  event.respondWith(
    fetch(req)
      .then((resp) => {
        if (resp && resp.ok && SHELL.includes(url.pathname)) {
          const copy = resp.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return resp;
      })
      .catch(() => caches.match(req).then((c) => c || caches.match('/')))
  );
});
