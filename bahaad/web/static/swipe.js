// 手機排版：左右滑動螢幕開關側邊（使用者 2026-10-23）。
//   右滑：先收起已開啟的右側面板（週期表／製作資訊）；沒有的話開啟左側選單
//   左滑：先收起已開啟的左側選單；沒有的話開啟右側面板（該頁有的話）
// 只是替使用者按下既有的切換鈕（#sidebar-toggle、各頁的面板鈕），開合邏輯、圖示、偏好都沿用原本那套。
// 只在手機排版（BahaADLayout.isMobile()）啟用；從橫向可捲動的區域（番劇列、表格、輸入框、影片）開始的
// 滑動不處理，免得跟那些元件自己的橫向捲動打架。
(function () {
  "use strict";

  var MIN_DX = 60;         // 至少橫向滑這麼遠（px）
  var MAX_MS = 700;        // 太慢的拖曳不算滑動
  var DOMINANCE = 1.5;     // 橫向距離要是縱向的這麼多倍，才不會誤把捲頁當滑動
  var RIGHT_PANELS = [
    ["schedule-panel", "schedule-panel-toggle"],
    ["ya-panel", "ya-panel-toggle"],
    ["subs-schedule-panel", "subs-schedule-toggle"],
  ];
  var IGNORE = "input, textarea, select, video, [contenteditable], [role=dialog], .modal, .hscroll, .no-swipe";

  var startX = 0, startY = 0, startT = 0, tracking = false;

  function isMobile() {
    return !!(window.BahaADLayout && window.BahaADLayout.isMobile());
  }

  // 從 el 往上找到 #shell，途中只要有元素橫向可捲動就視為「它自己在處理橫向手勢」
  function inHorizontalScroller(el) {
    for (var n = el; n && n.nodeType === 1 && n.id !== "shell" && n !== document.body; n = n.parentElement) {
      if (n.scrollWidth > n.clientWidth + 1) {
        var ox = getComputedStyle(n).overflowX;
        if (ox === "auto" || ox === "scroll") { return true; }
      }
    }
    return false;
  }

  function openRightPanel() {
    for (var i = 0; i < RIGHT_PANELS.length; i++) {
      var p = document.getElementById(RIGHT_PANELS[i][0]);
      if (p && p.classList.contains("collapsed")) { return RIGHT_PANELS[i]; }
    }
    return null;
  }

  function openedRightPanel() {
    for (var i = 0; i < RIGHT_PANELS.length; i++) {
      var p = document.getElementById(RIGHT_PANELS[i][0]);
      if (p && !p.classList.contains("collapsed")) { return RIGHT_PANELS[i]; }
    }
    return null;
  }

  function press(id) {
    var btn = document.getElementById(id);
    if (btn) { btn.click(); }
  }

  function handle(dx) {
    var shell = document.getElementById("shell");
    if (!shell) { return; }
    var leftOpen = shell.classList.contains("sidebar-drawer-open");
    if (dx > 0) {                       // 右滑
      var opened = openedRightPanel();
      if (opened) { press(opened[1]); }
      else if (!leftOpen) { press("sidebar-toggle"); }
    } else {                            // 左滑
      if (leftOpen) { press("sidebar-toggle"); }
      else {
        var closed = openRightPanel();
        if (closed) { press(closed[1]); }
      }
    }
  }

  document.addEventListener("touchstart", function (e) {
    tracking = false;
    if (e.touches.length !== 1 || !isMobile()) { return; }
    var t = e.target;
    if (t && t.closest && (t.closest(IGNORE) || inHorizontalScroller(t))) { return; }
    startX = e.touches[0].clientX;
    startY = e.touches[0].clientY;
    startT = Date.now();
    tracking = true;
  }, { passive: true });

  document.addEventListener("touchend", function (e) {
    if (!tracking) { return; }
    tracking = false;
    var t = e.changedTouches[0];
    var dx = t.clientX - startX, dy = t.clientY - startY;
    if (Date.now() - startT > MAX_MS) { return; }
    if (Math.abs(dx) < MIN_DX || Math.abs(dx) < Math.abs(dy) * DOMINANCE) { return; }
    handle(dx);
  }, { passive: true });

  document.addEventListener("touchcancel", function () { tracking = false; }, { passive: true });
})();
