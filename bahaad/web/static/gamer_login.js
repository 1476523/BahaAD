// 動畫瘋登入 / 環境偽裝指紋採集的進度頁：輪詢 /gamer-login/status，done 就導到 next，
// error 就把訊息秀出來、給「繼續 / 重新輸入帳密」按鈕。
(function () {
  var root = document.getElementById("gamer-login-progress");
  if (!root) {
    return;
  }
  var nextUrl = root.dataset.nextUrl || "/";
  var messageEl = document.getElementById("gamer-login-message");
  var hintEl = document.getElementById("gamer-login-hint");
  var actionsEl = document.getElementById("gamer-login-actions");
  var titleEl = root.querySelector("h1");

  function poll() {
    fetch("/gamer-login/status", { headers: { "Accept": "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.state === "done") {
          window.location = nextUrl;
          return;
        }
        if (data.state === "error") {
          titleEl.textContent = "沒有完成";
          messageEl.textContent = data.message || "登入沒有完成，請再試一次。";
          if (hintEl) { hintEl.hidden = true; }
          if (actionsEl) { actionsEl.hidden = false; }
          return;
        }
        if (data.message) {
          messageEl.textContent = data.message;
        }
        setTimeout(poll, 1500);
      })
      .catch(function () {
        setTimeout(poll, 3000);
      });
  }

  poll();
})();
