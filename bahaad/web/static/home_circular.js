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
      // 個請求同時打上來 → 連線風暴，AdGuard 之類的過濾軟體擋不住就 RST，使用者
      // 2026-09-09 `net::ERR_CONNECTION_RESET`）。clone 的圖一律**先不載**，等它真的
      // 被捲進可視範圍（IntersectionObserver）才從對應原圖抄 src——初次載入時 before／
      // after 兩輪都在視窗外，等於只剩中間那一輪的圖在請求。
      var clonedImgs = c.querySelectorAll("img");
      for (var i = 0; i < clonedImgs.length && i < originalImgs.length; i++) {
        deferCloneImg(originalImgs[i], clonedImgs[i]);
      }
    });
    scroller.insertBefore(before, cycle);
    scroller.appendChild(after);
    cloned = true;
  }

  // 複製輪的封面：一律**先不載**，捲進可視範圍才交給 img_loader.js 處理
  // （它會走 /cache/img-batch 批次、或直接套已經抓過的圖，絕不逐張對 /cache/img 發請求
  // ——逐張突發正是 AdGuard 之類軟體重設連線的原因，使用者 2026-09-09）。
  var cloneObserver = null;
  function getCloneObserver() {
    if (cloneObserver !== null) { return cloneObserver; }
    try {
      cloneObserver = new IntersectionObserver(function (entries) {
        entries.forEach(function (e) {
          if (!e.isIntersecting) { return; }
          var img = e.target;
          // 原圖已經有真圖（img_loader 補好的 data: URI）→ 直接抄，不必再要
          var origSrc = img._origImg && (img._origImg.getAttribute("src") || "");
          if (origSrc && origSrc.slice(0, 5) === "data:") {
            img.src = origSrc;
          } else if (typeof window.__imgLoaderScan === "function") {
            img.setAttribute("data-cache-src", img._dcs || "");
            window.__imgLoaderScan(img);
          }
          cloneObserver.unobserve(img);
        });
      }, { root: scroller, rootMargin: "400px" });
    } catch (err) {
      cloneObserver = false;
    }
    return cloneObserver;
  }

  function deferCloneImg(orig, clone) {
    if (!orig || !clone) { return; }
    clone._dcs = clone.getAttribute("data-cache-src") || ""; // 收起來，捲到才放回
    clone.removeAttribute("src");
    clone.removeAttribute("srcset");
    clone.removeAttribute("data-cache-src");
    clone._origImg = orig;
    var obs = getCloneObserver();
    if (obs) {
      obs.observe(clone);
    } else if (clone._dcs) {
      clone.setAttribute("data-cache-src", clone._dcs);
      if (typeof window.__imgLoaderScan === "function") { window.__imgLoaderScan(clone); }
    }
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
