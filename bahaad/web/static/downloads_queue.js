// 下載列表頁「進行中任務」區塊：輪詢 /browse/downloads/api/status。
//   - active 任務：完成後卡片顯示 5 秒再自動消失（綠框「下載完成」）；使用者按過
//     「中止」的則是紅框「下載已取消」（.0 改進.txt 第 11 項）
//   - failing 任務（階段 3 從已退休的 /dashboard 搬過來）：狀態「下載失敗」，封面正
//     中央一顆「重試」按鈕 → POST /browse/downloads/retry/<sn>，重試成功下一輪輪詢
//     它會回到 active、失敗卡片自動移除
// 見 docs/requirements/web_redesign.md「下載列表頁」與 web_redesign_round2.md 階段 3。
(function () {
  "use strict";

  var POLL_INTERVAL_MS = 1000;  // round 6 第 10 項：每秒一次，進度條才不會一格一格跳
  var COMPLETED_DISPLAY_MS = 5000;

  // 誠實對照目前後端實際能區分出的三種狀態（見 web/browse.py `downloads_api_status`）。
  var STATE_LABELS = {
    preparing: "前置準備",
    downloading: "下載中",
    finalizing: "解密合併中",
  };

  var grid = document.getElementById("active-downloads-grid");
  var emptyMsg = document.getElementById("active-downloads-empty");
  var cooldownBanner = document.getElementById("download-cooldown-banner");
  if (!grid) {
    return;
  }

  // .0 改進.txt 第 12 項：下載完一集後的冷卻（參考 aniGamerPlus），worker slot 還佔著、
  // 下一部還沒開始——顯示橫幅說明還要等幾秒，不會看起來像卡住。
  function renderCooldown(seconds) {
    if (!cooldownBanner) return;
    if (seconds && seconds > 0) {
      cooldownBanner.textContent = "下載冷卻中，約 " + seconds + " 秒後開始下一部";
      cooldownBanner.hidden = false;
    } else {
      cooldownBanner.hidden = true;
    }
  }

  var known = {}; // video_sn -> { el, kind: "active" | "failed" }
  var completingTimers = {};
  var aborting = {}; // video_sn -> true（使用者按了「中止」，還在等它從進行中清單消失）

  function esc(s) {
    var d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  function episodeText(task) {
    // episode_label 由後端依「補齊長度」設定組好（「第 008 集」／「特別篇 1」）
    return task.episode_label || (task.episode_number ? "第" + task.episode_number + "集" : "");
  }

  function thumbHtml(task, inner) {
    var img = task.cover_url ? '<img src="' + esc(task.cover_url) + '" alt="">' : "";
    return '<div class="task-thumb">' + img + (inner || "") + "</div>";
  }

  function episodeLabelForConfirm(task) {
    var t = task.anime_title || ("sn " + task.video_sn);
    var ep = task.episode_label || (task.episode_number ? "第" + task.episode_number + "集" : "");
    return "《" + t + (ep ? " " + ep : "") + "》";
  }

  // 紅底白圖示的控制鈕（僅圖示，放在「狀態」那一行的右側，使用者 2026-08-28 回饋）
  function dangerBtn(cls, icon, label) {
    return (
      '<button type="button" class="task-danger-btn ' + cls + '" title="' + esc(label) + '" aria-label="' + esc(label) + '">' +
      '<img src="/static/icons/' + icon + '" alt="">' +
      "</button>"
    );
  }

  // 「狀態文字 ＋ 右側控制鈕」同一行
  function stateRow(stateHtml, btnHtml) {
    return '<div class="task-state-row"><p class="task-state">' + stateHtml + "</p>" + btnHtml + "</div>";
  }

  function activeCardBody(task) {
    // 已按「中止」、還沒從進行中清單消失：顯示「取消中…」、不再給中止鈕，避免重複點
    if (aborting[task.video_sn]) {
      return (
        thumbHtml(task) +
        '<p class="task-title">' + esc(task.anime_title) + "</p>" +
        '<p class="task-episode">' + esc(episodeText(task)) + "</p>" +
        stateRow("取消中…", "")
      );
    }
    var percentText = task.percent != null ? task.percent + "%" : "";
    var stateLabel = STATE_LABELS[task.state] || task.state;
    // 遊客／非 VIP 看廣告下載：前置準備階段顯示細部狀態（「等待廣告播放（約 N 秒）」等），
    // 不要一直只顯示「前置準備」（使用者 2026-09-01 回饋）
    if (task.state === "preparing" && task.phase) stateLabel = task.phase;
    return (
      thumbHtml(task) +
      '<p class="task-title">' + esc(task.anime_title) + "</p>" +
      '<p class="task-episode">' + esc(episodeText(task)) + "</p>" +
      stateRow(
        esc(stateLabel) + (percentText ? "　" + percentText : ""),
        dangerBtn("task-abort-btn", "abort-white.png", "中止")
      ) +
      '<div class="task-progress-bar"><div class="task-progress-fill" style="width:' +
      (task.percent || 0) + '%"></div></div>'
    );
  }

  function failedCardBody(task) {
    // 半透明深色 scrim 上永遠用白圖示，跟頁面主題無關（見 style.css .task-retry-btn）
    var retryBtn =
      '<button type="button" class="task-retry-btn icon-btn" title="重試">' +
      '<img src="/static/icons/refresh-white.png" alt="重試">' +
      "</button>";
    return (
      thumbHtml(task, retryBtn) +
      '<p class="task-title">' + esc(task.anime_title) + "</p>" +
      '<p class="task-episode">' + esc(episodeText(task)) + "</p>" +
      stateRow(
        "下載失敗" + (task.failure_count > 1 ? "（連續 " + task.failure_count + " 次）" : ""),
        dangerBtn("task-discard-btn", "discard-white.png", "丟棄（不再下載）")
      ) +
      // round 7 第 17 項：把失敗原因顯示出來（例如「這一集是付費會員限定」）
      (task.last_error ? '<p class="task-error">' + esc(task.last_error) + "</p>" : "")
    );
  }

  function awaitingMoveCardBody(task) {
    return (
      thumbHtml(task) +
      '<p class="task-title">' + esc(task.anime_title) + "</p>" +
      '<p class="task-episode">' + esc(episodeText(task)) + "</p>" +
      '<p class="task-state">下載完成，等待搬進下載目錄</p>' +
      '<div class="task-move-actions">' +
      '<button type="button" class="task-retrymove-btn btn-accent">再試一次</button>' +
      '<a class="btn-neutral" href="/settings#download_dir">更改下載目錄</a>' +
      "</div>"
    );
  }

  function retry(sn, btn) {
    if (btn.disabled) {
      return;
    }
    btn.disabled = true;
    fetch("/browse/downloads/retry/" + sn, { method: "POST" })
      .then(function (r) {
        return r.json();
      })
      .then(function (data) {
        if (data.ok) {
          var RESULT_MSG = {
            submitted: "已重新送出下載",
            already_downloaded: "這一集其實已經下載完成了",
            already_active: "這一集已經在下載中",
            blocked_by_access_gate: "帳號多 IP 防護啟用中，暫時無法下載",
          };
          showToast(RESULT_MSG[String(data.result || "").toLowerCase()] || "已重新送出下載");
          // 下一輪輪詢通常就會把卡片換成 active；沒換到（例如已下載過）也讓使用者能再按
          window.setTimeout(function () {
            btn.disabled = false;
          }, 3000);
        } else {
          showAlert("重試失敗：" + (data.error || "請稍後再試"));
          btn.disabled = false;
        }
      })
      .catch(function () {
        showAlert("重試失敗，請稍後再試一次");
        btn.disabled = false;
      });
  }

  function renderCard(sn, kind, bodyHtml, cardClass, wire) {
    var existing = known[sn];
    if (existing && existing.kind === kind) {
      existing.el.className = cardClass;
      existing.el.innerHTML = bodyHtml;
    } else {
      if (existing) {
        existing.el.remove();
      }
      var el = document.createElement("div");
      el.className = cardClass;
      el.innerHTML = bodyHtml;
      grid.appendChild(el);
      known[sn] = { el: el, kind: kind };
    }
    if (wire) {
      wire(known[sn].el);
    }
  }

  function abort(sn, task, btn) {
    if (btn.disabled) return;
    showConfirm("確定要中止 " + episodeLabelForConfirm(task) + " 的下載嗎？已下載的暫存檔會一併清除。", function () {
      btn.disabled = true;
      fetch("/browse/downloads/abort/" + sn, { method: "POST" })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          // aborted:false ＝按下去那一刻它其實已經下載完了／不在進行中——別把它畫成「已取消」
          if (data && data.aborted === false) {
            showToast("這一集已經下載完成了");
            btn.disabled = false;
            return;
          }
          aborting[sn] = true;
          showToast("已中止下載");
          var entry = known[sn];
          if (entry && entry.kind === "active") {
            entry.el.innerHTML = activeCardBody(task);  // 立刻換成「取消中…」，不等下一輪輪詢
          }
        })
        .catch(function () { showAlert("中止失敗，請稍後再試"); btn.disabled = false; });
    });
  }

  function discard(sn, task, btn) {
    if (btn.disabled) return;
    showConfirm("確定要丟棄 " + episodeLabelForConfirm(task) + " 嗎？之後的自動排程檢查也不會再下載這一集。", function () {
      btn.disabled = true;
      fetch("/browse/downloads/discard/" + sn, { method: "POST" })
        .then(function (r) { return r.json(); })
        .then(function () { showToast("已丟棄"); })
        .catch(function () { showAlert("丟棄失敗，請稍後再試"); btn.disabled = false; });
    });
  }

  function retryMove(sn, btn) {
    if (btn.disabled) return;
    btn.disabled = true;
    fetch("/browse/downloads/retry-move/" + sn, { method: "POST" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        showToast(data.message || (data.ok ? "已搬進下載目錄" : "還是搬不進去"));
        if (!data.ok) window.setTimeout(function () { btn.disabled = false; }, 2000);
      })
      .catch(function () { showAlert("再試一次失敗，請稍後再試"); btn.disabled = false; });
  }

  function renderActive(task) {
    // 這個 sn 之前是失敗卡片、現在重試回到 active：清掉「完成後移除」的計時器
    if (completingTimers[task.video_sn]) {
      window.clearTimeout(completingTimers[task.video_sn]);
      delete completingTimers[task.video_sn];
    }
    renderCard(task.video_sn, "active", activeCardBody(task), "task-card state-" + task.state, function (el) {
      var btn = el.querySelector(".task-abort-btn");
      if (btn) btn.addEventListener("click", function () { abort(task.video_sn, task, btn); });
    });
  }

  function renderFailed(task) {
    delete aborting[task.video_sn]; // 竟然是失敗收場（而非中止成功）——當一般失敗處理
    renderCard(task.video_sn, "failed", failedCardBody(task), "task-card state-failed", function (el) {
      var rBtn = el.querySelector(".task-retry-btn");
      if (rBtn) rBtn.addEventListener("click", function () { retry(task.video_sn, rBtn); });
      var dBtn = el.querySelector(".task-discard-btn");
      if (dBtn) dBtn.addEventListener("click", function () { discard(task.video_sn, task, dBtn); });
    });
  }

  function renderAwaitingMove(task) {
    renderCard(task.video_sn, "awaiting_move", awaitingMoveCardBody(task), "task-card state-awaiting-move", function (el) {
      var btn = el.querySelector(".task-retrymove-btn");
      if (btn) btn.addEventListener("click", function () { retryMove(task.video_sn, btn); });
    });
  }

  // active 卡片從進行中清單消失後的收尾：正常完成＝綠框「下載完成」；使用者按過
  // 「中止」＝紅框「下載已取消」（.0 改進.txt 第 11 項）。兩種都停留 COMPLETED_DISPLAY_MS
  // 再自己移除。
  function finishActive(sn, cssClass, stateText, refreshDownloaded) {
    var entry = known[sn];
    if (!entry || entry.kind !== "active" || completingTimers[sn]) {
      return;
    }
    entry.el.classList.add(cssClass);
    var stateEl = entry.el.querySelector(".task-state");
    if (stateEl) {
      stateEl.textContent = stateText;
    }
    if (refreshDownloaded) {
      refreshDownloadedSection();  // round 6 第 11 項：完成即時出現在「已下載的番劇」
    }
    completingTimers[sn] = window.setTimeout(function () {
      entry.el.remove();
      delete known[sn];
      delete completingTimers[sn];
      delete aborting[sn];
      updateEmptyState();
    }, COMPLETED_DISPLAY_MS);
  }

  function markCompleted(sn) {
    finishActive(sn, "completed", "下載完成", true);
  }

  function markCancelled(sn) {
    finishActive(sn, "cancelled", "下載已取消", false);
  }

  function updateEmptyState() {
    emptyMsg.style.display = Object.keys(known).length === 0 ? "" : "none";
  }

  var downloadedSection = document.getElementById("downloaded-anime");
  function refreshDownloadedSection() {
    if (!downloadedSection) return;
    fetch("/browse/downloads/downloaded")
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (html) { if (html !== null) downloadedSection.innerHTML = html; })
      .catch(function () {});
  }

  function poll() {
    fetch("/browse/downloads/api/status")
      .then(function (response) {
        return response.json();
      })
      .then(function (data) {
        renderCooldown(data.cooldown_seconds);
        var seen = {};
        (data.active || []).forEach(function (task) {
          seen[task.video_sn] = true;
          renderActive(task);
        });
        (data.failing || []).forEach(function (task) {
          seen[task.video_sn] = true;
          renderFailed(task);
        });
        (data.awaiting_move || []).forEach(function (task) {
          seen[task.video_sn] = true;
          renderAwaitingMove(task);
        });
        Object.keys(known).forEach(function (snKey) {
          var sn = Number(snKey);
          if (seen[sn] || completingTimers[sn]) {
            return;
          }
          if (known[sn].kind === "active") {
            // 消失的 active：按過「中止」＝已取消（紅框），否則＝下載完成（綠框）
            if (aborting[sn]) {
              markCancelled(sn);
            } else {
              markCompleted(sn);
            }
          } else {
            var wasAwaitingMove = known[sn].kind === "awaiting_move";
            known[sn].el.remove(); // 消失的 failed/awaiting_move＝已被清掉／處理完
            delete known[sn];
            delete aborting[sn]; // 別讓中止旗標殘留（同一 sn 之後重下會誤顯示「取消中…」）
            if (wasAwaitingMove) refreshDownloadedSection(); // 搬完 → 已下載區塊更新
          }
        });
        updateEmptyState();
      })
      .catch(function () {
        // 輪詢失敗（暫時網路問題）不彈錯誤打斷使用者，下一輪自然會重試
      });
  }

  poll();
  window.setInterval(poll, POLL_INTERVAL_MS);

  // round 7 第 19 項：「已下載的番劇」區塊定期重掃——使用者手動刪掉某集檔案時，
  // 伺服器端 list_by_anime() 對每筆做 Path.exists() 檢查，區塊即時反映（集數減少／
  // 番劇消失）。純掃檔案存在、不打網路，每 2 秒一次負擔可接受。
  if (downloadedSection) {
    window.setInterval(refreshDownloadedSection, 2000);
  }
})();
