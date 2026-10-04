/* Service worker do app. So liga em HTTPS (regra do navegador), e so existe
   pra uma coisa: abrir o app sem rede. A casca (html, manifesto, icones) vem
   da rede quando da e do cache quando nao da. /api/ nunca passa por aqui - a
   fila de batidas do proprio app cuida da falta de conexao. */
var CACHE = "ponto-v1";
var CASCA = ["app", "app.webmanifest", "icone-180.png", "icone-192.png", "icone-512.png"];

self.addEventListener("install", function(e){
  e.waitUntil(caches.open(CACHE).then(function(c){ return c.addAll(CASCA); })
    .then(function(){ return self.skipWaiting(); }));
});
self.addEventListener("activate", function(e){
  e.waitUntil(caches.keys().then(function(ks){
    return Promise.all(ks.filter(function(k){ return k !== CACHE; })
      .map(function(k){ return caches.delete(k); }));
  }).then(function(){ return self.clients.claim(); }));
});
self.addEventListener("fetch", function(e){
  var u = new URL(e.request.url);
  /* SEGURANCA: nada de /api/ fica em cache (seus dados nao ficam guardados
     no service worker). */
  if(e.request.method !== "GET" || u.pathname.indexOf("/api/") >= 0) return;
  e.respondWith(fetch(e.request).then(function(r){
    /* so guarda resposta boa: a tela de senha (redirect) nao vira a casca */
    /* SEGURANCA: so guarda resposta boa: a tela de login nao vira o app offline. */
    if(r.ok && !r.redirected){
      var copia = r.clone();
      caches.open(CACHE).then(function(c){ c.put(e.request, copia); });
    }
    return r;
  }).catch(function(){ return caches.match(e.request); }));
});
