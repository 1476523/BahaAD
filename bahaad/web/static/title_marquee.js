// 週期表側板：項目標題太長被「…」截斷時，讓標題水平跑馬燈捲動看完整
// （使用者 2026-09-01）。純前端、不改樣板文字。
//
//   - 金框「當前更新」那一列：一直捲（不用滑鼠）。
//   - 其餘每一列：滑鼠**路過**就觸發，捲一整趟（去 + 回）才停，中途離開不中斷；
//     滑鼠若還停在上面，捲完接著再一趟。
//
// 捲動用固定速度（px/s，跟標題長短無關）＋兩端各停一下讓你看得清楚。用 Web
// Animations API 排幀，`iterations: 2`（去 + 回）＝一整趟，`onfinish` 時再決定要不要
// 接下一趟（金框那列、或滑鼠還在上面）。
//
// hover 當下不改 DOM：文字進頁面時（prepareAll）就先包進 .marquee-inner span，
// hover 時只量一次寬度 + 呼叫 animate()。動畫建好後把 currentTime 推到「開始捲」那
// 一刻，跳過起始停頓，路過後 ~50ms 就看到在捲。
//
// 金框的 .schedule-entry--recent class 是 schedule_panel.js 展開時才動態加的、還會
// 連同整輪內容 clone 3 份，所以用 MutationObserver 跟著開關。
(function () {
  "use strict";

  var SPEED = 55; // px/秒——固定速度，長短標題都一樣快
  var EDGE_HOLD = 700; // ms，捲到兩端各停這麼久
  var TITLE_SELECTOR = ".schedule-entry-title";
  var ENTRY_SELECTOR = ".schedule-entry";
  var RECENT_CLASS = "schedule-entry--recent";

  function reducedMotion() {
    return (
      window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches
    );
  }

  // 進頁面就先把文字包進 span——hover 時不用再動 DOM。用 DOM 裡有沒有 span 判斷
  // （不是 JS 旗標）：schedule_panel.js clone 出來的那幾輪會帶著 span，但 JS 屬性
  // 不會被 cloneNode 複製，靠旗標會誤判成沒包、再包一層。
  function prepare(title) {
    var span = title.querySelector(":scope > .marquee-inner");
    if (!span) {
      span = document.createElement("span");
      span.className = "marquee-inner";
      span.textContent = title.textContent;
      title.textContent = "";
      title.appendChild(span);
    }
    return span;
  }

  function overflowOf(title) {
    // 每次現量——量一次 scrollWidth 只是一次 reflow，play() 又不是每幀呼叫；快取反而
    // 會把「收合中量到 0」或「clone 排版未穩」的錯值凍住。收合中（clientWidth 0）直接
    // 當沒有溢出、先不捲。
    var cw = title.clientWidth;
    return cw === 0 ? 0 : title.scrollWidth - cw;
  }

  // 捲一整趟（去 + 回）。跑完 onfinish 再看要不要接下一趟。
  function play(title) {
    if (!title || title._marqueeAnim || reducedMotion()) {
      return;
    }
    var shift = overflowOf(title);
    if (shift <= 2) {
      return; // 沒被截斷，不用捲
    }
    var span = prepare(title);
    title.classList.add("is-marquee");

    var scrollMs = Math.round((shift / SPEED) * 1000);
    var total = EDGE_HOLD + scrollMs + EDGE_HOLD;
    var toEnd = EDGE_HOLD / total;
    var atEnd = (EDGE_HOLD + scrollMs) / total;
    var anim = span.animate(
      [
        { transform: "translateX(0px)", offset: 0 },
        { transform: "translateX(0px)", offset: toEnd },
        { transform: "translateX(-" + shift + "px)", offset: atEnd },
        { transform: "translateX(-" + shift + "px)", offset: 1 },
      ],
      {
        duration: total,
        iterations: 2, // 去 + 回 = 一整趟，結束時 transform 回到 0
        direction: "alternate",
        easing: "linear", // 等速——不要 ease，不然像卡一下才動
      }
    );
    // 跳過第一趟的起始停頓，路過後馬上看到在捲
    try {
      anim.currentTime = EDGE_HOLD;
    } catch (e) {
      /* 少數瀏覽器 currentTime 邊界情況，忽略 */
    }
    anim.onfinish = function () {
      anim.cancel();
      title._marqueeAnim = null;
      title.classList.remove("is-marquee");
      // 金框那列、或滑鼠還停在上面 → 接著再捲一趟
      if (title._alwaysMarquee || title._hovered) {
        play(title);
      }
    };
    title._marqueeAnim = anim;
  }

  function titleOf(entry) {
    return entry.querySelector(TITLE_SELECTOR);
  }

  function prepareAll(scope) {
    scope.querySelectorAll(TITLE_SELECTOR).forEach(prepare);
  }

  // 滑鼠路過某一列就觸發，離開不中斷（只清掉「還在上面」的旗標，讓這一趟自然跑完）
  document.addEventListener("mouseover", function (e) {
    var entry = e.target.closest(ENTRY_SELECTOR);
    if (!entry) {
      return;
    }
    var title = titleOf(entry);
    if (title) {
      title._hovered = true;
      play(title);
    }
  });

  document.addEventListener("mouseout", function (e) {
    var entry = e.target.closest(ENTRY_SELECTOR);
    if (!entry || entry.contains(e.relatedTarget)) {
      return; // 還在同一列裡移動
    }
    var title = titleOf(entry);
    if (title) {
      title._hovered = false; // 不 cancel——這一趟跑完才停
    }
  });

  function setAlways(title, on) {
    if (!title) {
      return;
    }
    title._alwaysMarquee = on;
    if (on) {
      play(title);
    }
  }

  function syncRecent(scope) {
    scope.querySelectorAll("." + RECENT_CLASS).forEach(function (entry) {
      setAlways(titleOf(entry), true);
    });
  }

  function initObserver() {
    var scroll = document.getElementById("schedule-panel-scroll");
    if (!scroll || !window.MutationObserver) {
      return;
    }
    prepareAll(scroll);
    var observer = new MutationObserver(function (mutations) {
      mutations.forEach(function (m) {
        if (m.type === "childList") {
          m.addedNodes.forEach(function (node) {
            if (node.nodeType === 1) {
              prepareAll(node);
              syncRecent(node);
            }
          });
          return;
        }
        var target = m.target;
        if (!target.matches || !target.matches(ENTRY_SELECTOR)) {
          return;
        }
        setAlways(titleOf(target), target.classList.contains(RECENT_CLASS));
      });
    });
    observer.observe(scroll, {
      attributes: true,
      attributeFilter: ["class"],
      subtree: true,
      childList: true,
    });
    syncRecent(scroll);
  }

  document.addEventListener("DOMContentLoaded", initObserver);
})();
