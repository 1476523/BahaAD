// 通知範本編輯：格式工具列（把選取文字包進標籤）／token chip 插入／Discord 唯讀預覽
// 即時轉換／模擬預覽 modal／模板測試（實際發一次）。見 web_redesign_round3.md 階段 5-4。
(function () {
  "use strict";

  // ---- Telegram HTML → Discord markdown（notify/render.py 規則的前端精簡版，唯讀預覽用）----
  function wrapPerLine(marker) {
    return function (_, inner) {
      return inner.split("\n").map(function (l) { return marker + l + marker; }).join("\n");
    };
  }
  var MD_RULES = [
    [/<pre>\s*<code class="language-([^"]*)">([\s\S]*?)<\/code>\s*<\/pre>/g, "```$1\n$2\n```"],
    [/<pre>([\s\S]*?)<\/pre>/g, "```\n$1\n```"],
    [/<code>([\s\S]*?)<\/code>/g, "`$1`"],
    [/<(?:b|strong)>([\s\S]*?)<\/(?:b|strong)>/g, wrapPerLine("**")],
    [/<(?:i|em)>([\s\S]*?)<\/(?:i|em)>/g, wrapPerLine("*")],
    [/<(?:u|ins)>([\s\S]*?)<\/(?:u|ins)>/g, wrapPerLine("__")],
    [/<(?:s|strike|del)>([\s\S]*?)<\/(?:s|strike|del)>/g, wrapPerLine("~~")],
    [/<tg-spoiler>([\s\S]*?)<\/tg-spoiler>/g, wrapPerLine("||")],
    [/<a href="([^"]*)">([\s\S]*?)<\/a>/g, "[$2]($1)"],
    [/<blockquote(?:\s+expandable)?>([\s\S]*?)<\/blockquote>/g, function (_, inner) {
      return inner.replace(/^\n+|\n+$/g, "").split("\n").map(function (l) { return "> " + l; }).join("\n");
    }],
  ];
  function toDiscordMarkdown(text) {
    MD_RULES.forEach(function (rule) { text = text.replace(rule[0], rule[1]); });
    return text;
  }

  function insertAtCursor(textarea, before, after) {
    var start = textarea.selectionStart;
    var end = textarea.selectionEnd;
    var selected = textarea.value.slice(start, end);
    var replacement = before + selected + after;
    textarea.value = textarea.value.slice(0, start) + replacement + textarea.value.slice(end);
    // 沒選取 → 游標放到標籤中間；有選取 → 選起包好的整段
    if (selected) {
      textarea.selectionStart = start;
      textarea.selectionEnd = start + replacement.length;
    } else {
      textarea.selectionStart = textarea.selectionEnd = start + before.length;
    }
    textarea.focus();
    textarea.dispatchEvent(new Event("input"));
  }

  document.querySelectorAll(".notify-template-block").forEach(function (block) {
    var category = block.dataset.category;
    var form = block.querySelector(".notify-template-form");
    var textarea = block.querySelector(".notify-template-textarea");
    var discordPreview = block.querySelector(".notify-discord-preview");

    function refreshDiscord() {
      discordPreview.textContent = toDiscordMarkdown(textarea.value);
    }
    textarea.addEventListener("input", refreshDiscord);
    refreshDiscord();

    // 格式工具列：data-insert 是「<b>文字</b>」這種範本，用「文字」當分隔切出前後半
    block.querySelectorAll(".notify-tool-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var tpl = btn.dataset.insert;
        var idx = tpl.indexOf("文字");
        if (idx === -1) {
          insertAtCursor(textarea, tpl, "");
        } else {
          insertAtCursor(textarea, tpl.slice(0, idx), tpl.slice(idx + 2));
        }
      });
    });

    // token chip：插到游標位置
    block.querySelectorAll(".notify-chip").forEach(function (chip) {
      chip.addEventListener("click", function () {
        insertAtCursor(textarea, chip.dataset.token, "");
      });
    });

    // 模擬預覽
    block.querySelector(".notify-preview-btn").addEventListener("click", function () {
      fetch("/notify/templates/" + category + "/preview", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: "template=" + encodeURIComponent(textarea.value),
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.error) { showAlert(data.error); return; }
          showPreviewModal(data.telegram_html, data.discord_html);
        })
        .catch(function () { showAlert("預覽失敗，請稍後再試一次"); });
    });

    // 模板測試（實際發一次）
    block.querySelector(".notify-test-tpl-btn").addEventListener("click", function () {
      showConfirm("用目前「已儲存」的範本＋範例內容，實際發一次測試通知？（未儲存的修改不會生效）", function () {
        fetch("/notify/templates/" + category + "/test", { method: "POST" })
          .then(function (r) { return r.json(); })
          .then(function (data) { showAlert(data.info); })
          .catch(function () { showAlert("測試送出失敗，請稍後再試一次"); });
      });
    });
  });

  function showPreviewModal(telegramHtml, discordHtml) {
    var root = document.getElementById("modal-root");
    var overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    var box = document.createElement("div");
    box.className = "modal-box notify-preview-modal";

    box.appendChild(makePreviewCard("Telegram", "notify-tg-bubble", telegramHtml));
    box.appendChild(makePreviewCard("Discord", "notify-dc-card", discordHtml));

    var actions = document.createElement("div");
    actions.className = "modal-actions";
    var close = document.createElement("button");
    close.type = "button";
    close.className = "btn-accent";
    close.textContent = "關閉";
    close.addEventListener("click", function () { root.innerHTML = ""; });
    actions.appendChild(close);
    box.appendChild(actions);

    overlay.appendChild(box);
    root.appendChild(overlay);
    close.focus();
  }

  function makePreviewCard(label, cls, safeHtml) {
    var wrap = document.createElement("div");
    var h = document.createElement("p");
    h.className = "help-text";
    h.textContent = label;
    var body = document.createElement("div");
    body.className = cls;
    // safeHtml 由後端 to_*_preview_html 產生：整段 escape 後只還原白名單標籤，塞 innerHTML 安全
    body.innerHTML = safeHtml;
    wrap.appendChild(h);
    wrap.appendChild(body);
    return wrap;
  }
})();
