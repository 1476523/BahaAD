// 共用的網頁內提示/確認元件。規格見 docs/requirements/web_overview.md「前端 UI 慣例」——
// 所有 blueprint 的模板都引用這支檔案，不用瀏覽器原生的 alert()/confirm()/prompt()，
// 也不要讓每個頁面各自寫一套彈窗邏輯。
//
// 一律用 DOM API 建立節點、textContent 塞文字，不用 innerHTML 拼字串——訊息內容雖然
// 目前都是後端寫死的字串，仍然照安全的方式寫，不留習慣性的 XSS 隱患。

(function () {
  function getRoot() {
    var root = document.getElementById("modal-root");
    if (!root) {
      root = document.createElement("div");
      root.id = "modal-root";
      document.body.appendChild(root);
    }
    return root;
  }

  function closeModal() {
    getRoot().innerHTML = "";
  }

  function buildOverlay() {
    var overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    var box = document.createElement("div");
    box.className = "modal-box";
    overlay.appendChild(box);
    return { overlay: overlay, box: box };
  }

  function iconVariant() {
    return document.documentElement.dataset.theme === "light" ? "black" : "white";
  }

  function addParagraph(parent, text) {
    var p = document.createElement("p");
    p.textContent = text;
    parent.appendChild(p);
    return p;
  }

  // message 可以是：
  //   - 字串：換行（\n）拆成獨立段落（modal-box 很窄，長訊息硬塞一段不好讀）
  //   - 物件 { icon, heading, lines: [...] }：帶圖示的標題列＋逐行段落
  //   - 陣列：多個上述區塊組在同一個對話框（見 subscribe.js 的「訂閱說明＋退訂警示」）
  //   icon 是 static/icons/<icon>-<variant>.png 的檔名前綴。
  function addMessage(parent, message) {
    if (Array.isArray(message)) {
      message.forEach(function (part) {
        addMessage(parent, part);
      });
      return;
    }
    if (message && typeof message === "object") {
      if (message.icon || message.heading) {
        var head = document.createElement("div");
        head.className = "modal-heading";
        if (message.icon) {
          head.dataset.icon = message.icon;
          var img = document.createElement("img");
          img.src = "/static/icons/" + message.icon + "-" + iconVariant() + ".png";
          img.alt = "";
          head.appendChild(img);
        }
        if (message.heading) {
          var span = document.createElement("span");
          span.textContent = message.heading;
          head.appendChild(span);
        }
        parent.appendChild(head);
      }
      (message.lines || []).forEach(function (line) {
        addParagraph(parent, line);
      });
      return;
    }
    String(message).split("\n").forEach(function (line) {
      addParagraph(parent, line);
    });
  }

  window.showAlert = function (message) {
    var built = buildOverlay();
    addMessage(built.box, message);

    var actions = document.createElement("div");
    actions.className = "modal-actions";
    var okButton = document.createElement("button");
    okButton.type = "button";
    okButton.className = "btn-accent";
    okButton.textContent = "確定";
    okButton.addEventListener("click", closeModal);
    actions.appendChild(okButton);
    built.box.appendChild(actions);

    getRoot().appendChild(built.overlay);
    okButton.focus();
  };

  window.showToast = function (message, durationMs) {
    var toast = document.createElement("div");
    toast.className = "toast";
    toast.textContent = message;
    getRoot().appendChild(toast);
    window.setTimeout(function () {
      toast.remove();
    }, durationMs || 3000);
  };

  window.showConfirm = function (message, onConfirm) {
    var built = buildOverlay();
    addMessage(built.box, message);

    var actions = document.createElement("div");
    actions.className = "modal-actions";
    var cancelButton = document.createElement("button");
    cancelButton.type = "button";
    cancelButton.textContent = "取消";
    cancelButton.addEventListener("click", closeModal);
    var okButton = document.createElement("button");
    okButton.type = "button";
    okButton.className = "btn-accent";
    okButton.textContent = "確定";
    okButton.addEventListener("click", function () {
      closeModal();
      onConfirm();
    });
    actions.appendChild(cancelButton);
    actions.appendChild(okButton);
    built.box.appendChild(actions);

    getRoot().appendChild(built.overlay);
  };

  // 文字輸入對話框：onConfirm(value) 拿到輸入的字（可能是空字串）。placeholder 選填。
  // 見 web_redesign_round3.md 階段 6-5（非訂閱番劇下載前問資料夾名稱）。
  window.showPrompt = function (message, placeholder, onConfirm) {
    var built = buildOverlay();
    addMessage(built.box, message);

    var input = document.createElement("input");
    input.type = "text";
    input.autocomplete = "off";
    if (placeholder) {
      input.placeholder = placeholder;
    }
    built.box.appendChild(input);

    var actions = document.createElement("div");
    actions.className = "modal-actions";
    var cancelButton = document.createElement("button");
    cancelButton.type = "button";
    cancelButton.textContent = "取消";
    cancelButton.addEventListener("click", closeModal);
    var okButton = document.createElement("button");
    okButton.type = "button";
    okButton.className = "btn-accent";
    okButton.textContent = "確定";
    okButton.addEventListener("click", function () {
      var value = input.value;
      closeModal();
      onConfirm(value);
    });
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") {
        e.preventDefault();
        okButton.click();
      }
    });
    actions.appendChild(cancelButton);
    actions.appendChild(okButton);
    built.box.appendChild(actions);

    getRoot().appendChild(built.overlay);
    input.focus();
  };

  // 給不可逆的破壞性操作用（例如重置 BahaAD）：要求使用者輸入一段指定文字才能按下確定，
  // 比單純點兩次確認按鈕更難誤觸。
  window.showTypedConfirm = function (message, requiredText, onConfirm) {
    var built = buildOverlay();
    addParagraph(built.box, message);
    addParagraph(built.box, "請輸入「" + requiredText + "」以確認：");

    var input = document.createElement("input");
    input.type = "text";
    input.autocomplete = "off";
    built.box.appendChild(input);

    var actions = document.createElement("div");
    actions.className = "modal-actions";
    var cancelButton = document.createElement("button");
    cancelButton.type = "button";
    cancelButton.textContent = "取消";
    cancelButton.addEventListener("click", closeModal);
    var okButton = document.createElement("button");
    okButton.type = "button";
    okButton.className = "btn-accent";
    okButton.textContent = "確定";
    okButton.disabled = true;
    okButton.addEventListener("click", function () {
      closeModal();
      onConfirm();
    });
    input.addEventListener("input", function () {
      okButton.disabled = input.value !== requiredText;
    });
    actions.appendChild(cancelButton);
    actions.appendChild(okButton);
    built.box.appendChild(actions);

    getRoot().appendChild(built.overlay);
    input.focus();
  };

  window.closeModal = closeModal;
})();
