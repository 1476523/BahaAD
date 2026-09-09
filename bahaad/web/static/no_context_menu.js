// 使用者 2026-09-04：除了輸入框（input / textarea / contenteditable），整站禁用右鍵選單。
(function () {
  "use strict";

  var EDITABLE = 'input, textarea, [contenteditable=""], [contenteditable="true"]';

  document.addEventListener(
    "contextmenu",
    function (e) {
      var t = e.target;
      if (t && t.closest && t.closest(EDITABLE)) {
        return; // 輸入框上放行（複製／貼上／選字要用）
      }
      e.preventDefault();
    },
    { capture: true }
  );
})();
