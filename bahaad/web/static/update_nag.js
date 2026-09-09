// 工具更新的提醒視窗 + 頂列工具更新鈕。見 docs/requirements/updater.md（2026-08-28 兩級改版）。
//
// 一般更新（policy="general"）：進頁面就彈一次提醒視窗，之後每小時再問一次
// /update/pending，有更新且視窗還沒開著就彈。「稍後再說」只關掉視窗，下次換頁或滿一
// 小時又彈。頂列黃色工具更新鈕點下去也能叫出視窗。
// 重點更新（policy="important"）：整個網頁 UI 已被鎖在首頁（後端 before_request 硬回
// 503），不自動彈視窗；頂列紅色工具更新鈕點下去可以叫出「立即套用」視窗。
(function () {
  "use strict";

  var POLL_MS = 3600 * 1000;
  var MODAL_ID = "update-nag-modal";
  var RESTART_POLL_MS = 1500;

  // 套用更新：fetch POST（不走整頁表單送出）→ 顯示不可關閉的「正在重新啟動」遮罩 →
  // 每 1.5s 輪詢 /update/version，等伺服器回來、版本號變成目標版本 → 試著關掉這個
  // 分頁（重啟時後端已 _reopen_web_on_start，會自動在新分頁開 BahaAD）；關不掉就把
  // 遮罩換成「更新完成」訊息，使用者自己關或重新載入（使用者 2026-09-08：關掉再重開）。
  function applyUpdate(targetVersion) {
    var overlay = showRestartOverlay();
    fetch("/dashboard/apply-update", {
      method: "POST",
      headers: { "X-Requested-With": "fetch", Accept: "application/json" },
    })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
      .then(function (data) {
        var want = (data && data.target_version) || targetVersion || null;
        pollForRestart(overlay, want);
      })
      .catch(function () {
        // 伺服器可能在回應前就開始關閉 → 也可能真的失敗。先當作已受理去輪詢；
        // 輪詢夠久還沒回來會在 pollForRestart 裡顯示逾時訊息。
        pollForRestart(overlay, targetVersion || null);
      });
  }

  function showRestartOverlay() {
    var root = document.getElementById("modal-root");
    var existing = document.getElementById("update-restart-overlay");
    if (existing) { return existing; }
    var overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.id = "update-restart-overlay";
    var box = document.createElement("div");
    box.className = "modal-box";
    box.style.maxWidth = "26rem";
    box.style.textAlign = "center";
    var p = document.createElement("p");
    p.id = "update-restart-msg";
    p.textContent = "正在更新並重新啟動……";
    var hint = document.createElement("p");
    hint.className = "help-text";
    hint.id = "update-restart-hint";
    hint.textContent = "完成後會自動在新分頁開啟 BahaAD，這個分頁會自動關閉。";
    box.appendChild(p);
    box.appendChild(hint);
    overlay.appendChild(box);
    (root || document.body).appendChild(overlay);
    return overlay;
  }

  function pollForRestart(overlay, wantVersion) {
    var startedAt = Date.now();
    var sawDown = false;
    var timer = setInterval(function () {
      fetch("/update/version", { headers: { Accept: "application/json" }, cache: "no-store" })
        .then(function (r) { return r.ok ? r.json() : Promise.reject(); })
        .then(function (data) {
          // 伺服器一度斷線（sawDown）再回來，或版本號已是目標版本 → 更新完成
          var ready = sawDown || !wantVersion || (data && data.version === wantVersion);
          if (ready) {
            clearInterval(timer);
            finishRestart(overlay);
          }
        })
        .catch(function () {
          sawDown = true; // 小幫手正在覆蓋安裝目錄，伺服器暫時連不上——預期中
          if (Date.now() - startedAt > 90000) {
            clearInterval(timer);
            timeoutRestart(overlay);
          }
        });
    }, RESTART_POLL_MS);
  }

  function finishRestart(overlay) {
    try { window.close(); } catch (e) { /* 非 script 開的分頁關不掉 */ }
    setTimeout(function () {
      var msg = document.getElementById("update-restart-msg");
      var hint = document.getElementById("update-restart-hint");
      if (msg) { msg.textContent = "✅ 更新完成"; }
      if (hint) {
        hint.textContent = "已在新分頁開啟 BahaAD，可以關閉這個分頁。";
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn-accent";
        btn.textContent = "在這個分頁重新載入";
        btn.style.marginTop = "0.75rem";
        btn.addEventListener("click", function () { window.location.href = "/"; });
        hint.parentNode.appendChild(btn);
      }
    }, 600);
  }

  function timeoutRestart(overlay) {
    var msg = document.getElementById("update-restart-msg");
    var hint = document.getElementById("update-restart-hint");
    if (msg) { msg.textContent = "更新可能仍在進行"; }
    if (hint) {
      hint.textContent = "如果 BahaAD 沒有自動開啟，請手動重新啟動，或稍後重新載入這個分頁。";
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn-accent";
      btn.textContent = "重新載入";
      btn.style.marginTop = "0.75rem";
      btn.addEventListener("click", function () { window.location.reload(); });
      hint.parentNode.appendChild(btn);
    }
  }

  function initialPending() {
    var el = document.getElementById("pending-update-data");
    if (!el) { return null; }
    try {
      var data = JSON.parse(el.textContent || "{}");
      return data && data.policy ? data : null;
    } catch (e) {
      return null;
    }
  }

  function buildModal(pending, dismissable) {
    if (!pending || document.getElementById(MODAL_ID)) { return; }
    var root = document.getElementById("modal-root");
    if (!root) { return; }

    var overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    var box = document.createElement("div");
    box.className = "modal-box";
    box.id = MODAL_ID;
    overlay.appendChild(box);

    var kind = pending.minor ? "小更新" : (pending.policy === "important" ? "重點更新" : "更新");
    var head = document.createElement("p");
    head.textContent = "有" + kind + "可以套用（" + (pending.version || "") + "）";
    box.appendChild(head);

    if (pending.notes) {
      var notes = document.createElement("p");
      notes.className = "help-text";
      notes.textContent = pending.notes;
      box.appendChild(notes);
    }

    var warn = document.createElement("p");
    warn.className = "help-text";
    warn.textContent = pending.policy === "important"
      ? "這是必須套用的更新，套用會關閉並重新啟動 BahaAD。"
      : "套用會關閉並重新啟動 BahaAD。";
    box.appendChild(warn);

    var actions = document.createElement("div");
    actions.className = "modal-actions";

    if (dismissable) {
      var later = document.createElement("button");
      later.type = "button";
      later.textContent = "稍後再說";
      later.addEventListener("click", function () { overlay.remove(); });
      actions.appendChild(later);
    }

    var apply = document.createElement("button");
    apply.type = "button";
    apply.className = "btn-accent";
    apply.textContent = "立即套用並重新啟動";
    apply.addEventListener("click", function () {
      apply.disabled = true;
      applyUpdate(pending && pending.version);
    });
    actions.appendChild(apply);

    box.appendChild(actions);
    root.appendChild(overlay);
  }

  // 一般更新才自動彈；重點更新靠首頁橫幅 + 紅色頂列鈕
  function autoNag(pending) {
    if (pending && pending.policy === "general") {
      buildModal(pending, true);
    }
  }

  // base.html 的全站橫幅按鈕是一個 <form>（沒有 JS 時仍可用傳統送出）。有 JS 就攔下來
  // 走 applyUpdate() 的 fetch + 遮罩 + 輪詢流程。
  function wireBannerForm() {
    var form = document.getElementById("apply-update-form");
    if (!form) { return; }
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var btn = form.querySelector("button");
      if (btn) { btn.disabled = true; }
      var pending = initialPending();
      applyUpdate(pending && pending.version);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var pending = initialPending();
    autoNag(pending);
    wireBannerForm();

    var toggle = document.getElementById("update-toggle");
    if (toggle) {
      toggle.addEventListener("click", function () {
        buildModal(pending, pending && pending.policy === "general");
      });
    }

    setInterval(function () {
      fetch("/update/pending", { headers: { Accept: "application/json" } })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          pending = data && data.policy ? data : null;
          autoNag(pending);
        })
        .catch(function () {});
    }, POLL_MS);
  });
})();
