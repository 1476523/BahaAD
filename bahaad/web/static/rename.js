// 番劇「更名」——改的是這部番劇未來下載時的資料夾名稱（schedule_entries 的 rename）。
// 只出現在已訂閱番劇的詳細頁（鈴鐺右側）。見使用者 2026-08-26 需求。
//
// 流程：按「更名」→ 標題變成輸入框、按鈕圖示變「儲存」→ 改字 → 按「儲存」→
// 前後端各檢查一次（不能空白、不能有系統不允許的檔名符號）→ POST /browse/rename/<sn>。
(function () {
  "use strict";

  // Windows 檔名不允許的字元（跟 scheduler/main_loop.py 的 _ILLEGAL_FILENAME_CHARS_RE
  // 一致）＋控制字元。空白／破折號／全形符號都允許（番劇名很常有）。
  var ILLEGAL = new RegExp("[<>:\"/\\\\|?*\\u0000-\\u001f]");
  var MAX_LEN = 120;

  function iconVariant() {
    return document.documentElement.dataset.theme === "light" ? "black" : "white";
  }

  function setButtonMode(btn, mode) {
    // mode: "rename" | "save"
    var img = btn.querySelector("img");
    if (img) {
      img.setAttribute("data-icon", mode);
      img.src = "/static/icons/" + mode + "-" + iconVariant() + ".png";
    }
    btn.dataset.editing = mode === "save" ? "true" : "false";
    btn.title = mode === "save" ? "儲存名稱" : "更改下載資料夾名稱";
  }

  function startEdit(titleEl, btn) {
    var current = btn.dataset.currentName || titleEl.textContent.trim();
    titleEl.dataset.original = titleEl.textContent;
    titleEl.textContent = "";
    var input = document.createElement("input");
    input.type = "text";
    input.className = "rename-input";
    input.value = current;
    input.maxLength = MAX_LEN;
    input.setAttribute("aria-label", "番劇名稱");
    titleEl.appendChild(input);
    input.focus();
    input.select();
    input.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") {
        ev.preventDefault();
        btn.click();
      } else if (ev.key === "Escape") {
        cancelEdit(titleEl, btn);
      }
    });
    setButtonMode(btn, "save");
  }

  function cancelEdit(titleEl, btn) {
    titleEl.textContent = titleEl.dataset.original || "";
    setButtonMode(btn, "rename");
  }

  function finishEdit(titleEl, btn, name) {
    titleEl.textContent = name;
    btn.dataset.currentName = name;
    setButtonMode(btn, "rename");
  }

  function save(titleEl, btn) {
    var input = titleEl.querySelector("input.rename-input");
    if (!input) {
      return;
    }
    var name = (input.value || "").trim();
    if (!name) {
      showAlert("名稱不能是空白。");
      input.focus();
      return;
    }
    if (ILLEGAL.test(name)) {
      showAlert('名稱不能包含這些系統不允許的符號：\n< > : " / \\ | ? *');
      input.focus();
      return;
    }
    if (name.length > MAX_LEN) {
      showAlert("名稱太長了（最多 " + MAX_LEN + " 個字）。");
      return;
    }

    btn.disabled = true;
    // 預設改番劇訂閱的資料夾名；新番快訊詳細頁用 data-rename-url 指到自己的端點
    // （改的是 newanime_item.display_name）。
    var url = btn.dataset.renameUrl || "/browse/rename/" + btn.dataset.rename;
    fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name }),
    })
      .then(function (r) {
        return r.json();
      })
      .then(function (data) {
        btn.disabled = false;
        if (data.ok) {
          finishEdit(titleEl, btn, name);
          showToast("已更新番劇名稱，之後下載會用新名稱");
        } else {
          showAlert(data.error || "更新失敗，請稍後再試一次。");
          input.focus();
        }
      })
      .catch(function () {
        btn.disabled = false;
        showAlert("更新失敗，請稍後再試一次。");
      });
  }

  document.addEventListener("click", function (event) {
    var btn = event.target.closest("[data-rename]");
    if (!btn || btn.disabled) {
      return;
    }
    event.preventDefault();
    var titleEl = document.querySelector(".anime-detail-title");
    if (!titleEl) {
      return;
    }
    if (btn.dataset.editing === "true") {
      save(titleEl, btn);
    } else {
      startEdit(titleEl, btn);
    }
  });
})();
