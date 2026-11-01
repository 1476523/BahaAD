// 更改下載目錄時自動遷移既有檔案。規格見 docs/requirements/download_dir_migration.md。
//
// 流程：settings-form（底部「全部儲存」或每欄自己的「儲存」，兩者都會在 #settings-form
// 上冒出 ajaxform:done，見 ajax_forms.js）存檔後，後端如果偵測到 download_dir 真的改了
// 且有既有檔案要搬，回應會帶 needs_migration_confirm——這裡彈確認視窗，使用者確認才
// POST /settings/download-dir-migrate/start 真的觸發背景遷移，接著輪詢
// /settings/download-dir-migrate/status 顯示進度，直到 finished 才收掉。
// 頁面載入時如果剛好有一輪還在跑（例如中途重新整理），直接接著顯示進度、不用重新確認。
(function () {
  "use strict";

  var STATUS_POLL_MS = 3000;
  var pollTimer = null;

  function formatBytes(n) {
    var units = ["B", "KB", "MB", "GB"];
    var i = 0;
    n = Number(n) || 0;
    while (n >= 1024 && i < units.length - 1) {
      n /= 1024;
      i++;
    }
    return n.toFixed(i === 0 ? 0 : 1) + " " + units[i];
  }

  function confirmMessage(detail) {
    return (
      "更改下載目錄後，要把既有的 " + detail.migration_count + " 部已下載檔案（約 " +
      formatBytes(detail.migration_total_size_bytes) + "）搬到新的位置嗎？\n" +
      "會在背景進行，過程中請不要關閉或重新啟動 BahaAD——就算真的中斷了，下次啟動也會" +
      "自動接續，但會讓這輪多花一些時間重跑。\n" +
      "選「取消」的話，新的下載會直接寫進新目錄，但既有檔案會留在原本的位置，之後可以" +
      "再手動觸發一次。"
    );
  }

  function overlayEl() {
    return document.getElementById("migration-progress-overlay");
  }

  function showProgressOverlay() {
    if (overlayEl()) return;
    var root = document.getElementById("modal-root") || document.body;
    var overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.id = "migration-progress-overlay";
    var box = document.createElement("div");
    box.className = "modal-box";
    box.style.maxWidth = "26rem";
    var msg = document.createElement("p");
    msg.id = "migration-progress-msg";
    msg.textContent = "正在搬移已下載的檔案……";
    var hint = document.createElement("div");
    hint.className = "help-text";
    hint.id = "migration-progress-hint";
    hint.textContent = "請不要關閉或重新啟動 BahaAD，完成後這裡會自動顯示結果。";
    box.appendChild(msg);
    box.appendChild(hint);
    overlay.appendChild(box);
    root.appendChild(overlay);
  }

  function requestRedownload(videoSn, button) {
    button.disabled = true;
    button.textContent = "已送出";
    fetch("/manual_download/submit", {
      method: "POST",
      headers: { "X-Requested-With": "fetch", "Content-Type": "application/x-www-form-urlencoded" },
      body: "video_sn=" + encodeURIComponent(videoSn),
    }).catch(function () {
      button.disabled = false;
      button.textContent = "重新下載";
    });
  }

  function renderFinished(status) {
    var overlay = overlayEl();
    if (!overlay) return;
    var msg = document.getElementById("migration-progress-msg");
    var hint = document.getElementById("migration-progress-hint");
    if (msg) {
      msg.textContent = "✅ 遷移完成（成功搬移 " + status.migrated_count + " 部）";
    }
    if (hint) {
      hint.textContent = "";
      if (status.failed && status.failed.length) {
        var failMsg = document.createElement("p");
        failMsg.textContent = "以下項目遷移失敗，檔案仍留在原本的位置：";
        hint.appendChild(failMsg);
        var list = document.createElement("ul");
        status.failed.forEach(function (item) {
          var li = document.createElement("li");
          li.appendChild(document.createTextNode(item.title + " "));
          var btn = document.createElement("button");
          btn.type = "button";
          btn.className = "btn-neutral";
          btn.textContent = "重新下載";
          btn.addEventListener("click", function () {
            requestRedownload(item.video_sn, btn);
          });
          li.appendChild(btn);
          list.appendChild(li);
        });
        hint.appendChild(list);
      } else {
        hint.textContent = "所有檔案都已經搬到新的位置。";
      }
    }
    var box = overlay.querySelector(".modal-box");
    var actions = document.createElement("div");
    actions.className = "modal-actions";
    var okBtn = document.createElement("button");
    okBtn.type = "button";
    okBtn.className = "btn-accent";
    okBtn.textContent = "關閉";
    okBtn.addEventListener("click", function () {
      overlay.remove();
      window.location.reload();
    });
    actions.appendChild(okBtn);
    box.appendChild(actions);
  }

  function pollStatus() {
    if (pollTimer) return;
    pollTimer = window.setInterval(function () {
      fetch("/settings/download-dir-migrate/status", { headers: { Accept: "application/json" } })
        .then(function (r) {
          return r.ok ? r.json() : Promise.reject();
        })
        .then(function (status) {
          // 剛跑完那一輪的狀態一定是 active=false（正常）——要先判斷 finished 顯示
          // 結果畫面，不能先看 active 就把畫面悄悄收掉，不然使用者永遠看不到完成訊息
          // 跟失敗清單（這裡曾經真的是這樣一個反過來的順序，讓進度視窗直接消失）。
          if (status.finished) {
            window.clearInterval(pollTimer);
            pollTimer = null;
            renderFinished(status);
            return;
          }
          if (!status.active) {
            // 沒有 finished 旗標、也不是 active——代表根本沒有遷移在跑（例如頁面重新
            // 整理後才第一次輪詢，發現其實什麼都沒發生），安靜收掉進度視窗。
            window.clearInterval(pollTimer);
            pollTimer = null;
            var overlay = overlayEl();
            if (overlay) overlay.remove();
            return;
          }
          var msg = document.getElementById("migration-progress-msg");
          if (msg) {
            var phaseText = status.phase === "final_verify" ? "正在做最後確認……" : "正在搬移已下載的檔案……";
            msg.textContent =
              phaseText + "（已完成 " + status.migrated_count + " 部" +
              (status.pending_count ? "，剩 " + status.pending_count + " 部" : "") + "）";
          }
        })
        .catch(function () {
          /* 這輪查詢失敗（暫時性網路問題）——下一輪再試，不中斷輪詢 */
        });
    }, STATUS_POLL_MS);
  }

  function startMigration(oldDir, newDir) {
    fetch("/settings/download-dir-migrate/start", {
      method: "POST",
      headers: { "X-Requested-With": "fetch", "Content-Type": "application/x-www-form-urlencoded" },
      body: "old_dir=" + encodeURIComponent(oldDir) + "&new_dir=" + encodeURIComponent(newDir),
    })
      .then(function (r) {
        return r.json();
      })
      .then(function (data) {
        if (!data.ok) {
          if (window.showAlert) window.showAlert(data.error || "無法開始遷移");
          return;
        }
        showProgressOverlay();
        pollStatus();
      })
      .catch(function () {
        if (window.showAlert) window.showAlert("無法開始遷移，請稍後再試");
      });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var form = document.getElementById("settings-form");
    if (form) {
      form.addEventListener("ajaxform:done", function (e) {
        var detail = e.detail;
        if (!detail || !detail.needs_migration_confirm) return;
        window.showConfirm(confirmMessage(detail), function () {
          startMigration(detail.migration_old_dir, detail.migration_new_dir);
        });
      });
    }

    var el = document.getElementById("migration-status-data");
    if (el) {
      try {
        var initial = JSON.parse(el.textContent || "{}");
        if (initial && initial.phase) {
          showProgressOverlay();
          pollStatus();
        }
      } catch (e) {
        /* noop */
      }
    }
  });
})();
