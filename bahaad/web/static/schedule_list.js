// 排程清單的「編輯展開」——schedule_embed.html（訂閱列表頁右側面板 iframe，
// round 7 第 4 項）用。取消追蹤走卡片鈴鐺，這頁沒有刪除鈕。
(function () {
  "use strict";

  document.querySelectorAll(".schedule-item").forEach(function (item) {
    var editForm = item.querySelector(".schedule-item-edit");
    var toggle = item.querySelector(".schedule-edit-toggle");
    if (!editForm || !toggle) return;
    toggle.addEventListener("click", function () {
      editForm.hidden = !editForm.hidden;
    });
    var cancel = editForm.querySelector(".schedule-edit-cancel");
    if (cancel) {
      cancel.addEventListener("click", function () { editForm.hidden = true; });
    }
  });
})();
