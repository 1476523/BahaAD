// 訂閱／下載列表卡片上的「公開」勾選框（使用者 2026-09-04）：勾了訪客在公開模式下才
// 看得到這部番劇。點一下直接存，失敗就還原。事件委派掛在 document 上，下載列表片段
// 被 downloads_queue.js 換掉後還是有效。
(function () {
  "use strict";

  document.addEventListener("change", function (e) {
    var box = e.target;
    if (!box.classList || !box.classList.contains("public-toggle")) return;
    var title = box.dataset.title || "";
    if (!title) return;
    var want = box.checked;
    box.disabled = true;

    var body = new FormData();
    body.append("title", title);
    if (want) body.append("public", "on");

    fetch("/browse/public-anime", {
      method: "POST",
      headers: { "X-Requested-With": "fetch" },
      body: body,
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok) {
          box.checked = !want;
          if (window.showAlert) window.showAlert((d && d.error) || "更新失敗");
        } else if (window.showToast) {
          window.showToast(want ? "已設為公開" : "已取消公開");
        }
      })
      .catch(function () {
        box.checked = !want;
        if (window.showAlert) window.showAlert("更新失敗，請稍後再試");
      })
      .finally(function () { box.disabled = false; });
  });
})();
