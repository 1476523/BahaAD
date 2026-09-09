// round 7 第 5 項：<form data-ajax> 的送出改成 fetch + toast，不重新整理整頁。
// 後端端點看到 X-Requested-With: fetch 就回 JSON {ok, message}（見 web/responses.py），
// 沒有 JS 時表單照常送出、走 flash + redirect 的退路。
(function () {
  "use strict";

  function toast(message, isError) {
    if (window.showToast) {
      window.showToast(message);
    } else if (isError && window.showAlert) {
      window.showAlert(message);
    }
  }

  function submitAjax(form, submitter) {
    var btns = form.querySelectorAll('button[type="submit"], button:not([type])');
    btns.forEach(function (b) { b.disabled = true; });

    // <button formaction=...>（例如「清除」鈕）要 POST 到它自己的 formaction，不是 form.action
    var action = (submitter && submitter.getAttribute("formaction")) || form.action;

    fetch(action, {
      method: (form.method || "POST").toUpperCase(),
      headers: { "X-Requested-With": "fetch" },
      body: new FormData(form),
    })
      .then(function (r) {
        var ct = r.headers.get("content-type") || "";
        if (ct.indexOf("application/json") !== -1) {
          return r.json();
        }
        // 端點還沒改成 ajax-aware（回了 redirect/HTML）——安全退路：整頁重載
        window.location.reload();
        return null;
      })
      .then(function (data) {
        if (data === null) return;
        // 帳號／密碼已更改：session 全部失效——這個分頁提示重新登入，其他開著的分頁靠
        // localStorage 事件一起提示（auth_watch.js）。（使用者 2026-09-01）
        if (data.relogin) {
          try { localStorage.setItem("bahaad-relogin", String(Date.now())); } catch (e) { /* 私密視窗等 */ }
          var msg = data.message || "帳號或密碼已更新，請重新登入";
          if (window.showAlert) { window.showAlert(msg); } else { toast(msg); }
          window.setTimeout(function () { window.location = "/login"; }, 1800);
          return;
        }
        // 改連接埠：BahaAD 正在重啟、之後會自己開新分頁——這個分頁請關掉（使用者 2026-08-29 第 12 項）
        if (data.restart) {
          if (window.showAlert) {
            window.showAlert(data.message);
          } else {
            toast(data.message);
          }
          window.setTimeout(function () {
            try { window.close(); } catch (e) { /* 手動開的分頁關不掉，留著讓使用者自己關 */ }
          }, 4000);
          return;
        }
        toast(data.message || (data.ok ? "已儲存" : "儲存失敗"), !data.ok);
        // 端點回傳 {"update": {selector: 新文字}} → 就地更新畫面上的字，不用整頁重載
        if (data.update) {
          Object.keys(data.update).forEach(function (sel) {
            var el = document.querySelector(sel);
            if (el) { el.textContent = data.update[sel]; }
          });
        }
        // 頁面自己要做更細的就地更新（狀態徽章換色、清空輸入框…）就聽這個事件，
        // 不用再走「存檔後整頁重載」那條退路（使用者 2026-09-01：任何儲存都不該刷新）
        form.dispatchEvent(new CustomEvent("ajaxform:done", { detail: data, bubbles: true }));
      })
      .catch(function () {
        toast("儲存失敗，請稍後再試", true);
      })
      .finally(function () {
        btns.forEach(function (b) { b.disabled = false; });
      });
  }

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-ajax")) return;
    e.preventDefault();
    submitAjax(form, e.submitter);
  });
})();
