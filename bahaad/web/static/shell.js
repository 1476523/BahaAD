// 側欄殼層：主題切換／側欄收合，兩者都是純前端立即生效＋背景送一次 fire-and-forget
// 的 POST 存進 SettingsStore，讓下次開啟維持上次的選擇。見
// docs/requirements/web_redesign.md「整體殼層」。
(function () {
  "use strict";

  function iconPathFor(iconName, variant) {
    return "/static/icons/" + iconName + "-" + variant + ".png";
  }

  function applyIconVariant(variant) {
    document.querySelectorAll("img.theme-icon[data-icon]").forEach(function (img) {
      var name = img.dataset.icon;
      // 收合／展開切換鈕的圖示要看目前是收合還是展開態，不是固定用同一張圖，另外處理：
      // 側欄收合鈕（sidebar-collapse）跟首頁週期表切換鈕（panel-expand）都是這種。
      // 訂閱通知鈕（notif）的黑白切換由 notif.js 自己管（有通知時是動圖、不 tint）。
      if (name === "sidebar-collapse" || name === "panel-expand" || name === "notif") {
        return;
      }
      img.src = iconPathFor(name, variant);
    });
  }

  function updateSidebarToggleIcon(collapsed, variant) {
    var img = document.getElementById("sidebar-toggle-icon");
    if (!img) {
      return;
    }
    img.src = iconPathFor(collapsed ? "sidebar-expand" : "sidebar-collapse", variant);
  }

  // 首頁週期表切換鈕：切主題時依側板目前收合／展開態換對應圖示的黑白版，
  // 比照 updateSidebarToggleIcon。首頁以外的頁面沒有這顆鈕，靜默跳過。
  function updateSchedulePanelToggleIcon(variant) {
    var img = document.getElementById("schedule-panel-toggle-icon");
    var panel = document.getElementById("schedule-panel");
    if (!img || !panel) {
      return;
    }
    var collapsed = panel.classList.contains("collapsed");
    img.src = iconPathFor(collapsed ? "panel-expand" : "panel-collapse", variant);
  }

  function currentVariant() {
    return document.documentElement.dataset.theme === "light" ? "black" : "white";
  }

  // 公開模式（未登入唯讀）：主題／側欄收合都不能寫進擁有者的 SettingsStore，改存訪客
  // 自己的 **cookie**——伺服器（web/__init__.py `_inject_ui_shell_prefs`）讀 cookie
  // server-render，換頁不閃（使用者 2026-09-06：localStorage 版換頁會先展開再收起）。
  // 右側浮動面板（排程清單／製作資訊）不記憶——每頁都從收起開始。
  var PUBLIC_RO = document.documentElement.dataset.publicReadonly === "true";

  function setGuestCookie(name, value) {
    try {
      document.cookie =
        name + "=" + value + "; path=/; max-age=31536000; samesite=lax";
    } catch (e) { /* 私密瀏覽等 */ }
  }

  function initThemeToggle() {
    var btn = document.getElementById("theme-toggle");
    if (!btn) {
      return;
    }
    btn.addEventListener("click", function () {
      var next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      var variant = currentVariant();
      applyIconVariant(variant);
      var collapsed = document.getElementById("shell").classList.contains("sidebar-collapsed");
      updateSidebarToggleIcon(collapsed, variant);
      updateSchedulePanelToggleIcon(variant);

      if (PUBLIC_RO) {
        setGuestCookie("bahaad_pub_theme", next);
        return;  // 未登入不打 /ui/theme（會被導去登入頁）
      }

      fetch("/ui/theme", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: "value=" + encodeURIComponent(next),
      }).catch(function () {
        // 存偏好失敗不影響這次畫面已經切換好的主題，下次重新整理才會退回舊值，
        // 不需要跳錯誤訊息打斷使用者
      });
    });
  }

  // 手機／小平板（觸控裝置 + 窄畫面）：同一顆鈕改成開關「滑出式抽屜」
  // （.sidebar-drawer-open），不寫進偏好（抽屜是一次性的）。桌面（含把視窗拉窄）維持
  // 原本的「收合 72px」＋存偏好行為。條件要跟 style.css 的手機 @media 一致
  // （使用者 2026-09-10：不要跟非手機窄邊顯示搞混）。
  var MOBILE_MQ = window.matchMedia("(max-width: 48rem) and (pointer: coarse)");

  function initSidebarToggle() {
    var btn = document.getElementById("sidebar-toggle");
    var shell = document.getElementById("shell");
    var sidebar = document.getElementById("sidebar");
    if (!btn || !shell) {
      return;
    }

    // 手機抽屜的圖示要看「抽屜開沒開」，不是看桌面存下的「收合偏好」——不然抽屜關著時
    // 卻顯示 sidebar-collapse（← 收合箭頭）看起來像放反了（使用者 2026-09-10）。
    function updateDrawerIcon() {
      var img = document.getElementById("sidebar-toggle-icon");
      if (!img) { return; }
      var open = shell.classList.contains("sidebar-drawer-open");
      img.src = iconPathFor(open ? "sidebar-collapse" : "sidebar-expand", currentVariant());
    }

    function closeDrawer() {
      shell.classList.remove("sidebar-drawer-open");
      updateDrawerIcon();
    }

    btn.addEventListener("click", function (e) {
      if (MOBILE_MQ.matches) {
        e.stopPropagation(); // 別讓這一下被下面的「點外面關抽屜」接到
        shell.classList.toggle("sidebar-drawer-open");
        updateDrawerIcon();
        return;
      }
      var collapsed = !shell.classList.contains("sidebar-collapsed");
      shell.classList.toggle("sidebar-collapsed", collapsed);
      updateSidebarToggleIcon(collapsed, currentVariant());

      if (PUBLIC_RO) {
        setGuestCookie("bahaad_pub_sidebar", collapsed ? "1" : "0");
        return;  // 未登入不打 /ui/sidebar（會被導去登入頁）
      }
      fetch("/ui/sidebar", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: "value=" + (collapsed ? "true" : "false"),
      }).catch(function () {});
    });

    // 點選單項目、點背景遮罩、按 Esc → 關抽屜
    if (sidebar) {
      sidebar.addEventListener("click", function (e) {
        if (e.target.closest("a[href]")) { closeDrawer(); }
      });
    }
    shell.addEventListener("click", function (e) {
      if (!shell.classList.contains("sidebar-drawer-open")) { return; }
      if (e.target.closest("#sidebar") || e.target.closest(".content-top-left")) { return; }
      closeDrawer();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { closeDrawer(); }
    });
    // 轉回桌面尺寸時把抽屜狀態清掉、圖示回到桌面收合態；轉進手機尺寸時同步成抽屜態
    var onMqChange = function () {
      if (!MOBILE_MQ.matches) {
        shell.classList.remove("sidebar-drawer-open");
        updateSidebarToggleIcon(shell.classList.contains("sidebar-collapsed"), currentVariant());
      } else {
        updateDrawerIcon();
      }
    };
    if (MOBILE_MQ.addEventListener) { MOBILE_MQ.addEventListener("change", onMqChange); }
    else if (MOBILE_MQ.addListener) { MOBILE_MQ.addListener(onMqChange); }

    // 進頁時如果已經是手機尺寸，把圖示從 server-render 的「桌面收合偏好」校正成抽屜態
    if (MOBILE_MQ.matches) { updateDrawerIcon(); }
  }

  // 導覽整頁重載期間的即時回饋：點側欄項目到新頁首次 paint 之間畫面完全沒變化，
  // 使用者以為沒點到就重複點擊。點下去立刻蓋一層置中載入動畫，之後瀏覽器照常導覽
  // （整頁重載會丟棄整個 DOM，載入層不用自己拆）。見 web_redesign_round4.md。
  function showNavLoading() {
    if (document.getElementById("nav-loading-overlay")) {
      return;
    }
    var overlay = document.createElement("div");
    overlay.id = "nav-loading-overlay";
    overlay.className = "nav-loading-overlay";
    var img = document.createElement("img");
    img.src = "/static/icons/loading.gif";
    img.alt = "載入中";
    img.width = 48;
    img.height = 48;
    var span = document.createElement("span");
    span.textContent = "載入中…";
    overlay.appendChild(img);
    overlay.appendChild(span);
    document.body.appendChild(overlay);
  }

  function hideNavLoading() {
    var overlay = document.getElementById("nav-loading-overlay");
    if (overlay) {
      overlay.remove();
    }
  }

  // 其他頁面的 form.submit()（搜尋列、篩選面板自動送出）也要即時回饋——見
  // web_redesign_round5.md 項目 7。
  window.showNavLoading = showNavLoading;
  window.hideNavLoading = hideNavLoading;

  function initNavLoading() {
    var sidebar = document.getElementById("sidebar");
    if (!sidebar) {
      return;
    }
    sidebar.addEventListener("click", function (e) {
      if (e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) {
        return;
      }
      var link = e.target.closest("a[href]");
      if (!link || link.target === "_blank" || link.hasAttribute("download")) {
        return;
      }
      // link.href 是解析過的絕對網址。只處理站內、且不是目前這一頁的導覽。
      if (link.origin !== location.origin || link.href === location.href) {
        return;
      }
      showNavLoading();
    });
  }

  // 返回上一頁時 bfcache 會把「離開前顯示了載入層」的頁面原封不動還原，殘留的載入層
  // 要清掉（pageshow 的 persisted 為 true 就是從 bfcache 還原）。
  window.addEventListener("pageshow", hideNavLoading);

  document.addEventListener("DOMContentLoaded", function () {
    initThemeToggle();
    initSidebarToggle();
    initNavLoading();
  });
})();
