// 「發送驗證碼」按鈕：POST /verify-code/send，成功後進入冷卻倒數（30 秒，跟後端一致）；
// 失敗（冷卻中／鎖定中／這個環境沒有系統匣）顯示訊息。驗證碼本身**只送系統匣通知**，
// 這支永遠拿不到明碼——首次設定驗證頁、忘記密碼頁共用（使用者 2026-09-06）。
(function () {
  "use strict";

  var COOLDOWN_SECONDS = 30;

  function initSendCodeButtons() {
    document.querySelectorAll("[data-send-code-purpose]").forEach(function (btn) {
      var original = btn.textContent;
      btn.addEventListener("click", function () {
        var purpose = btn.dataset.sendCodePurpose;
        btn.disabled = true;
        fetch("/verify-code/send", {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: "purpose=" + encodeURIComponent(purpose),
        })
          .then(function (r) {
            return r.json().then(function (data) { return { ok: r.ok, data: data }; });
          })
          .then(function (res) {
            if (window.showToast) {
              window.showToast((res.data && res.data.message) || (res.ok ? "已發送" : "發送失敗"));
            }
            if (!res.ok) {
              btn.disabled = false;
              return;
            }
            var seconds = COOLDOWN_SECONDS;
            btn.textContent = "請稍候（" + seconds + "）";
            var timer = window.setInterval(function () {
              seconds -= 1;
              if (seconds <= 0) {
                window.clearInterval(timer);
                btn.textContent = original;
                btn.disabled = false;
                return;
              }
              btn.textContent = "請稍候（" + seconds + "）";
            }, 1000);
          })
          .catch(function () {
            if (window.showAlert) window.showAlert("發送失敗，請稍後再試");
            btn.disabled = false;
          });
      });
    });
  }

  document.addEventListener("DOMContentLoaded", initSendCodeButtons);
})();
