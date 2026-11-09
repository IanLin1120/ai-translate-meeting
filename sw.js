/* AI 翻譯會議的 Service Worker（GitHub Pages 網頁版／安裝成 App 的 PWA）
   1. 讓網頁版可以「安裝到桌面／主畫面」，沒有網路時也能打開（介面先用網路上的最新版，失敗才用上次存的）。
   2. coi=1 時加上 cross-origin isolation（COOP/COEP），ONNX Runtime 才能用多執行緒 WebAssembly 加速 AI 運算。
      改寫自 coi-serviceworker (MIT, Guido Zuidhof)；只處理同站請求，模型下載（跨站）不經過這裡。 */
const COI = new URL(self.location.href).searchParams.get('coi') !== '0';
const CACHE = 'aitm-shell-v1';
const SHELL = ['./', 'index.html', 'manifest.webmanifest', 'icons/icon-192.png', 'icons/icon-512.png'];
self.addEventListener('install', e => { self.skipWaiting(); e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {})); });
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
function iso(res) {
  if (!COI || res.status === 0) return res;
  const h = new Headers(res.headers);
  h.set('Cross-Origin-Embedder-Policy', 'credentialless');
  h.set('Cross-Origin-Opener-Policy', 'same-origin');
  h.set('Cross-Origin-Resource-Policy', 'same-origin');
  return new Response(res.body, { status: res.status, statusText: res.statusText, headers: h });
}
self.addEventListener('fetch', e => {
  const r = e.request;
  if (r.method !== 'GET') return;
  if (r.cache === 'only-if-cached' && r.mode !== 'same-origin') return;
  const u = new URL(r.url);
  if (u.origin !== self.location.origin) return;
  const shell = r.mode === 'navigate' || /\/(index\.html|manifest\.webmanifest|icons\/[^/]+\.png)$/.test(u.pathname);
  e.respondWith(fetch(r).then(res => {
    if (shell && res.ok) { const c = res.clone(); caches.open(CACHE).then(x => x.put(r.mode === 'navigate' ? './' : r, c)).catch(() => {}); }
    return iso(res);
  }).catch(async err => {
    if (shell) { const c = await caches.match(r.mode === 'navigate' ? './' : r) || await caches.match('index.html'); if (c) return iso(c); }
    return Response.error();
  }));
});
