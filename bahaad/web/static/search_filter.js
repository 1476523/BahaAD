// 搜尋番劇頁的右側篩選面板（屬性／類型／對象）。見 web_redesign_round3.md 階段 7-2。
//
// 面板是個 <form method="get">，**改動任一項就自動送出**（沒有「套用篩選」鈕，比照
// 動畫瘋 animeList）——送出就導到 /browse/search?tags=..&category=..&target=..，route
// 看到只有篩選、沒有關鍵字 → 走 animeList.php。這支 JS 做：
//   1. 切換鈕開/收面板（**不記憶狀態**——每次進搜尋番劇頁預設收起，除非正在看篩選結果）
//   2. 屬性：「全部」跟具體屬性互斥；具體屬性上限 max，到上限 disable 其餘；(n/5) 計數
//   3. 任一 input 改動 → 短 debounce 後自動送出（連點幾個屬性只刷一次）
//   4. 這次是帶著篩選結果來的（data-filter-active）→ 預設展開；否則收起
//
// 篩選只存在網址 query string，離開 /browse/search（側欄「搜尋番劇」連的是無 query 的
// 網址）就自然清掉——不需要另外「清除」動作。
(function () {
  "use strict";

  var toggle = document.getElementById("search-filter-toggle");
  var panel = document.getElementById("search-filter-panel");
  var form = document.getElementById("search-filter-form");
  var shell = document.getElementById("shell");
  if (!toggle || !panel || !form || !shell) {
    return;
  }

  function setOpen(open) {
    panel.classList.toggle("collapsed", !open);
    shell.classList.toggle("filter-open", open);
    var icon = document.getElementById("search-filter-toggle-icon");
    if (icon) {
      var variant = document.documentElement.dataset.theme === "light" ? "black" : "white";
      var name = open ? "panel-collapse" : "panel-expand";
      icon.dataset.icon = name;
      icon.src = "/static/icons/" + name + "-" + variant + ".png";
    }
  }

  toggle.addEventListener("click", function () {
    setOpen(panel.classList.contains("collapsed"));
  });

  var panelFlag = document.getElementById("search-filter-panel-flag");
  // 跟 style.css 的手機 @media 條件一致——只認觸控裝置，桌機拉窄不算（使用者 2026-09-10）
  var MOBILE_MQ = window.matchMedia("(max-width: 48rem) and (pointer: coarse)");

  // 改動任一項 → debounce 後自動送出。debounce 讓「連點 3 個屬性」只導頁一次。
  var submitTimer = null;
  function scheduleSubmit() {
    if (submitTimer) {
      clearTimeout(submitTimer);
    }
    submitTimer = setTimeout(function () {
      // 手機：面板是全螢幕蓋在結果上的——送出前先收起，重載後直接看到篩選結果，
      // 不用手動關（使用者 2026-09-10）。要再調整篩選就再點一次切換鈕。
      if (MOBILE_MQ.matches) {
        setOpen(false);
      }
      // 帶上面板目前的開合狀態，讓它跨整頁重載維持（round5 項目 7）
      if (panelFlag) {
        panelFlag.value = panel.classList.contains("collapsed") ? "" : "1";
      }
      if (window.showNavLoading) { window.showNavLoading(); }
      form.submit();
    }, 350);
  }

  // 關鍵字搜尋列送出也要即時載入回饋
  var searchBar = document.querySelector("form.search-bar");
  if (searchBar) {
    searchBar.addEventListener("submit", function () {
      if (window.showNavLoading) { window.showNavLoading(); }
    });
  }

  // 屬性 chip：「全部」跟具體屬性互斥；具體屬性上限 max。
  var max = parseInt(panel.dataset.maxTags, 10) || 5;
  var all = panel.querySelector('input[name="tags"][value="全部"]');
  var tagInputs = Array.prototype.slice
    .call(panel.querySelectorAll('input[name="tags"]'))
    .filter(function (c) { return c !== all; });
  var counter = document.getElementById("filter-tag-count");

  function refreshTags() {
    var checked = tagInputs.filter(function (c) { return c.checked; }).length;
    if (counter) {
      counter.textContent = "（" + checked + "/" + max + "）";
    }
    tagInputs.forEach(function (c) {
      c.disabled = !c.checked && checked >= max;
    });
  }

  // 屬性是主篩選：屬性完全沒選（沒按具體屬性、也沒按「全部」）時，類型／對象正常來說
  // 不該起作用——之前是 bug，沒選屬性單靠類型／對象也會送出篩選（使用者 2026-09-06）。
  // 屬性沒選時把類型／對象 disable 並重置回「全部」；屬性一有選擇（含按屬性的「全部」）
  // 就解除 disable。
  var catTargetRadios = Array.prototype.slice.call(
    panel.querySelectorAll('input[name="category"], input[name="target"]')
  );

  function hasAttrSelection() {
    return (all && all.checked) || tagInputs.some(function (c) { return c.checked; });
  }

  function refreshCatTargetGating() {
    var enabled = hasAttrSelection();
    catTargetRadios.forEach(function (r) {
      if (!enabled && r.value !== "" && r.checked) {
        r.checked = false;
        // 對應那組的「全部」radio（同 name、value=""）重新選上，維持一致的視覺狀態。
        var fallback = panel.querySelector('input[name="' + r.name + '"][value=""]');
        if (fallback) { fallback.checked = true; }
      }
      r.disabled = !enabled && r.value !== "";
    });
  }

  if (all) {
    all.addEventListener("change", function () {
      if (all.checked) {
        tagInputs.forEach(function (c) { c.checked = false; }); // 選「全部」→ 清掉具體屬性
      }
      refreshTags();
      refreshCatTargetGating();
      scheduleSubmit();
    });
  }
  tagInputs.forEach(function (c) {
    c.addEventListener("change", function () {
      if (c.checked && all) { all.checked = false; } // 選具體屬性 → 取消「全部」
      refreshTags();
      refreshCatTargetGating();
      scheduleSubmit();
    });
  });
  catTargetRadios.forEach(function (r) {
    r.addEventListener("change", scheduleSubmit);
  });
  refreshTags();
  refreshCatTargetGating();

  // 這次在看篩選結果 → 桌面預設展開（方便繼續微調）；手機不自動展開——面板全螢幕會
  // 蓋住剛篩出來的結果，讓使用者先看結果，要調整再點切換鈕（使用者 2026-09-10）。
  if (panel.dataset.filterActive === "true" && !MOBILE_MQ.matches) {
    setOpen(true);
  }
})();
