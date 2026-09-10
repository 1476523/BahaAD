// 設定頁的「按鈕組」型欄位（目前只有「偏好畫質」）：點一下直接存這一欄，選中的按鈕
// 變色，不用另外按「儲存」（使用者 2026-09-05）。無 JS 時每顆按鈕本身是 submit、帶
// 自己的 name/value 走 formaction，一樣存得成。
(function () {
  "use strict";

  document.addEventListener("click", function (e) {
    var btn = e.target.closest(".choice-btn");
    if (!btn || btn.disabled) return;
    var group = btn.closest(".choice-btns");
    if (!group || !group.dataset.saveUrl) return;
    e.preventDefault();
    if (btn.classList.contains("selected")) return;

    var all = [].slice.call(group.querySelectorAll(".choice-btn"));
    var prev = group.querySelector(".choice-btn.selected");
    var locked = all.filter(function (b) { return b.disabled; });

    all.forEach(function (b) { b.classList.toggle("selected", b === btn); b.disabled = true; });

    var body = new FormData();
    body.append(group.dataset.field, btn.value);

    fetch(group.dataset.saveUrl, {
      method: "POST",
      headers: { "X-Requested-With": "fetch" },
      body: body,
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (window.showToast) window.showToast(d.message || (d.ok ? "已儲存" : "儲存失敗"));
        if (!d.ok) {
          all.forEach(function (b) { b.classList.toggle("selected", b === prev); });
        }
      })
      .catch(function () {
        if (window.showAlert) window.showAlert("儲存失敗，請稍後再試");
        all.forEach(function (b) { b.classList.toggle("selected", b === prev); });
      })
      .finally(function () {
        all.forEach(function (b) { b.disabled = locked.indexOf(b) !== -1; });
      });
  });
})();
