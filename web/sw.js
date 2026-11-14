/* Remote Desktop service worker (#49).
 *
 * Caches the static app shell so the client opens without a network;
 * NEVER intercepts the control API (/api/*) or the VNC stream
 * (/websockify) — those are always network-only so no stale auth or
 * session response can ever be served.
 *
 * Update strategy: shell documents (/, /touch.js, locales, manifest) are
 * network-first with a cache fallback, so a deploy takes effect on the
 * next load instead of trapping users on a stale shell. Vendored static
 * assets (web/app/*, web/core/*, web/vendor/*, web/include/*) are
 * cache-first: they only change on a release, and the cache version below
 * is bumped with it.
 */
const VERSION = 'v2.6.2';
const CACHE = 'rd-shell-' + VERSION;

// fetched up front so a cold offline start has a shell to show
const PRECACHE = ['/', '/touch.js', '/manifest.webmanifest', '/include/logo.js.png'];

const CACHE_FIRST = /^\/(app|core|vendor|include)\/|^\/app\/images\/icons\//;
const NETWORK_FIRST = /^\/touch\.js$|^\/locales\/|^\/manifest\.webmanifest$/;

self.addEventListener('install', (e) => {
  e.waitUntil((async () => {
    const c = await caches.open(CACHE);
    await Promise.all(PRECACHE.map(async (u) => {
      try {
        const r = await fetch(u, { cache: 'no-store' });
        if (r && r.ok) await c.put(u, r);   // skip redirects (e.g. / -> /login)
      } catch (_) {}
    }));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  // network-only: control API, VNC stream and per-app icons
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/icon/') ||
      url.pathname === '/websockify') return;

  // navigations: network first, offline falls back to the cached shell
  if (req.mode === 'navigate') {
    e.respondWith((async () => {
      try {
        const r = await fetch(req);
        if (r && r.ok && url.pathname === '/') {
          const c = await caches.open(CACHE);
          c.put('/', r.clone());
        }
        return r;
      } catch (_) {
        const c = await caches.open(CACHE);
        return (await c.match('/')) || Response.error();
      }
    })());
    return;
  }

  if (CACHE_FIRST.test(url.pathname)) {
    e.respondWith((async () => {
      const c = await caches.open(CACHE);
      const hit = await c.match(req, { ignoreSearch: true });
      if (hit) return hit;
      try {
        const r = await fetch(req);
        if (r && r.ok) c.put(req, r.clone());
        return r;
      } catch (_) {
        return Response.error();
      }
    })());
    return;
  }

  if (NETWORK_FIRST.test(url.pathname)) {
    e.respondWith((async () => {
      const c = await caches.open(CACHE);
      try {
        const r = await fetch(req);
        if (r && r.ok) c.put(req, r.clone());
        return r;
      } catch (_) {
        const hit = await c.match(req);
        if (hit) return hit;
        return Response.error();
      }
    })());
  }
});
