// Prime Vendas: guarda a "casca" do app no aparelho para abrir sem internet. Dados (/api/*) nunca passam por aqui.
const CACHE = 'primevendas-__VERSAO__';
const CASCA = ['/', '/static/manifest.json', '/static/icons/icon-192.png', '/static/icons/icon-512.png', '/static/icons/logo.png'];

self.addEventListener('install', (ev) => {
  ev.waitUntil(caches.open(CACHE).then((c) => c.addAll(CASCA)));
  self.skipWaiting();
});

self.addEventListener('activate', (ev) => {
  ev.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))));
  self.clients.claim();
});

self.addEventListener('fetch', (ev) => {
  const url = new URL(ev.request.url);
  if (ev.request.method !== 'GET' || url.origin !== location.origin || url.pathname.startsWith('/api/')) return;
  // Com internet busca a versão nova (e atualiza a cópia); sem internet usa a cópia guardada.
  ev.respondWith(
    fetch(ev.request).then((resp) => {
      if (resp.ok && CASCA.includes(url.pathname)) {
        const copia = resp.clone();
        caches.open(CACHE).then((c) => c.put(ev.request, copia));
      }
      return resp;
    }).catch(() => caches.match(ev.request).then((r) => r || caches.match('/')))
  );
});
