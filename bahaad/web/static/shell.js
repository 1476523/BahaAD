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
      if (name === "sidebar-collapse" || name === "panel-expand" || name === "notif" || name === "layout-mode") {
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

  // 排版模式（使用者 2026-09-21）：手機／電視／電腦手動覆寫，或退回自動判斷
  // （(pointer: coarse) 觸控裝置＋窄螢幕≤48rem）——智慧電視等裝置的遙控器操作／
  // 回報的邏輯寬度，可能讓自動判斷誤判成手機，讓使用者自己選「電視」/「電腦」
  // 蓋掉。`data-layout-active`（真正驅動 style.css 版面切換的那個屬性）是這裡
  // 算出的最終結果；base.html 開頭那段阻塞式 script 用同一套邏輯先算一次、避免
  // 畫面先畫錯排版再跳一下。改變時發 `bahaad-layout-change` 事件，
  // `initSidebarToggle()` 的手機抽屜同步邏輯訂閱這個事件，不再直接聽
  // `MOBILE_MQ` 自己的 change（這樣手動覆寫也能觸發同一套同步，不用另外複製
  // 一份）。
  var MOBILE_MQ = window.matchMedia("(max-width: 48rem) and (pointer: coarse)");
  var LAYOUT_MODES = ["mobile", "tv", "desktop"];
  var LAYOUT_ICON_FOR = { mobile: "mobile-mode", tv: "tv-mode", desktop: "desktop-mode" };

  function currentLayoutMode() {
    return document.documentElement.dataset.layoutMode || "desktop";
  }

  function computeActiveLayout(mode) {
    if (mode === "mobile") { return "mobile"; }
    if (mode === "tv" || mode === "desktop") { return "desktop"; }
    return MOBILE_MQ.matches ? "mobile" : "desktop";
  }

  function applyActiveLayout() {
    var active = computeActiveLayout(currentLayoutMode());
    document.documentElement.dataset.layoutActive = active;
    window.dispatchEvent(new Event("bahaad-layout-change"));
  }

  if (MOBILE_MQ.addEventListener) { MOBILE_MQ.addEventListener("change", applyActiveLayout); }
  else if (MOBILE_MQ.addListener) { MOBILE_MQ.addListener(applyActiveLayout); }

  window.BahaADLayout = {
    isMobile: function () { return document.documentElement.dataset.layoutActive === "mobile"; },
  };

  function updateLayoutModeIcon(variant) {
    var img = document.getElementById("layout-mode-icon");
    if (!img) { return; }
    var iconName = LAYOUT_ICON_FOR[currentLayoutMode()] || "desktop-mode";
    img.src = iconPathFor(iconName, variant);
  }

  // 電視遙控器「上」鍵在有置頂標題的清單頁（style.css `.pinned-scroll`／
  // `.home-circular`——首頁本季新番、近期熱播、最新上架、搜尋番劇、訂閱列表、
  // 下載列表、排行榜、日誌等頁）按不動（使用者 2026-09-21：可以一直往下捲，
  // 往上完全沒反應；沒鎖住捲動的番劇詳細頁不受影響）。這些頁面實際捲動的是
  // 卡片格外層那個 `overflow-y:auto` 的巢狀 div，不是整個文件；往下捲每次都會
  // 移到下一張卡片、原生「把被選中的元素捲進可視範圍」機制順便把捲動容器帶著走，
  // 但捲到第一列卡片後再往上，上面只有不會聚焦的固定標題文字、原生機制找不到
  // 「上一個」目標，焦點卡住不動、捲動也跟著卡住。這裡不依賴任何焦點/方向鍵
  // 導覽語意，直接對捲動容器下 `scrollBy()`，保證按得動；往下原本就正常，
  // 不去動它。
  var TV_SCROLL_SELECTORS = "#newanime-scroll, #trending-scroll, #latest-scroll, " +
    "#search-scroll, #subs-scroll, #leaderboard-scroll, #downloads-scroll, " +
    "#newanime-list-scroll, .paged-card__body";
  var TV_SCROLL_STEP = 220;

  function findTvScrollTarget() {
    var nodes = document.querySelectorAll(TV_SCROLL_SELECTORS);
    for (var i = 0; i < nodes.length; i++) {
      if (nodes[i].offsetParent !== null) { return nodes[i]; }
    }
    return null;
  }

  function initTvScrollFix() {
    document.addEventListener("keydown", function (e) {
      if (e.key !== "ArrowUp" || currentLayoutMode() !== "tv") { return; }
      var target = e.target;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT")) {
        return;
      }
      var scrollEl = findTvScrollTarget();
      if (!scrollEl || scrollEl.scrollTop <= 0) { return; }
      scrollEl.scrollBy({ top: -TV_SCROLL_STEP, behavior: "smooth" });
      e.preventDefault();
    });
  }

  function initLayoutModeToggle() {
    var btn = document.getElementById("layout-mode-toggle");
    if (!btn) { return; }
    btn.addEventListener("click", function () {
      // 目前是 "auto"（從沒手動選過）時從頭開始循環；已經手動選過就依序切下一個。
      var idx = LAYOUT_MODES.indexOf(currentLayoutMode());
      var next = LAYOUT_MODES[(idx + 1) % LAYOUT_MODES.length];
      document.documentElement.dataset.layoutMode = next;
      applyActiveLayout();
      updateLayoutModeIcon(currentVariant());

      // 切成電視模式時，側欄如果剛好停在（桌面模式存下的）收合態，直接展開
      // 回來——電視一律不收合（使用者 2026-09-21），不用等下次重新整理才校正。
      if (next === "tv") {
        var shellEl = document.getElementById("shell");
        if (shellEl && shellEl.classList.contains("sidebar-collapsed")) {
          shellEl.classList.remove("sidebar-collapsed");
          updateSidebarToggleIcon(false, currentVariant());
        }
      }

      if (PUBLIC_RO) {
        setGuestCookie("bahaad_pub_layout_mode", next);
        return;  // 未登入不打 /ui/layout-mode（會被導去登入頁）
      }
      fetch("/ui/layout-mode", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: "value=" + encodeURIComponent(next),
      }).catch(function () {});
    });
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
      updateLayoutModeIcon(variant);

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

  // 手機／小平板（觸控裝置 + 窄畫面，或手動覆寫成「手機」）：同一顆鈕改成開關
  // 「滑出式抽屜」（.sidebar-drawer-open），不寫進偏好（抽屜是一次性的）。桌面
  // （含把視窗拉窄，或手動覆寫成「電視」/「電腦」）維持原本的「收合 72px」＋
  // 存偏好行為。是不是手機一律問 `window.BahaADLayout.isMobile()`（見上面
  // `applyActiveLayout()` 開頭說明），不是手動覆寫時就是這裡的媒體查詢結果，
  // 手動覆寫時直接照使用者選的算，不看實際量測（使用者 2026-09-21）。

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
      if (window.BahaADLayout.isMobile()) {
        e.stopPropagation(); // 別讓這一下被下面的「點外面關抽屜」接到
        shell.classList.toggle("sidebar-drawer-open");
        updateDrawerIcon();
        return;
      }
      // 電視模式側欄一律不收合（使用者 2026-09-21：遙控器操作，72px 窄條版對
      // 電視沒意義）——按這顆鈕不做事，跟 style.css `[data-layout-mode="tv"]`
      // 那段把收合寬度強制蓋回展開寬度的視覺保底是同一個道理，這裡從根本不讓
      // 使用者按得出收合狀態。
      if (currentLayoutMode() === "tv") {
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
    // 轉回桌面排版時把抽屜狀態清掉、圖示回到桌面收合態；轉進手機排版時同步成抽屜態
    // ——不管是視窗尺寸真的改變，還是使用者按了排版模式切換鈕，都會發這個事件
    // （見上面 applyActiveLayout()）。
    window.addEventListener("bahaad-layout-change", function () {
      if (!window.BahaADLayout.isMobile()) {
        shell.classList.remove("sidebar-drawer-open");
        updateSidebarToggleIcon(shell.classList.contains("sidebar-collapsed"), currentVariant());
      } else {
        updateDrawerIcon();
      }
    });

    // 進頁時如果已經是手機排版，把圖示從 server-render 的「桌面收合偏好」校正成抽屜態
    if (window.BahaADLayout.isMobile()) { updateDrawerIcon(); }
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
    initLayoutModeToggle();
    initTvScrollFix();
    initNavLoading();
  });
})();
