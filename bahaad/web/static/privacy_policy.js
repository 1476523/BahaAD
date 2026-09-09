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
    card.className = "auth-card extra-wide-card markdown-doc doc-overlay-card";

    // 即時統計晶片：標題 + 一排數字，置頂固定在政策內文上方（使用者 2026-09-08）。
    var stats = document.createElement("div");
    stats.className = "privacy-stats-head";
    stats.setAttribute("data-stats-summary", "");
    stats.innerHTML =
      '<h2>隱私權政策</h2>' +
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

    card.appendChild(stats);
    card.appendChild(body);
    overlay.appendChild(card);

    // 晶片數字：開浮層當下抓一次，浮層開著時每 45 秒刷新（關掉就停）。
    var timer = null;
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
        })
        .catch(function () {});
    }
    loadStats();
    timer = window.setInterval(loadStats, 45000);

    function close() {
      if (timer) window.clearInterval(timer);
      overlay.remove();
      document.removeEventListener("keydown", onKey);
    }
    function onKey(e) {
      if (e.key === "Escape") {
        close();
      }
    }
    // 點視窗任一處（含內容卡片本身）都關閉
    overlay.addEventListener("click", close);
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
