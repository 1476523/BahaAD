// 「我的收藏」帳號連結區塊：發送測試訊息 + 驗證碼確認（使用者 2026-09-23）——
// 送出測試訊息會帶一組隨機驗證碼，要把 Telegram／Discord 收到的碼貼回這裡核對
// 才算真的確認收到（不是單看中繼 API 回應「送出成功」）。核對成功才整頁重新
// 整理（使用者 2026-09-23：「輸入正確才刷新並隱藏提示」），錯誤留在原地讓使用者
// 重試，不打斷其他操作。
(function () {
  "use strict";

  function verifyRowFor(channel) {
    return document.querySelector('.subscriber-verify-row[data-channel="' + channel + '"]');
  }

  document.querySelectorAll(".test-message-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      if (btn.disabled) return;
      btn.disabled = true;
      fetch("/subscriber/notify-test", {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          "X-Requested-With": "fetch",
        },
        body: "channel=" + encodeURIComponent(btn.dataset.channel),
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (window.showAlert) { window.showAlert(data.info); }
          if (data.ok) {
            var row = verifyRowFor(btn.dataset.channel);
            if (row) {
              row.hidden = false;
              var input = row.querySelector(".subscriber-verify-input");
              if (input) { input.value = ""; input.focus(); }
            }
          }
        })
        .catch(function () {
          if (window.showAlert) { window.showAlert("測試訊息發送失敗，請稍後再試"); }
        })
        .finally(function () {
          btn.disabled = false;
        });
    });
  });

  function confirmCode(channel, btn) {
    var row = verifyRowFor(channel);
    var input = row ? row.querySelector(".subscriber-verify-input") : null;
    var code = input ? input.value.trim() : "";
    if (!code) {
      if (window.showAlert) { window.showAlert("請輸入收到的驗證碼"); }
      return;
    }
    if (btn.disabled) return;
    btn.disabled = true;
    fetch("/subscriber/notify-test/confirm", {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Requested-With": "fetch",
      },
      body: "channel=" + encodeURIComponent(channel) + "&code=" + encodeURIComponent(code),
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.ok) {
          // 使用者 2026-09-23：輸入正確才刷新並隱藏提示——交給整頁重新整理，
          // 「尚未驗證」徽章／提醒都是伺服器端算的，不用自己在前端同步一堆狀態。
          window.location.reload();
          return;
        }
        if (window.showAlert) { window.showAlert(data.info); }
        btn.disabled = false;
      })
      .catch(function () {
        if (window.showAlert) { window.showAlert("驗證失敗，請稍後再試"); }
        btn.disabled = false;
      });
  }

  document.querySelectorAll(".verify-code-confirm-btn").forEach(function (btn) {
    btn.addEventListener("click", function () { confirmCode(btn.dataset.channel, btn); });
  });

  document.querySelectorAll(".subscriber-verify-input").forEach(function (input) {
    input.addEventListener("keydown", function (e) {
      if (e.key !== "Enter") return;
      e.preventDefault();
      var row = input.closest(".subscriber-verify-row");
      var btn = row ? row.querySelector(".verify-code-confirm-btn") : null;
      if (btn) { confirmCode(btn.dataset.channel, btn); }
    });
  });
})();
