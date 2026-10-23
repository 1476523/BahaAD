// 訂閱列表頁的「排程清單」右側浮動面板（round 7 第 4 項）——比照首頁週期表側板：
// 切換鈕在 .content-top、展開時給 #shell 掛 .subs-schedule-open、點面板外收起。
// 內容是 schedule_embed.html 的 iframe（排程清單有一堆編輯/刪除表單，隔離比較乾淨）。
(function () {
  "use strict";

  function currentVariant() {
    return document.documentElement.dataset.theme === "light" ? "black" : "white";
  }

  document.addEventListener("DOMContentLoaded", function () {
    var panel = document.getElementById("subs-schedule-panel");
    var toggle = document.getElementById("subs-schedule-toggle");
    var icon = document.getElementById("subs-schedule-toggle-icon");
    var frame = document.getElementById("subs-schedule-frame");
    var shell = document.getElementById("shell");
    if (!panel || !toggle || !icon || !frame) {
      return;
    }
    var loaded = false;

    function setCollapsed(collapsed) {
      panel.classList.toggle("collapsed", collapsed);
      if (shell) {
        shell.classList.toggle("subs-schedule-open", !collapsed);
      }
      icon.src =
        "/static/icons/" + (collapsed ? "panel-expand" : "panel-collapse") + "-" + currentVariant() + ".png";
      if (!collapsed && !loaded) {
        frame.src = frame.dataset.src; // 第一次展開才載入
        loaded = true;
      }
    }

    toggle.addEventListener("click", function (e) {
      e.stopPropagation();
      setCollapsed(!panel.classList.contains("collapsed"));
    });

    document.addEventListener("click", function (e) {
      if (panel.classList.contains("collapsed")) return;
      if (panel.contains(e.target) || toggle.contains(e.target)) return;
      setCollapsed(true);
    });
  });
})();
