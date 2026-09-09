// 番劇詳細頁「製作／配音／音樂」右側浮動面板的收合切換（使用者 2026-09-01 回饋：
// 右欄改成像首頁週期表那樣的可收合右側面板）。比照 schedule_panel.js 的 setCollapsed
// 那一段，但沒有環狀捲動／金框那些，單純開合。
(function () {
  "use strict";

  function currentVariant() {
    return document.documentElement.dataset.theme === "light" ? "black" : "white";
  }

  document.addEventListener("DOMContentLoaded", function () {
    var panel = document.getElementById("ya-panel");
    var toggle = document.getElementById("ya-panel-toggle");
    var icon = document.getElementById("ya-panel-toggle-icon");
    var shell = document.getElementById("shell");
    if (!panel || !toggle || !icon) {
      return;
    }

    function setCollapsed(collapsed) {
      panel.classList.toggle("collapsed", collapsed);
      // 展開時給 #shell 掛 .ya-panel-open：CSS 把切換鈕 position:fixed 到面板左緣
      // （跟著一起移位、不被面板蓋住），也把 --overlay-right 撐開讓 modal/toast 仍
      // 置中在內容區——跟 schedule_panel.js / .schedule-open 同一套。
      if (shell) {
        shell.classList.toggle("ya-panel-open", !collapsed);
      }
      icon.src =
        "/static/icons/" + (collapsed ? "panel-expand" : "panel-collapse") + "-" + currentVariant() + ".png";
    }

    toggle.addEventListener("click", function (e) {
      e.stopPropagation();
      setCollapsed(!panel.classList.contains("collapsed"));
    });

    // 點面板／切換鈕以外的地方 → 收起（比照 schedule_panel.js / notif.js）
    document.addEventListener("click", function (e) {
      if (panel.classList.contains("collapsed")) {
        return;
      }
      if (panel.contains(e.target) || toggle.contains(e.target)) {
        return;
      }
      setCollapsed(true);
    });
  });
})();
