// 新番快訊的「追蹤」鈕（愛心）：跟訂閱鈴鐺（subscribe.js）同樣效果——POST 完就地切換
// 圖示狀態，不整頁重新整理（使用者 2026-10-05：追蹤時整個網頁會刷新）。
// 表單 `.newanime-track-form`（列表卡片、詳細頁各一個）；沒有 JS 時表單照常送出、走
// 後端 redirect 的退路。後端看到 `X-Requested-With: fetch` 回 `{ok, message, tracked}`。
(function () {
  "use strict";

  var TITLE_TRACKED = "取消追蹤";
  var TITLE_UNTRACKED = "追蹤（正式上架後自動轉訂閱）";

  function applyState(form, tracked) {
    var btn = form.querySelector("button");
    if (btn) {
      btn.classList.toggle("subscribed", tracked);
      btn.title = tracked ? TITLE_TRACKED : TITLE_UNTRACKED;
      btn.disabled = false;
      var img = btn.querySelector("img");
      if (img) { img.alt = tracked ? "取消追蹤" : "追蹤"; }
    }
    form.action = form.action.replace(/\/(un)?track$/, tracked ? "/untrack" : "/track");
  }

  // 同一部新番的追蹤鈕可能同時出現在多處，一起切換
  function syncAll(virtualSn, tracked) {
    document
      .querySelectorAll('.newanime-track-form[data-virtual-sn="' + virtualSn + '"]')
      .forEach(function (f) { applyState(f, tracked); });
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form.classList || !form.classList.contains("newanime-track-form")) { return; }
    event.preventDefault();
    var btn = form.querySelector("button");
    if (btn) { btn.disabled = true; }
    fetch(form.action, { method: "POST", headers: { "X-Requested-With": "fetch" } })
      .then(function (r) {
        if (!r.ok) { throw new Error("request failed"); }
        return r.json();
      })
      .then(function (data) {
        if (!data.ok) { throw new Error(data.message || "failed"); }
        syncAll(form.dataset.virtualSn, !!data.tracked);
      })
      .catch(function () {
        if (btn) { btn.disabled = false; }
        if (window.showAlert) { window.showAlert("追蹤失敗，請稍後再試一次"); }
      });
  });
})();
