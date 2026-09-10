// 頂列「訂閱通知」鈕 + 下拉面板。兩種通知：
//   - 番劇完結（kind="completion"）：單行敘述 + 按鈕
//   - 官方公告（kind="gossip"）：番劇名稱／機動調整結果／原始公告文字 三行 + 按鈕
//     人工操作：立即操作（開處置視窗）／忽略公告
//     自動調整：更改操作（開處置視窗）／了解
// 載入時 fetch /notifications、每 60 秒輪詢；有未處理通知就把圖示換成 notify-animated.gif。
(function () {
  "use strict";

  var toggle = document.getElementById("notif-toggle");
  var panel = document.getElementById("notif-panel");
  var listEl = document.getElementById("notif-list");
  var emptyEl = panel ? panel.querySelector(".notif-empty") : null;
  var iconImg = document.getElementById("notif-toggle-icon");
  if (!toggle || !panel || !listEl || !iconImg) {
    return;
  }

  function variant() {
    return document.documentElement.dataset.theme === "light" ? "black" : "white";
  }

  function setIcon(hasUnresolved) {
    if (hasUnresolved) {
      iconImg.src = "/static/icons/notify-animated.gif";
      iconImg.dataset.animated = "true";
    } else {
      iconImg.src = "/static/icons/subscribe-" + variant() + ".png";
      delete iconImg.dataset.animated;
    }
  }

  function animeNode(item) {
    var frag = document.createDocumentFragment();
    frag.appendChild(document.createTextNode("《"));
    if (item.anime_url) {
      var link = document.createElement("a");
      link.href = item.anime_url;
      link.textContent = item.anime_title;
      frag.appendChild(link);
    } else {
      frag.appendChild(document.createTextNode(item.anime_title));
    }
    frag.appendChild(document.createTextNode("》"));
    return frag;
  }

  function renderItem(item) {
    var li = document.createElement("li");
    li.className = "notif-item";

    if (item.kind === "gossip") {
      var title = document.createElement("p");
      title.className = "notif-item-title";
      title.appendChild(animeNode(item));
      li.appendChild(title);

      // adjustment_text 可能帶 \n（機動調整結果 + 預計日期分行）
      (item.adjustment_text || "").split("\n").forEach(function (line) {
        if (!line) { return; }
        var adj = document.createElement("p");
        adj.className = "notif-item-adjust";
        adj.textContent = line;
        li.appendChild(adj);
      });

      var clause = document.createElement("p");
      clause.className = "notif-item-clause";
      clause.textContent = item.raw_clause || "";
      li.appendChild(clause);
    } else {
      var text = document.createElement("p");
      text.appendChild(animeNode(item));
      text.appendChild(document.createTextNode(item.detail_text || ""));
      li.appendChild(text);
    }

    var actions = document.createElement("div");
    actions.className = "notif-item-actions";
    (item.actions || []).forEach(function (a) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = a.label;
      btn.addEventListener("click", function () {
        if (a.key === "gossip_operate" || a.key === "gossip_change") {
          openDispositionModal(item);
        } else {
          runAction(item.id, a.key, btn);
        }
      });
      actions.appendChild(btn);
    });
    li.appendChild(actions);
    return li;
  }

  function render(data) {
    var items = (data && data.items) || [];
    setIcon(items.length > 0);
    listEl.innerHTML = "";
    if (emptyEl) {
      emptyEl.hidden = items.length > 0;
    }
    items.forEach(function (item) {
      listEl.appendChild(renderItem(item));
    });
  }

  function load() {
    fetch("/notifications", { headers: { Accept: "application/json" } })
      .then(function (r) { return r.json(); })
      .then(render)
      .catch(function () {});
  }

  function runAction(id, action, btn) {
    var buttons = btn.parentElement.querySelectorAll("button");
    buttons.forEach(function (b) { b.disabled = true; });
    fetch("/notifications/" + encodeURIComponent(id) + "/action", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: "action=" + encodeURIComponent(action),
    })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (!res.ok) {
          buttons.forEach(function (b) { b.disabled = false; });
          var msg = {
            no_schedule_time: "這部不在本季週期表上，暫時無法重新訂閱",
            schedule_lookup_failed: "查詢週期表失敗，請稍後再試",
          }[res.error] || res.error || "操作失敗，請稍後再試";
          if (window.showAlert) { showAlert(msg); }
          return;
        }
        load();
      })
      .catch(function () {
        buttons.forEach(function (b) { b.disabled = false; });
      });
  }

  // ---- 「立即操作／更改操作」處置視窗 ----------------------------------------
  // 抓 /gossip/actionable/<id>/form 的表單片段塞進 modal，套用跟操作處置頁一樣的
  // 「依選中處置顯示對應自訂欄位」邏輯，submit 用 fetch POST 到既有端點。

  function syncCustomFields(form) {
    var value = form.querySelector(".gossip-disposition-select").value;
    form.querySelectorAll(".gossip-custom").forEach(function (block) {
      var show = block.dataset.for === value;
      block.hidden = !show;
      block.querySelectorAll("input, select").forEach(function (field) { field.disabled = !show; });
    });
  }

  function submitForm(form) {
    return fetch(form.action, { method: "POST", body: new FormData(form) });
  }

  function wireModalForms(box, eventId) {
    var assignForm = box.querySelector(".gossip-assign-form");
    if (assignForm) {
      assignForm.addEventListener("submit", function (e) {
        e.preventDefault();
        submitForm(assignForm)
          .then(function () { return fetchForm(eventId); })
          .then(function (html) { fillModal(box, html, eventId); })
          .catch(function () { if (window.showAlert) showAlert("指定失敗，請稍後再試"); });
      });
    }
    var dispForm = box.querySelector(".gossip-disposition-form");
    if (dispForm) {
      var select = dispForm.querySelector(".gossip-disposition-select");
      select.addEventListener("change", function () { syncCustomFields(dispForm); });
      syncCustomFields(dispForm);
      dispForm.addEventListener("submit", function (e) {
        e.preventDefault();
        submitForm(dispForm)
          .then(function () {
            if (window.closeModal) closeModal();
            load();
          })
          .catch(function () { if (window.showAlert) showAlert("套用處置失敗，請稍後再試"); });
      });
    }
  }

  function fillModal(box, html, eventId) {
    box.innerHTML = "";
    var wrap = document.createElement("div");
    wrap.innerHTML = html;
    box.appendChild(wrap);

    var actions = document.createElement("div");
    actions.className = "modal-actions";
    var closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.textContent = "關閉";
    closeBtn.addEventListener("click", function () { if (window.closeModal) closeModal(); });
    actions.appendChild(closeBtn);
    box.appendChild(actions);

    wireModalForms(box, eventId);
  }

  function fetchForm(eventId) {
    return fetch("/gossip/actionable/" + encodeURIComponent(eventId) + "/form").then(function (r) {
      if (!r.ok) { throw new Error("form fetch failed"); }
      return r.text();
    });
  }

  function openDispositionModal(item) {
    var eventId = String(item.id).replace(/^gossip:/, "");
    fetchForm(eventId)
      .then(function (html) {
        var root = document.getElementById("modal-root");
        if (!root) { return; }
        var overlay = document.createElement("div");
        overlay.className = "modal-overlay";
        var box = document.createElement("div");
        box.className = "modal-box gossip-modal-box";
        overlay.appendChild(box);
        root.appendChild(overlay);
        fillModal(box, html, eventId);
      })
      .catch(function () {
        if (window.showAlert) { showAlert("開啟處置視窗失敗，請稍後再試"); }
      });
  }

  toggle.addEventListener("click", function (e) {
    e.stopPropagation();
    panel.hidden = !panel.hidden;
  });
  document.addEventListener("click", function (e) {
    if (!panel.hidden && !panel.contains(e.target) && e.target !== toggle && !toggle.contains(e.target)) {
      panel.hidden = true;
    }
  });

  // 切主題時，如果目前是「沒通知」的靜態圖示，要換成新主題的黑白版（動圖不動）
  document.addEventListener("DOMContentLoaded", function () {
    var themeBtn = document.getElementById("theme-toggle");
    if (themeBtn) {
      themeBtn.addEventListener("click", function () {
        if (iconImg.dataset.animated !== "true") {
          iconImg.src = "/static/icons/subscribe-" + variant() + ".png";
        }
      });
    }
    load();
    setInterval(load, 60000);
  });
})();
