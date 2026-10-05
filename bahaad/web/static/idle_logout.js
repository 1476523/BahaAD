// 登入後閒置太久 → 自動登出（使用者 2026-09-08，為了安全）。
//
// 伺服器端（web/__init__.py `_apply_idle_logout`）才是真正的把關：閒置超過
// `session_idle_timeout_minutes`（預設 60）的下一個請求就清 session、導去登入頁。
// 這支只是「使用者沒在動時、時間到就自己重新載入」，讓畫面即時跳到登入頁，
// 而不是停在舊畫面、等使用者點下一步才發現被登出。
//
// 「有動作」＝滑鼠移動／按鍵／捲動／點擊／觸控，**或播放器開著**（不管在播放還是暫停
// ——使用者 2026-09-08：播放器開著就算動作，按 ✕ 關閉播放器才回到一般閒置計時）。
// 播放器開著時 episode_picker.js 每分鐘也會 ping 伺服器維持 session。
(function () {
  "use strict";

  var mins = parseFloat(document.documentElement.getAttribute("data-idle-timeout-min") || "0");
  if (!isFinite(mins) || mins <= 0) return;

  var timeoutMs = mins * 60 * 1000;
  var last = Date.now();
  function bump() { last = Date.now(); }

  ["mousemove", "mousedown", "keydown", "scroll", "touchstart", "click"].forEach(function (ev) {
    document.addEventListener(ev, bump, { passive: true });
  });

  function playerOpen() {
    // 播放器浮層開著就算「在用」——不管在播、暫停、還是投放到裝置上。
    return !!document.querySelector(
      "#episode-player-overlay:not([hidden]), .player-overlay:not([hidden])"
    );
  }

  window.setInterval(function () {
    if (playerOpen()) {
      bump();
      return;
    }
    if (Date.now() - last > timeoutMs) {
      // 伺服器這個請求就會判定閒置、清 session、導去登入頁
      window.location.reload();
    }
  }, 30000);
})();
