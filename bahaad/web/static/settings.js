// 設定頁「一般設定」表單的兩件事（round 6 第 4 項）：
//   1. 「還原」鈕只把該欄輸入框改回預設值，不送出表單、不重新整理頁面——真正存檔靠「儲存」
//   2. 每個輸入框自己一個 undo/redo 堆疊：Ctrl+Z 復原、Ctrl+Shift+Z / Ctrl+Y 重做
//      （原生 input 本來就有 Ctrl+Z，但這裡也把「還原」造成的變更納進同一個堆疊）
(function () {
  "use strict";

  var form = document.getElementById("settings-form");
  if (!form) return;

  // --- 1. 還原鈕（client-side） ---
  form.querySelectorAll("[data-reset-default]").forEach(function (btn) {
    btn.addEventListener("click", function (e) {
      e.preventDefault(); // JS 接手：不送出 formaction（那會整頁重載）
      var input = form.querySelector('[name="' + btn.getAttribute("data-reset-key") + '"]');
      if (!input) return;
      input.value = btn.getAttribute("data-reset-default");
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.focus();
    });
  });

  // --- 2. per-input undo/redo ---
  form.querySelectorAll("input").forEach(function (input) {
    var stack = [input.value];
    var ptr = 0;
    var applying = false;

    input.addEventListener("input", function () {
      if (applying) return;
      stack = stack.slice(0, ptr + 1);
      stack.push(input.value);
      ptr = stack.length - 1;
    });

    input.addEventListener("keydown", function (e) {
      var z = e.key === "z" || e.key === "Z";
      var y = e.key === "y" || e.key === "Y";
      if (!(e.ctrlKey || e.metaKey)) return;
      if (z && !e.shiftKey) {
        if (ptr > 0) { ptr--; set(); e.preventDefault(); }
      } else if ((z && e.shiftKey) || y) {
        if (ptr < stack.length - 1) { ptr++; set(); e.preventDefault(); }
      }
    });

    function set() {
      applying = true;
      input.value = stack[ptr];
      applying = false;
    }
  });
})();
