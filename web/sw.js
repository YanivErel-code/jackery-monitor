/**
 * Jackery Monitor service worker.
 *
 * Goal: make the app installable as a PWA and keep the UI shell loading
 * fast even when the NAS is briefly unreachable. We cache the static
 * shell (HTML + CSS + JS + icon) on install, and use a network-first
 * strategy for everything else (live API calls always go to the bridge).
 */

const CACHE = 'jackery-shell-v1';
const SHELL = [
  '/',
  '/static/style.css',
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
