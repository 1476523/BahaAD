/* BahaAD 公開模式 Service Worker（版本 {{ cache_version }}）。
   只快取帶 ?v= 版本號的靜態資源（style.css、各 .js、圖示），其餘（頁面、API、影片、封面圖）
   一律直接走網路——頁面內容即時、而且依訪客／會員身分不同，不能拿快取頁給人看。 */
const CACHE = "bahaad-static-{{ cache_version }}";
const OFFLINE_HTML =
  '<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">' +
  '<meta name="viewport" content="width=device-width, initial-scale=1">' +
  '<title>BahaAD</title><style>body{margin:0;min-height:100vh;display:flex;align-items:center;' +
  'justify-content:center;background:#1a1a1a;color:#eee;font-family:sans-serif;text-align:center}' +
  'button{margin-top:1rem;padding:.6rem 1.4rem;border:0;border-radius:.5rem;background:#444;color:#fff;font-size:1rem}' +
  '</style></head><body><div><h1>目前沒有網路連線</h1><p>請確認網路後再試一次。</p>' +
  '<button onclick="location.reload()">重新載入</button></div></body></html>';

self.addEventListener("install", function () {
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys()
      .then(function (keys) {
        return Promise.all(keys.filter(function (k) { return k !== CACHE; })
          .map(function (k) { return caches.delete(k); }));
      })
      .then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function (event) {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  if (req.mode === "navigate") {
    event.respondWith(fetch(req).catch(function () {
      return new Response(OFFLINE_HTML, { headers: { "Content-Type": "text/html; charset=utf-8" } });
    }));
    return;
  }

  if (url.pathname.startsWith("/static/") && url.search.indexOf("v=") !== -1) {
    event.respondWith(caches.open(CACHE).then(function (cache) {
      return cache.match(req).then(function (hit) {
        if (hit) return hit;
        return fetch(req).then(function (res) {
          if (res.ok) cache.put(req, res.clone());
          return res;
        });
      });
    }));
  }
});
