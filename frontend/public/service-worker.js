/* Cache only the static application shell. Business APIs and media never enter Cache Storage. */
const CACHE = 'ai-workbench-shell-v1';
const SHELL = ['/', '/manifest.webmanifest', '/app-icon.svg', '/app-icon-180.png', '/app-icon-192.png', '/app-icon-512.png'];
self.addEventListener('install', event => { event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL))); });
self.addEventListener('activate', event => { event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key.startsWith('ai-workbench-shell-') && key !== CACHE).map(key => caches.delete(key)))).then(() => self.clients.claim())); });
self.addEventListener('message', event => { if (event.data?.type === 'ACTIVATE_UPDATE') self.skipWaiting(); });
self.addEventListener('fetch', event => {
  const request = event.request; const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/')) return;
  if (request.mode === 'navigate' && url.pathname === '/') {
    event.respondWith(fetch(request).then(response => { if (response.ok) { const clone = response.clone(); void caches.open(CACHE).then(cache => cache.put('/', clone)); } return response; }).catch(() => caches.match('/')));
    return;
  }
  if (!/^\/assets\/[A-Za-z0-9_.-]+\.(js|css|woff2?|svg)$/.test(url.pathname) && !SHELL.includes(url.pathname)) return;
  event.respondWith(caches.match(request).then(cached => cached || fetch(request).then(response => { if (response.ok) { const clone = response.clone(); void caches.open(CACHE).then(cache => cache.put(request, clone)); } return response; })));
});
