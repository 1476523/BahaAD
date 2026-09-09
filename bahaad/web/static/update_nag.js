// 工具更新的提醒視窗 + 頂列工具更新鈕。見 docs/requirements/updater.md（2026-08-28 兩級改版）。
//
// 一般更新（policy="general"）：進頁面就彈一次提醒視窗，之後每 20 秒再問一次
// /update/pending，有更新且視窗還沒開著就彈。「稍後再說」只關掉視窗，下次換頁或滿
// 20 秒又彈。頂列黃色工具更新鈕點下去也能叫出視窗。
// 重點更新（policy="important"）：整個網頁 UI 已被鎖在首頁（後端 before_request 硬回
// 503），不自動彈視窗；頂列紅色工具更新鈕點下去可以叫出「立即套用」視窗。
//
// 開著的分頁即時反映更新狀態（橫幅原本是伺服器 render 的，不換頁不會變，使用者 2026-09-09
// 「從系統匣按檢查更新後，網頁也該顯示」）：
//   - GitHub 偵測到新版、差異檔還在背景下載（`_update_available`）→ 長出「背景下載中」橫幅
//   - 差異檔下載好（`_pending_update`）→ 換成「套用更新並重新啟動」橫幅、順便彈提醒視窗
(function () {
  "use strict";

  var POLL_MS = 20 * 1000;
  var MODAL_ID = "update-nag-modal";
  var RESTART_POLL_MS = 1500;

  // 套用更新：fetch POST（不走整頁表單送出）→ 顯示不可關閉的「正在重新啟動」遮罩 →
  // 每 1.5s 輪詢 /update/version，等伺服器斷線再回來 → 直接重新載入這個分頁
  // （使用者 2026-09-09：手動開的分頁 window.close() 關不掉，改成重新整理就好；
  //  fetch 這條路後端也不再設 _reopen_web_on_start，不會多開一個分頁）。
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
    hint.textContent = "完成後這個分頁會自動重新載入，不用手動操作。";
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
    var msg = document.getElementById("update-restart-msg");
    var hint = document.getElementById("update-restart-hint");
    if (msg) { msg.textContent = "✅ 更新完成"; }
    if (hint) { hint.textContent = "正在重新載入這個分頁……"; }
    // 伺服器已經回應成功（在 pollForRestart 的 .then 裡才會走到這），重新整理是安全的。
    setTimeout(function () { window.location.reload(); }, 800);
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
    head.textContent = "有" + kind + "可以套用（v" + (pending.version || "") + "）";
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

  function bannerText(pending) {
    var t;
    if (pending.policy === "important") {
      t = "重點更新：必須套用才能繼續使用 BahaAD（v" + (pending.version || "") + "）";
    } else {
      t = "有" + (pending.minor ? "小" : "") + "更新可套用（v" + (pending.version || "") +
          "），建議立即套用並重新啟動";
    }
    if (pending.notes) { t += "（" + pending.notes + "）"; }
    return t;
  }

  // 背景下載完成後把伺服器 render 的「背景下載中」橫幅換成「套用更新並重新啟動」橫幅
  // （或整個頁面沒有更新橫幅時，補插一條在最上面）。已經有套用按鈕就什麼都不做。
  function renderPendingBanner(pending) {
    if (!pending || document.getElementById("apply-update-form")) { return; }
    var main = document.getElementById("page-content");
    if (!main) { return; }

    var banner = document.createElement("div");
    banner.className = "warning";
    banner.id = "update-ready-banner";
    banner.appendChild(document.createTextNode(bannerText(pending) + " "));

    var form = document.createElement("form");
    form.method = "post";
    form.action = "/dashboard/apply-update";
    form.id = "apply-update-form";
    var btn = document.createElement("button");
    btn.type = "submit";
    btn.className = "btn-accent";
    btn.textContent = "套用更新並重新啟動";
    form.appendChild(btn);
    banner.appendChild(form);

    var downloading = document.getElementById("update-downloading-banner");
    if (downloading) { downloading.replaceWith(banner); }
    else { main.insertBefore(banner, main.firstChild); }

    // initialPending() / wireBannerForm() 之後也讀得到新的待套用資料
    var dataEl = document.getElementById("pending-update-data");
    if (dataEl) { try { dataEl.textContent = JSON.stringify(pending); } catch (e) { /* noop */ } }

    wireBannerForm();
  }

  // GitHub 偵測到新版、差異檔還在背景下載 → 開著的分頁補一條「背景下載中」橫幅
  // （比照 base.html `{% elif update_available %}` 那條）。已經有任何更新橫幅就不動。
  function renderDownloadingBanner(available) {
    if (!available) { return; }
    if (document.getElementById("apply-update-form")
        || document.getElementById("update-downloading-banner")) { return; }
    var main = document.getElementById("page-content");
    if (!main) { return; }

    var banner = document.createElement("div");
    banner.className = "warning";
    banner.id = "update-downloading-banner";
    banner.appendChild(document.createTextNode(
      "發現新版本 " + (available.latest_version || "")
      + "，更新檔正在背景下載…… 下載完成後這裡會出現「套用更新並重新啟動」按鈕（系統匣也會通知）。 "
    ));
    main.insertBefore(banner, main.firstChild);
  }

  // base.html 的全站橫幅按鈕是一個 <form>（沒有 JS 時仍可用傳統送出）。有 JS 就攔下來
  // 走 applyUpdate() 的 fetch + 遮罩 + 輪詢流程。
  function wireBannerForm() {
    var form = document.getElementById("apply-update-form");
    if (!form || form.dataset.wired) { return; }
    form.dataset.wired = "1";
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
          if (pending) {
            renderPendingBanner(pending);
            autoNag(pending);
          } else if (data && data.available) {
            renderDownloadingBanner(data.available);
          }
        })
        .catch(function () {});
    }, POLL_MS);
  });
})();
