const CACHE_NAME = "primesys-produtos-v3";
// Só os ícones entram em cache (praticamente nunca mudam, e ajudam o app a
// instalar/aparecer corretamente no Android). Todo o resto — a página em si
// e as chamadas de API — vai direto pra rede, sem nenhum cache no meio.
// Um app de consulta de preço/estoque mostrando dado desatualizado é pior
// do que ele demorar meio segundo a mais pra carregar.
const ICONES = ["/static/icons/icon-192.png", "/static/icons/icon-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(ICONES)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.map((k) => caches.delete(k))))
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (!ICONES.includes(url.pathname)) {
    return; // deixa o navegador buscar normalmente, sem passar pelo service worker
  }
  event.respondWith(caches.match(event.request).then((cached) => cached || fetch(event.request)));
});
