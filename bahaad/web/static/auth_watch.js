// 帳號／密碼在某個分頁被更改後（settings.change_account），session 金鑰輪替、所有
// 既有 session 失效。做更改的那個分頁由 ajax_forms.js 自己導向 /login；**其他開著的
// 分頁**靠這裡監聽 localStorage 的 storage 事件（只會在其他分頁觸發），一起提示重新
// 登入，避免舊分頁在沒重新登入的情況下繼續操作。（使用者 2026-09-01）
(function () {
  "use strict";

  var redirecting = false;

  function goRelogin(reason) {
    if (redirecting) {
      return;
    }
    redirecting = true;
    if (window.showAlert) {
      window.showAlert(reason);
    }
    window.setTimeout(function () {
      window.location = "/login";
    }, 1800);
  }

  window.addEventListener("storage", function (e) {
    if (e.key === "bahaad-relogin" && e.newValue) {
      goRelogin("帳號或密碼已在其他分頁更改，請重新登入");
    }
  });
})();
