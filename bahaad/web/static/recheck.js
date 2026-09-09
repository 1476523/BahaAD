// 「重新檢查排程更新」前端流程。見 docs/requirements/web_redesign_round2.md 階段 4。
//
// 按鈕（`[data-recheck]`）→ 確認框 → 進度框（輪詢 /browse/recheck/status）→ 結果框
// （逐項選「下載」／「標記為已下載」）→ 下載進度框 → 完成。
//
// 檢查／下載都在後端背景執行緒（scheduler/recheck.py 的 RecheckCoordinator），這裡
// 只負責啟動、輪詢狀態、把使用者的逐項選擇送回去。換頁再回來會靠 initFromStatus()
// 重新接上進行中的檢查。
(function () {
  "use strict";

  var POLL_MS = 1500;

  var root = document.getElementById("modal-root");
  var pollTimer = null;
  var buttons = []; // 頁面上所有 [data-recheck] 按鈕，狀態變化時一起 disable/換圖示

  // ---- 小工具 ----

  function esc(s) {
    var d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  function iconVariant() {
    return document.documentElement.dataset.theme === "light" ? "black" : "white";
  }

  function closeModal() {
    var overlay = root.querySelector(".recheck-overlay");
    if (overlay) {
      overlay.remove();
    }
  }

  function renderModal(bodyHtml, actionsHtml) {
    closeModal();
    var overlay = document.createElement("div");
    overlay.className = "modal-overlay recheck-overlay";
    overlay.innerHTML =
      '<div class="modal-box recheck-box">' +
      '<div class="recheck-body">' + bodyHtml + "</div>" +
      '<div class="modal-actions">' + (actionsHtml || "") + "</div>" +
      "</div>";
    root.appendChild(overlay);
    return overlay;
  }

  var doneState = false; // 檢查完成後圖示要維持 refresh-done

  function paintButtonIcons() {
    var name = doneState ? "refresh-done" : "refresh";
    var v = iconVariant();
    buttons.forEach(function (btn) {
      var img = btn.querySelector("img");
      if (img) {
        img.src = "/static/icons/" + name + "-" + v + ".png";
      }
    });
  }

  function setButtonsBusy(busy, done) {
    doneState = !!done;
    buttons.forEach(function (btn) {
      btn.disabled = busy;
    });
    paintButtonIcons();
  }

  // shell.js 切主題時會把 data-icon="refresh" 的圖示無條件換成 refresh-<variant>，
  // 丟掉「已完成」狀態——監看 <html data-theme> 變化，切完後把圖示重畫回來。
  new MutationObserver(paintButtonIcons).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme"],
  });

  // ---- 各階段畫面 ----

  function showCheckingModal(st) {
    var line = st.total
      ? "檢查中… （" + st.checked + " / " + st.total + " 部）"
      : "檢查中…";
    renderModal(
      "<p>" + esc(line) + "</p>" +
      '<p class="help-text">執行期間會暫停自動排程檢查，但不會中斷進行中的下載。</p>',
      '<button type="button" data-act="cancel">取消</button>'
    );
  }

  function showDownloadingModal(st) {
    if (st.submitting) {
      // 背景執行緒還在逐集送出（每一集一次 get_video()）——數字還沒定案，先顯示準備中
      renderModal(
        "<p>準備下載中…</p>" +
        '<p class="help-text">全部完成後會自動恢復排程。可以離開這一頁，下載會在背景繼續。</p>',
        '<button type="button" data-act="close">關閉</button>'
      );
      return;
    }
    var total = st.downloading_total || 0;
    var doneCount = total - (st.downloading_remaining || 0);
    renderModal(
      "<p>下載中… （" + doneCount + " / " + total + "）</p>" +
      '<p class="help-text">全部完成後會自動恢復排程。可以離開這一頁，下載會在背景繼續。</p>',
      '<button type="button" data-act="close">關閉</button>'
    );
  }

  function showResultsModal(found) {
    if (!found || !found.length) {
      return;
    }
    var groups = found
      .map(function (g) {
        var rows = g.episodes
          .map(function (ep) {
            return (
              '<label class="recheck-ep">' +
              '<span class="recheck-ep-label">第 ' + esc(ep.episode_label) + " 集</span>" +
              '<select data-video-sn="' + ep.video_sn + '">' +
              '<option value="download">下載</option>' +
              '<option value="skip">標記為已下載</option>' +
              "</select>" +
              "</label>"
            );
          })
          .join("");
        return (
          '<div class="recheck-group">' +
          "<h4>" + esc(g.anime_title) + "</h4>" +
          rows +
          "</div>"
        );
      })
      .join("");

    renderModal(
      '<p>找到 ' + found.reduce(function (n, g) { return n + g.episodes.length; }, 0) +
      " 集新的更新。每一集選「下載」或「標記為已下載」（標記後自動排程不會再排入）：</p>" +
      '<div class="recheck-bulk">' +
      "全部設為：" +
      '<button type="button" data-bulk="download">下載</button> ' +
      '<button type="button" data-bulk="skip">標記為已下載</button>' +
      "</div>" +
      '<div class="recheck-list">' + groups + "</div>",
      '<button type="button" data-act="cancel">取消</button>' +
      '<button type="button" class="btn-accent" data-act="confirm">確認</button>'
    );
  }

  // ---- 輪詢 ----

  function stopPolling() {
    if (pollTimer !== null) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function startPolling() {
    stopPolling();
    pollOnce();
    pollTimer = window.setInterval(pollOnce, POLL_MS);
  }

  function pollOnce() {
    fetch("/browse/recheck/status")
      .then(function (r) { return r.json(); })
      .then(applyStatus)
      .catch(function () { /* 下一輪再試 */ });
  }

  var lastState = "idle";

  function applyStatus(st) {
    var state = st.state;

    if (state === "checking") {
      setButtonsBusy(true, false);
      showCheckingModal(st);
    } else if (state === "results_ready") {
      setButtonsBusy(true, false);
      stopPolling(); // 等使用者操作，不用一直輪詢
      showResultsModal(st.found);
    } else if (state === "downloading") {
      setButtonsBusy(true, false);
      showDownloadingModal(st);
    } else {
      // idle / error
      stopPolling();
      var wasBusy = lastState !== "idle" && lastState !== "error";
      closeModal();
      if (state === "error" && st.error) {
        showAlert("重新檢查失敗：" + st.error);
        setButtonsBusy(false, false);
      } else if (lastState === "checking") {
        showToast("沒有找到新的更新");
        setButtonsBusy(false, true);
      } else if (wasBusy) {
        showToast("重新檢查完成");
        setButtonsBusy(false, true);
      } else {
        setButtonsBusy(false, false);
      }
    }
    lastState = state;
  }

  // ---- 動作 ----

  function startRecheck(target) {
    var url = target === "all" ? "/browse/recheck" : "/browse/recheck/" + target;
    fetch(url, { method: "POST" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.started === false) {
          showAlert("已經有一輪重新檢查在進行中了。");
          return;
        }
        // 標記成「檢查中」，即使第一次輪詢就回 idle（檢查很快、沒找到新集數）也能在
        // applyStatus 的 idle 分支認出「剛跑完一輪檢查」而顯示「沒有找到新的更新」。
        lastState = "checking";
        setButtonsBusy(true, false);
        startPolling();
      })
      .catch(function () { showAlert("啟動重新檢查失敗，請稍後再試一次"); });
  }

  function submitChoices(overlay) {
    var choices = {};
    overlay.querySelectorAll("select[data-video-sn]").forEach(function (sel) {
      choices[sel.dataset.videoSn] = sel.value;
    });
    fetch("/browse/recheck/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ choices: choices }),
    })
      .then(function (r) { return r.json(); })
      .then(function () { startPolling(); })
      .catch(function () { showAlert("送出失敗，請稍後再試一次"); });
  }

  function cancelRecheck() {
    fetch("/browse/recheck/cancel", { method: "POST" }).catch(function () {});
    stopPolling();
    closeModal();
    setButtonsBusy(false, false);
    lastState = "idle";
  }

  // ---- 事件 ----

  document.addEventListener("click", function (event) {
    var trigger = event.target.closest("[data-recheck]");
    if (trigger) {
      event.preventDefault();
      if (trigger.disabled) {
        return;
      }
      var target = trigger.dataset.recheck; // "all" 或 video_sn
      var title = (trigger.dataset.animeTitle || "").trim();
      var what = target === "all" ? "所有收藏" : title ? "《" + title + "》" : "這部番劇";
      showConfirm(
        "重新檢查" + what + "有沒有漏掉的更新。\n執行期間會暫停自動排程檢查（不中斷進行中的下載）。要執行嗎？",
        function () { startRecheck(target); }
      );
      return;
    }

    var overlay = event.target.closest(".recheck-overlay");
    if (!overlay) {
      return;
    }
    var act = event.target.dataset.act;
    var bulk = event.target.dataset.bulk;
    if (bulk) {
      overlay.querySelectorAll("select[data-video-sn]").forEach(function (sel) {
        sel.value = bulk;
      });
    } else if (act === "cancel") {
      cancelRecheck();
    } else if (act === "close") {
      closeModal();
    } else if (act === "confirm") {
      submitChoices(overlay);
    }
  });

  document.addEventListener("DOMContentLoaded", function () {
    buttons = Array.prototype.slice.call(document.querySelectorAll("[data-recheck]"));
    // 換頁回來時接上進行中的檢查
    fetch("/browse/recheck/status")
      .then(function (r) { return r.json(); })
      .then(function (st) {
        if (st.state && st.state !== "idle") {
          applyStatus(st);
          if (st.state !== "results_ready") {
            startPolling();
          }
        }
      })
      .catch(function () {});
  });
})();
