// 「隱私權政策」按鈕（側欄版本號右側 ＋ 首次設定頁）——按下後在目前頁面疊一層浮層顯示 docs/PRIVACY_POLICY.md
// 的即時內容（?embed=1 只回內容片段，每次按都重讀檔案、不快取）。點浮層任一處即關閉，
// Esc 也可以（使用者 2026-09-03：要疊在設定上、不是換頁）。
(function () {
  "use strict";

  function openOverlay(html) {
    var overlay = document.createElement("div");
    overlay.className = "doc-overlay";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");

    var card = document.createElement("div");
    // has-stats-head：讓「標題固定、只有內文捲動」的版面不必倚賴 :has()（舊版 WebKit）。
    card.className = "auth-card extra-wide-card markdown-doc doc-overlay-card has-stats-head";

    // 即時統計晶片：標題 + 一排數字，置頂固定在政策內文上方（使用者 2026-09-08）。
    // 標題旁的呼吸燈跟排行榜同一套設計（使用者 2026-09-16）：連線狀態／下次刷新
    // 秒數／本機還沒送出的資料筆數，共用 stats.js 的 mountSyncLight()。
    var stats = document.createElement("div");
    stats.className = "privacy-stats-head";
    stats.setAttribute("data-stats-summary", "");
    stats.innerHTML =
      '<h2>隱私權政策' +
      '<span class="player-net" id="privacy-stats-net" data-net="unknown" tabindex="0" role="button" aria-label="與伺服器同步狀態">' +
      '  <span class="player-net-dot"></span>' +
      '  <span class="player-net-tip" id="privacy-stats-net-tip" hidden></span>' +
      '</span>' +
      '</h2>' +
      '<div class="privacy-stats-chips">' +
      '  <span class="pv-chip"><span class="pv-chip-label">總下載次數</span>' +
      '    <span class="pv-chip-num" data-stats-field="downloads_total">—</span></span>' +
      '  <span class="pv-chip"><span class="pv-chip-label">BahaAD 總使用人數</span>' +
      '    <span class="pv-chip-num" data-stats-field="bahaad_users">—</span></span>' +
      '  <span class="pv-chip"><span class="pv-chip-label">公開模式 總使用人數</span>' +
      '    <span class="pv-chip-num" data-stats-field="public_users">—</span></span>' +
      '  <span class="pv-chip"><span class="pv-chip-label">BahaAD 總在線數</span>' +
      '    <span class="pv-chip-num" data-stats-field="bahaad_online">—</span></span>' +
      '  <span class="pv-chip"><span class="pv-chip-label">公開模式 總在線人數</span>' +
      '    <span class="pv-chip-num" data-stats-field="public_online">—</span></span>' +
      '</div>';

    var body = document.createElement("div");
    body.className = "doc-overlay-body";
    // 內容來自本站自家的 mini_markdown（已 html.escape），非使用者輸入。
    body.innerHTML = html;

    // 明確的關閉鈕——手機版浮層鋪滿整個畫面，「點任一處關閉」不夠直覺，而且
    // iOS Safari 對非互動元素不一定派送 click（使用者 2026-09-10）。
    var closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "doc-overlay-close";
    closeBtn.setAttribute("aria-label", "關閉");
    closeBtn.textContent = "✕";

    card.appendChild(closeBtn);
    card.appendChild(stats);
    card.appendChild(body);
    overlay.appendChild(card);

    // 晶片數字：開浮層當下抓一次，浮層開著時每 45 秒刷新（關掉就停）。
    var STATS_REFRESH_MS = 45000;
    var timer = null;
    var netLight = window.BahaStats && window.BahaStats.mountSyncLight
      ? window.BahaStats.mountSyncLight(
          stats.querySelector("#privacy-stats-net"),
          stats.querySelector("#privacy-stats-net-tip"),
          { refreshMs: STATS_REFRESH_MS }
        )
      : null;
    function loadStats() {
      fetch("/ui/stats/summary", { headers: { Accept: "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) {
          var nums = stats.querySelectorAll("[data-stats-field]");
          Array.prototype.forEach.call(nums, function (el) {
            var k = el.getAttribute("data-stats-field");
            var v = data && data[k];
            el.textContent =
              v === null || v === undefined || isNaN(v)
                ? "—"
                : Number(v).toLocaleString("en-US");
          });
          if (netLight) netLight.bumpRefresh();
        })
        .catch(function () {});
    }
    loadStats();
    timer = window.setInterval(loadStats, STATS_REFRESH_MS);

    function close() {
      if (timer) window.clearInterval(timer);
      if (netLight) netLight.destroy();
      overlay.remove();
      document.removeEventListener("keydown", onKey);
    }
    function onKey(e) {
      if (e.key === "Escape") {
        close();
      }
    }
    // 關閉鈕 ＋ 點背景（卡片以外的區域）關閉。使用者 2026-09-17 回報：隱私權政策
    // 各大段落改成收合／展開（<details>）之後，點標題展開會被這裡誤判成「點卡片
    // 本身」而把整個浮層關掉，完全打不開任何段落——原本「點卡片本身也關閉」的
    // 設計現在會跟卡片內任何互動元素（展開／連結／表格）衝突，改成只有點擊
    // backdrop（e.target === overlay，卡片以外）才關閉，卡片內部點擊一律不觸發。
    closeBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      close();
    });
    overlay.addEventListener("click", function (e) {
      if (e.target === overlay) close();
    });
    document.addEventListener("keydown", onKey);

    document.body.appendChild(overlay);
    card.scrollTop = 0;
  }

  function bind(btn) {
    btn.addEventListener("click", function () {
      var url = btn.getAttribute("data-privacy-url") || "/settings/privacy-policy";
      btn.disabled = true;
      fetch(url + (url.indexOf("?") === -1 ? "?" : "&") + "embed=1", {
        headers: { "X-Requested-With": "fetch" },
      })
        .then(function (resp) {
          if (!resp.ok) {
            throw new Error("HTTP " + resp.status);
          }
          return resp.text();
        })
        .then(function (html) {
          openOverlay(html);
        })
        .catch(function () {
          if (typeof showAlert === "function") {
            showAlert("讀取隱私權說明失敗，請稍後再試。");
          }
        })
        .finally(function () {
          btn.disabled = false;
        });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("privacy-policy-open");
    if (btn) {
      bind(btn);
    }
  });
})();
