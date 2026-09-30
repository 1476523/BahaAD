// 首頁週期表側板。見 docs/requirements/web_redesign_round2.md 階段 2-1。
//
// 三件事：
//   1. 展開／收合切換鈕（鈕在 base.html 的 .content-top 裡，見階段 1）。展開時給
//      #shell 掛 .schedule-open，CSS 把鈕 position:fixed 到 fixed 側板的左緣、跟著
//      一起浮，不被側板蓋住（階段 2）
//   2. 無限環狀捲動：伺服器只渲染一輪「週一～週日」，這裡複製成 3 輪，捲到邊緣就把
//      scrollTop 平移一整輪，感覺上可以一直往下（週日接週一）或往上（週一接週日）
//   3. 每次展開時，把「最近剛更新過」的那筆用金框框住並捲到側板最上方——判斷方式是
//      純前端算「現在時間往回，最近一個已過的週期表時段」（規格開放問題定案 (a)）
(function () {
  "use strict";

  var GOLD_CLASS = "schedule-entry--recent"; // 對應 style.css 的金色外框

  function currentVariant() {
    return document.documentElement.dataset.theme === "light" ? "black" : "white";
  }

  // weekday: 1=週一…7=週日（跟 store/schedule_list.py 一致）；hhmm: "HH:MM"
  // 回傳「距離最近一次這個時段，過了幾分鐘」（0 ~ 7*1440），parse 不出來回 null
  function minutesSinceSlot(weekday, hhmm) {
    var parts = String(hhmm).split(":");
    var h = parseInt(parts[0], 10);
    var m = parseInt(parts[1], 10);
    if (isNaN(h) || isNaN(m)) {
      return null;
    }
    var now = new Date();
    var jsDow = now.getDay(); // 0=週日…6=週六
    var isoDow = jsDow === 0 ? 7 : jsDow; // 1=週一…7=週日
    var nowMin = now.getHours() * 60 + now.getMinutes();
    var diff = (isoDow - weekday) * 1440 + (nowMin - (h * 60 + m));
    if (diff < 0) {
      diff += 7 * 1440; // 這個時段這週還沒到，那「最近一次」是上週
    }
    return diff;
  }

  // 在指定容器裡找「最近剛更新」的 .schedule-entry（minutesSinceSlot 最小的那筆）
  function findRecentEntry(scope) {
    var best = null;
    var bestDiff = Infinity;
    scope.querySelectorAll(".schedule-entry[data-weekday][data-time]").forEach(function (el) {
      var diff = minutesSinceSlot(parseInt(el.dataset.weekday, 10), el.dataset.time);
      if (diff !== null && diff < bestDiff) {
        bestDiff = diff;
        best = el;
      }
    });
    return best;
  }

  function initTogglePanel() {
    var panel = document.getElementById("schedule-panel");
    var toggle = document.getElementById("schedule-panel-toggle");
    var icon = document.getElementById("schedule-panel-toggle-icon");
    var scroller = document.getElementById("schedule-panel-scroll");
    var shell = document.getElementById("shell");
    if (!panel || !toggle || !icon || !scroller) {
      return;
    }

    var tripled = false; // 只複製一次
    var cycleHeight = 0;

    // 一輪內容已經比可視區高，才有必要做環狀捲動；否則維持單輪、不複製
    function maybeTripleCycle() {
      if (tripled) {
        return;
      }
      var original = scroller.querySelector(".schedule-cycle");
      if (!original || original.offsetHeight <= scroller.clientHeight) {
        return;
      }
      var before = original.cloneNode(true);
      var after = original.cloneNode(true);
      before.dataset.clone = "before";
      after.dataset.clone = "after";
      scroller.insertBefore(before, original);
      scroller.appendChild(after);
      tripled = true;
    }

    // scrollTop 維持落在「中間那一輪」的範圍內：滑出上半就 +一輪、滑出下半就 -一輪。
    // 三輪內容完全一樣、金框位置也一樣，平移的瞬間畫面看不出跳動。
    function keepCentered() {
      if (!tripled || !cycleHeight) {
        return;
      }
      var st = scroller.scrollTop;
      if (st < cycleHeight * 0.5) {
        scroller.scrollTop = st + cycleHeight;
      } else if (st > cycleHeight * 1.5) {
        scroller.scrollTop = st - cycleHeight;
      }
    }

    function clearHighlight() {
      scroller
        .querySelectorAll("." + GOLD_CLASS + ", ." + GOLD_CLASS + "-first, ." + GOLD_CLASS + "-last")
        .forEach(function (el) {
          el.classList.remove(GOLD_CLASS, GOLD_CLASS + "-first", GOLD_CLASS + "-last");
        });
    }

    function layoutOnExpand() {
      maybeTripleCycle();
      clearHighlight();

      var cycles = scroller.querySelectorAll(".schedule-cycle");
      var middle = tripled ? cycles[1] : cycles[0];
      if (!middle) {
        return; // 週期表無資料（開發中佔位）
      }
      cycleHeight = middle.offsetHeight;

      var midEntries = Array.prototype.slice.call(middle.querySelectorAll(".schedule-entry"));
      var recent = findRecentEntry(middle);
      if (!recent) {
        scroller.scrollTop = tripled ? cycleHeight : 0;
        return;
      }

      // 同一個時段可能有多部番劇一起更新——把「跟 recent 同星期＋同時間」的那幾筆
      // 全部框起來（使用者 2026-08-29 回饋），不是只框第一筆。用 DOM 索引對齊，環狀
      // 平移後每一輪的金框都停在同位置。
      var wd = recent.dataset.weekday;
      var tm = recent.dataset.time;
      var indices = [];
      midEntries.forEach(function (el, i) {
        if (el.dataset.weekday === wd && el.dataset.time === tm) {
          indices.push(i);
        }
      });
      cycles.forEach(function (cyc) {
        var entries = cyc.querySelectorAll(".schedule-entry");
        indices.forEach(function (i, pos) {
          if (!entries[i]) return;
          entries[i].classList.add(GOLD_CLASS);
          if (pos === 0) entries[i].classList.add(GOLD_CLASS + "-first");
          if (pos === indices.length - 1) entries[i].classList.add(GOLD_CLASS + "-last");
        });
      });

      // recent 捲到 scroller 頂端切齊——只留 2px 吸收環狀捲動正規化的整數誤差
      // （使用者 2026-09-01：金框上緣沒切齊，之前 16px 空隙讓上一組的「週X」標題
      // 露出來一截）。金框是 inset box-shadow、畫在框內，2px 也不會被裁掉。
      var RECENT_TOP_GAP = 2;
      var target =
        recent.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop - RECENT_TOP_GAP;
      if (tripled && cycleHeight) {
        while (target > cycleHeight * 1.5) {
          target -= cycleHeight;
        }
        while (target < cycleHeight * 0.5) {
          target += cycleHeight;
        }
      }
      scroller.scrollTop = target;
    }

    scroller.addEventListener("scroll", keepCentered);

    function setCollapsed(collapsed) {
      panel.classList.toggle("collapsed", collapsed);
      // 展開時 #shell 掛 .schedule-open：CSS 把切換鈕 position:fixed 到側板左緣，
      // 跟著側板「移位」、不被 fixed 側板蓋住（使用者 2026-08-26 回饋：要跟左側一樣
      // 一起移動，不要蓋住再在側板上另放收合鈕）
      if (shell) {
        shell.classList.toggle("schedule-open", !collapsed);
      }
      icon.src =
        "/static/icons/" + (collapsed ? "panel-expand" : "panel-collapse") + "-" + currentVariant() + ".png";
      if (!collapsed) {
        // display:none 時量不到高度，展開後才排版／定位
        layoutOnExpand();
      }
    }

    toggle.addEventListener("click", function (e) {
      e.stopPropagation();
      setCollapsed(!panel.classList.contains("collapsed"));
    });

    // 點側板／切換鈕以外的地方 → 收起（round 6 第 5 項，比照 notif.js 的下拉面板）
    document.addEventListener("click", function (e) {
      if (panel.classList.contains("collapsed")) {
        return;
      }
      if (panel.contains(e.target) || toggle.contains(e.target)) {
        return;
      }
      setCollapsed(true);
    });
  }

  document.addEventListener("DOMContentLoaded", initTogglePanel);
})();
