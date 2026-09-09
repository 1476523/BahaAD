// 封面圖批次載入。
//
// 番劇卡片的封面 `<img>` 在 HTML 裡是 `data-cache-src`（沒有 src），瀏覽器不會一進頁
// 就對 127.0.0.1 發 ~60 張突發請求——本機的過濾／代理軟體（AdGuard for Windows 之類）
// 擋不住這種突發會直接把連線重設 → 封面破圖（使用者 2026-09-09：
// `net::ERR_CONNECTION_RESET` on /cache/img）。
//
// 這裡把整頁封面的 hash 收集起來，用 `POST /cache/img-batch` **一個請求拿回一批**
// （伺服器回 base64 data URI），連線數從 ~60 降到 2-3。伺服器還沒抓到的封面回 null →
// 先留佔位，過幾秒再要一批（背景佇列這時多半抓好了）。
//
// batch 端點不可用（舊版後端）時，退回逐張載入、併發上限 5。
(function () {
  "use strict";

  var BATCH_URL = "/cache/img-batch";
  var CHUNK = 20; // 每個 batch 請求最多幾個 hash
  var MAX_INFLIGHT = 2; // 同時最多幾個 batch 請求
  var REBATCH_DELAYS = [2500, 6000, 12000, 20000]; // null（還沒抓好）的重試節奏
  var FALLBACK_CONCURRENCY = 5;

  var byHash = {}; // hash -> [img, img, ...]（等這一批 batch 回來的）
  var resolved = {}; // hash -> data URI（已經拿到的——換頁／局部刷新重插入的圖直接套，不重打）
  var pending = []; // 還沒要過的 hash
  var inflight = 0;
  var rebatchRound = 0;
  var batchBroken = false;

  function hashOf(img) {
    var s = img.getAttribute("data-cache-src") || "";
    var i = s.indexOf("/cache/img/");
    return i === -1 ? null : s.slice(i + "/cache/img/".length).split("?")[0].split("#")[0];
  }

  function setImg(img, dataUri) {
    img.removeAttribute("data-cache-src");
    // data: URI 沒有網路請求可延遲，`loading=lazy` 反而會讓 Chrome 拖著不 decode——拿掉
    img.removeAttribute("loading");
    img.src = dataUri;
  }

  function collect(root) {
    var imgs = (root || document).querySelectorAll("img[data-cache-src]");
    for (var i = 0; i < imgs.length; i++) {
      var img = imgs[i];
      if (img.__il) { continue; }
      img.__il = true;
      var h = hashOf(img);
      if (!h) { img.src = img.getAttribute("data-cache-src"); continue; }
      // 這張圖之前就抓過了（局部刷新／下載列表每 2 秒重繪）→ 直接套記憶體裡的，不再打 batch
      if (resolved[h]) { setImg(img, resolved[h]); continue; }
      img.__ilHash = h;
      if (!byHash[h]) { byHash[h] = []; pending.push(h); }
      byHash[h].push(img);
    }
    pump();
  }

  function apply(hash, dataUri) {
    if (!dataUri) { return; }
    resolved[hash] = dataUri;
    var list = byHash[hash];
    if (list) {
      for (var i = 0; i < list.length; i++) { setImg(list[i], dataUri); }
      delete byHash[hash];
    }
  }

  function pump() {
    if (batchBroken) { return fallbackPump(); }
    while (inflight < MAX_INFLIGHT && pending.length) {
      sendBatch(pending.splice(0, CHUNK));
    }
  }

  function sendBatch(hashes) {
    inflight++;
    fetch(BATCH_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
      body: JSON.stringify({ hashes: hashes }),
    })
      .then(function (r) {
        if (!r.ok) { throw new Error("batch " + r.status); }
        return r.json();
      })
      .then(function (map) {
        inflight--;
        var stillNull = [];
        for (var i = 0; i < hashes.length; i++) {
          var h = hashes[i];
          if (map[h]) { apply(h, map[h]); }
          else if (byHash[h]) { stillNull.push(h); }
        }
        if (stillNull.length) { scheduleRebatch(stillNull); }
        pump();
      })
      .catch(function () {
        inflight--;
        batchBroken = true; // batch 端點掛了 → 之後全部退回逐張載入
        for (var i = 0; i < hashes.length; i++) {
          if (byHash[hashes[i]]) { pending.push(hashes[i]); }
        }
        fallbackPump();
      });
  }

  var rebatchQueue = [];
  var rebatchTimer = null;
  function scheduleRebatch(hashes) {
    for (var i = 0; i < hashes.length; i++) { rebatchQueue.push(hashes[i]); }
    if (rebatchTimer || rebatchRound >= REBATCH_DELAYS.length) { return; }
    rebatchTimer = setTimeout(function () {
      rebatchTimer = null;
      var q = rebatchQueue;
      rebatchQueue = [];
      for (var i = 0; i < q.length; i++) {
        if (byHash[q[i]] && pending.indexOf(q[i]) === -1) { pending.push(q[i]); }
      }
      pump();
    }, REBATCH_DELAYS[rebatchRound++]);
  }

  // ---- 退回：逐張載入、併發上限 ----
  var fbActive = 0;
  function fallbackPump() {
    while (fbActive < FALLBACK_CONCURRENCY && pending.length) {
      var h = pending.shift();
      var list = byHash[h];
      if (!list || !list.length) { continue; }
      fbActive++;
      (function (hash, imgs) {
        var probe = new Image();
        var finish = function () {
          fbActive--;
          setTimeout(fallbackPump, 0);
        };
        probe.onload = function () {
          for (var i = 0; i < imgs.length; i++) {
            imgs[i].removeAttribute("data-cache-src");
            imgs[i].removeAttribute("loading");
            imgs[i].src = probe.src;
          }
          delete byHash[hash];
          finish();
        };
        probe.onerror = finish;
        probe.src = "/cache/img/" + hash;
      })(h, list);
    }
  }

  // 邊解析邊收
  try {
    new MutationObserver(function (muts) {
      for (var m = 0; m < muts.length; m++) {
        var added = muts[m].addedNodes;
        for (var n = 0; n < added.length; n++) {
          var node = added[n];
          if (node.nodeType !== 1) { continue; }
          if (node.tagName === "IMG" && node.getAttribute("data-cache-src")) {
            collect(node.parentNode || document);
          } else if (node.querySelector && node.querySelector("img[data-cache-src]")) {
            collect(node);
          }
        }
      }
    }).observe(document.documentElement, { childList: true, subtree: true });
  } catch (e) {
    /* 舊瀏覽器：靠下面兩個事件 */
  }

  document.addEventListener("DOMContentLoaded", function () { collect(document); });
  window.addEventListener("load", function () { collect(document); });
  window.__imgLoaderScan = collect;
})();
