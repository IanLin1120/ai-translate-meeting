/* 讓 GitHub Pages 網頁版具備 cross-origin isolation(COOP/COEP),
   ONNX Runtime 才能用多執行緒 WebAssembly 加速 AI 運算。
   改寫自 coi-serviceworker (MIT, Guido Zuidhof);只處理同站請求,模型下載(跨站)不經過這裡。 */
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {
  const r = e.request;
  if (r.cache === 'only-if-cached' && r.mode !== 'same-origin') return;
  if (new URL(r.url).origin !== self.location.origin) return;
  e.respondWith(fetch(r).then(res => {
    if (res.status === 0) return res;
    const h = new Headers(res.headers);
    h.set('Cross-Origin-Embedder-Policy', 'credentialless');
    h.set('Cross-Origin-Opener-Policy', 'same-origin');
    h.set('Cross-Origin-Resource-Policy', 'same-origin');
    return new Response(res.body, { status: res.status, statusText: res.statusText, headers: h });
  }).catch(err => { console.error(err); return Response.error(); }));
});
