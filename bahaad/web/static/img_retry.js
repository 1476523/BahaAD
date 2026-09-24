// 封面圖還沒進本地快取時，`/cache/img/<hash>` 會回一張 1x1 佔位圖（不阻塞伺服器、
// 不 302——見 docs/requirements/anime_cache.md）。這支負責過幾秒把那些封面重抓一次
// （這時背景佇列多半已經把圖抓進本地快取），成功才換上去，不會閃破圖
// （使用者 2026-09-09：「有些圖片還是會異常，雖然後面會載入但觀感不佳」）。
(function () {
  "use strict";

  var DELAYS_MS = [1400, 3000, 5500, 9000];
  var TRANSPARENT =
    "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7";

  function isCacheImg(img) {
    var src = img.currentSrc || img.src || "";
    return src.indexOf("/cache/img/") !== -1;
  }

  // 拿得到原始 /cache/img/<hash> 路徑（去掉之前加過的 ?r=；error 時 src 已被換成透明，
  // 所以先看 data-cache-base）
  function baseSrc(img) {
    var s = img.getAttribute("data-cache-base") || img.getAttribute("src") || img.src || "";
    return s.split("#")[0].split("?")[0];
  }

  function scheduleRetry(img, attempt) {
    if (attempt >= DELAYS_MS.length) { return; }
    setTimeout(function () {
      var probe = new Image();
      var url = baseSrc(img) + "?r=" + (attempt + 1);
      probe.onload = function () {
        // 真的抓到圖（不是又一張佔位）才換上去
        if (probe.naturalWidth > 1) {
          img.src = probe.src;
        } else {
          scheduleRetry(img, attempt + 1);
        }
      };
      probe.onerror = function () { scheduleRetry(img, attempt + 1); };
      probe.src = url;
    }, DELAYS_MS[attempt]);
  }

  function handle(img) {
    if (!img || img.tagName !== "IMG" || !isCacheImg(img)) { return; }
    if (img.getAttribute("data-retry") === "on") { return; }
    img.setAttribute("data-retry", "on");
    scheduleRetry(img, 0);
  }

  // load 成功但拿到的是 1x1 佔位圖 → 排重抓
  document.addEventListener(
    "load",
    function (ev) {
      var img = ev.target;
      if (img && img.tagName === "IMG" && isCacheImg(img) && img.naturalWidth <= 1) {
        handle(img);
      }
    },
    true
  );

  // 真的載入失敗（連線被重設等）→ 先蓋透明、再排重抓
  document.addEventListener(
    "error",
    function (ev) {
      var img = ev.target;
      if (img && img.tagName === "IMG" && isCacheImg(img)) {
        var base = baseSrc(img);
        img.src = TRANSPARENT;
        img.setAttribute("data-cache-base", base);
        handle(img);
      }
    },
    true
  );

  // 進頁後掃一次：漏網的（load 事件比這支腳本早觸發）補上
  document.addEventListener("DOMContentLoaded", function () {
    setTimeout(function () {
      var imgs = document.querySelectorAll('img[src*="/cache/img/"]');
      for (var i = 0; i < imgs.length; i++) {
        if (imgs[i].complete && imgs[i].naturalWidth <= 1) { handle(imgs[i]); }
      }
    }, 2500);
  });
})();
