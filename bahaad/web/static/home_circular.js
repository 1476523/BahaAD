// 首頁本季新番：環狀無限捲動——比照週期表側板（schedule_panel.js）。把卡片清單
// （.newanime-cycle）複製成 3 輪，捲到上／下邊緣就把 scrollTop 平移一整輪回中間，
// 三輪內容一模一樣、平移瞬間看不出跳動，感覺上永遠捲不到底。使用者 2026-09-01。
//
// 樣式維持不變：#newanime-scroll 只是外面多包一層 overflow 容器（樣式由 CSS 的
// #shell.home-circular 那組把首頁鎖在視窗高度內），日期分組、卡片格都照舊。
(function () {
  "use strict";

  var scroller;
  var cycle;
  var cycleHeight = 0;
  var cloned = false;

  function ensureClones() {
    if (cloned || !cycle) {
      return;
    }
    // 一輪內容沒有比可視區高就不需要環狀——維持單輪、正常捲（其實也不會捲）
    if (cycle.offsetHeight <= scroller.clientHeight) {
      return;
    }
    var before = cycle.cloneNode(true);
    var after = cycle.cloneNode(true);
    before.dataset.clone = "1";
    after.dataset.clone = "1";
    var originalImgs = cycle.querySelectorAll("img");
    [before, after].forEach(function (c) {
      c.setAttribute("aria-hidden", "true");
      // 複製出來的卡片不要進 tab 焦點順序（同一張番劇會有 3 份）
      c.querySelectorAll("a, button, input").forEach(function (el) {
        el.tabIndex = -1;
      });
      // 封面圖：clone 出來會各自再對 /cache/img 發一次請求（一頁 ~60 張 × 3 輪 = 180
      // 個請求，還沒進本地快取的那些是 no-store 佔位、每輪都重抓 → 連線風暴、
      // ERR_CONNECTION_RESET，使用者 2026-09-09）。改成 clone 的圖不自己載，鏡射對應的
      // 原圖 src（原圖只抓一次；瀏覽器對 clone 的相同 URL 走記憶體快取，佔位那些等原圖
      // 被 img_retry.js 換好後這裡一起跟上）。
      var clonedImgs = c.querySelectorAll("img");
      for (var i = 0; i < clonedImgs.length && i < originalImgs.length; i++) {
        mirrorImg(originalImgs[i], clonedImgs[i]);
      }
    });
    scroller.insertBefore(before, cycle);
    scroller.appendChild(after);
    cloned = true;
  }

  function mirrorImg(src, dst) {
    if (!src || !dst) { return; }
    dst.removeAttribute("src");
    var apply = function () {
      var s = src.getAttribute("src") || "";
      if (s && dst.getAttribute("src") !== s) { dst.setAttribute("src", s); }
    };
    apply();
    // 原圖之後被 img_retry.js 換成真圖時，clone 也跟著換
    try {
      new MutationObserver(apply).observe(src, { attributes: true, attributeFilter: ["src"] });
    } catch (e) { /* 舊瀏覽器沒 MutationObserver 就算了，clone 維持空 */ }
  }

  // scrollTop 維持落在「中間那一輪」的範圍內：滑出上半就 +一輪、滑出下半就 -一輪。
  function keepCentered() {
    if (!cloned || !cycleHeight) {
      return;
    }
    var st = scroller.scrollTop;
    if (st < cycleHeight * 0.5) {
      scroller.scrollTop = st + cycleHeight;
    } else if (st > cycleHeight * 1.5) {
      scroller.scrollTop = st - cycleHeight;
    }
  }

  // 一輪高度可能因為圖片載入完、視窗縮放換行而變——重量一次、按比例修正 scrollTop
  // 捲到「中間那一輪的第一個日期標題貼齊 scroller 上緣」——`.newanime-cycle` 是
  // flow-root（BFC），第一個標題的 margin-top 被包在該輪裡，所以 scrollTop=cycleHeight
  // 只到那個 margin 的上緣，還要再往下捲掉一個 margin 才切齊。
  function snapToTop() {
    var mid = scroller.querySelectorAll(".newanime-cycle")[1];
    var heading = mid && mid.querySelector(".home-day-heading");
    if (!heading) {
      scroller.scrollTop = cycleHeight;
      return;
    }
    scroller.scrollTop = cycleHeight;
    var delta = heading.getBoundingClientRect().top - scroller.getBoundingClientRect().top;
    scroller.scrollTop += delta;
  }

  function remeasure() {
    if (!cloned) {
      ensureClones();
      if (cloned) {
        cycleHeight = cycle.offsetHeight;
        snapToTop();
      }
      return;
    }
    var next = cycle.offsetHeight;
    if (next && next !== cycleHeight) {
      var frac = cycleHeight ? scroller.scrollTop / cycleHeight : 1;
      cycleHeight = next;
      scroller.scrollTop = frac * cycleHeight;
    }
  }

  function init() {
    scroller = document.getElementById("newanime-scroll");
    cycle = scroller && scroller.querySelector(".newanime-cycle");
    if (!scroller || !cycle) {
      return;
    }
    ensureClones();
    if (!cloned) {
      // 內容不夠高：之後視窗放大縮小或圖片載入可能就夠了，掛個 resize 再試
      window.addEventListener("resize", debounce(remeasure, 200));
      window.addEventListener("load", remeasure);
      return;
    }
    cycleHeight = cycle.offsetHeight;
    snapToTop(); // 中間那一輪的第一個日期標題貼齊上緣（看起來就是清單頂端）
    scroller.addEventListener("scroll", keepCentered, { passive: true });
    window.addEventListener("load", remeasure);
    window.addEventListener("resize", debounce(remeasure, 200));
  }

  function debounce(fn, ms) {
    var t;
    return function () {
      clearTimeout(t);
      t = setTimeout(fn, ms);
    };
  }

  document.addEventListener("DOMContentLoaded", init);
})();
