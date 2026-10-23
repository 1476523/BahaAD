/* BahaAD 公開模式 Service Worker（版本 {{ cache_version }}）。
   快取兩類東西，讓 APP／PWA 重新整理時不必重抓：
   1. 帶 ?v= 版本號的靜態資源（style.css、各 .js、圖示）——網址跟版本綁在一起，內容不會變；
   2. 番劇封面（/cache/img/<雜湊>）——網址是來源網址的雜湊，內容不會變，快取優先。
   頁面、API、影片一律直接走網路（內容即時、而且依訪客／會員身分不同，不能拿快取頁給人看）。
   每筆快取記下存入時間，超過 90 天的自動清除（最多每天檢查一次）；封面快取上限 600 張，超過就丟最舊的。
   封面快取不跟程式版本綁在一起（改版不會清掉）；沒抓好的占位圖（Cache-Control: no-store）不快取。 */
const STATIC_CACHE = "bahaad-static-{{ cache_version }}";
const IMG_CACHE = "bahaad-img-v1";
const META_CACHE = "bahaad-meta";
const MAX_AGE_MS = 90 * 24 * 60 * 60 * 1000;
const PURGE_EVERY_MS = 24 * 60 * 60 * 1000;
const MAX_IMAGES = 600;
const STAMP = "x-sw-cached-at";
const OFFLINE_HTML =
  '<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">' +
  '<meta name="viewport" content="width=device-width, initial-scale=1">' +
  '<title>BahaAD</title><style>body{margin:0;min-height:100vh;display:flex;align-items:center;' +
  'justify-content:center;background:#1a1a1a;color:#eee;font-family:sans-serif;text-align:center}' +
  'button{margin-top:1rem;padding:.6rem 1.4rem;border:0;border-radius:.5rem;background:#444;color:#fff;font-size:1rem}' +
  '</style></head><body><div><h1>目前沒有網路連線</h1><p>請確認網路後再試一次。</p>' +
  '<button onclick="location.reload()">重新載入</button></div></body></html>';

function isFresh(res) {
  const at = Number(res.headers.get(STAMP)) || 0;
  return at > 0 && Date.now() - at <= MAX_AGE_MS;
}

// 存入時順便記下存入時間，之後才知道什麼時候過期
async function putStamped(cache, req, res) {
  const body = await res.clone().blob();
  const headers = new Headers(res.headers);
  headers.set(STAMP, String(Date.now()));
  await cache.put(req, new Response(body, { status: res.status, statusText: res.statusText, headers: headers }));
}

function cacheable(res, wantImage) {
  if (!res || !res.ok) { return false; }
  if (/no-store/i.test(res.headers.get("Cache-Control") || "")) { return false; }
  return !wantImage || /^image\//i.test(res.headers.get("Content-Type") || "");
}

// 清掉超過 90 天的項目、並把封面快取修剪到上限；最多每天做一次
async function purge() {
  const meta = await caches.open(META_CACHE);
  const last = await meta.match("last-purge");
  const lastAt = last ? Number(await last.text()) || 0 : 0;
  if (Date.now() - lastAt < PURGE_EVERY_MS) { return; }
  for (const name of [STATIC_CACHE, IMG_CACHE]) {
    const cache = await caches.open(name);
    for (const req of await cache.keys()) {
      const res = await cache.match(req);
      if (!res || !isFresh(res)) { await cache.delete(req); }
    }
  }
  const imgs = await caches.open(IMG_CACHE);
  const keys = await imgs.keys();
  for (const req of keys.slice(0, Math.max(0, keys.length - MAX_IMAGES))) { await imgs.delete(req); }
  await meta.put("last-purge", new Response(String(Date.now())));
}

self.addEventListener("install", function () {
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys()
      .then(function (keys) {
        // 只丟掉舊版本的靜態快取；封面快取與記錄用的快取不動
        return Promise.all(keys
          .filter(function (k) { return k.indexOf("bahaad-static-") === 0 && k !== STATIC_CACHE; })
          .map(function (k) { return caches.delete(k); }));
      })
      .then(function () {
        // 導覽預載：頁面請求在 Service Worker 啟動的同時就先發出去，少等一段時間
        return self.registration.navigationPreload ? self.registration.navigationPreload.enable() : null;
      })
      .then(function () { return purge().catch(function () {}); })
      .then(function () { return self.clients.claim(); })
  );
});

// 快取優先：有（而且沒過期）就直接用，沒有才去網路抓、順便存起來
function cacheFirst(event, cacheName, wantImage) {
  const req = event.request;
  return caches.open(cacheName).then(function (cache) {
    return cache.match(req).then(function (hit) {
      if (hit && isFresh(hit)) { return hit; }
      return fetch(req).then(function (res) {
        if (cacheable(res, wantImage)) {
          event.waitUntil(putStamped(cache, req, res).then(function () { return purge(); }).catch(function () {}));
        }
        return res;
      }).catch(function (err) {
        if (hit) { return hit; }  // 過期但網路不通：還是比沒有好
        throw err;
      });
    });
  });
}

self.addEventListener("fetch", function (event) {
  const req = event.request;
  if (req.method !== "GET") { return; }
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) { return; }

  if (req.mode === "navigate") {
    event.respondWith(
      Promise.resolve(event.preloadResponse).then(function (pre) {
        return pre || fetch(req);
      }).catch(function () {
        return new Response(OFFLINE_HTML, { headers: { "Content-Type": "text/html; charset=utf-8" } });
      })
    );
    return;
  }

  if (url.pathname.indexOf("/cache/img/") === 0) {
    event.respondWith(cacheFirst(event, IMG_CACHE, true));
    return;
  }

  if (url.pathname.indexOf("/static/") === 0 && url.search.indexOf("v=") !== -1) {
    event.respondWith(cacheFirst(event, STATIC_CACHE, false));
  }
});
