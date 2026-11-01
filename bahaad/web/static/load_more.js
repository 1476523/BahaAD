// 「點擊載入更多」共用元件——相關動畫（番劇詳細頁）與搜尋結果共用同一套。
// 見 docs/requirements/web_redesign_round3.md 階段 3。
//
// 使用者要「點擊觸發」，不是下滑無限捲動（那是近期熱播 trending.js 的做法）。
//
// 掃描頁面上每個 .load-more 容器，各接一顆按鈕。兩種模式：
//   fetch 模式（有 data-endpoint）：點擊 → GET data-endpoint?offset=N，
//     預期回 {html, has_next, next_offset}，html append 進 data-target 指的格子。
//   reveal 模式（沒有 data-endpoint）：data-target 格子裡已 render 好、但帶 hidden +
//     data-load-more-hidden 的卡片，點一次顯示接下來 data-step 個（不再打網路）。
//
// 沒有更多 → 圖示換 refresh-done、文字換 data-done-text、按鈕停用。
(function () {
  "use strict";

  function themeVariant() {
    return document.documentElement.dataset.theme === "light" ? "black" : "white";
  }

  function markDone(box, btn) {
    box.dataset.hasNext = "false";
    var img = btn.querySelector("img.theme-icon");
    if (img) {
      img.dataset.icon = "refresh-done";
      img.src = "/static/icons/refresh-done-" + themeVariant() + ".png";
    }
    var label = btn.querySelector("span");
    if (label) {
      label.textContent = box.dataset.doneText || "已經沒有更多了";
    }
    btn.disabled = true;
  }

  function setupFetch(box, btn) {
    var busy = false;
    btn.addEventListener("click", function () {
      if (busy || box.dataset.hasNext !== "true") {
        return;
      }
      busy = true;
      // 使用者 2026-08-31：跟「近期熱播」一樣，載入時把按鈕變灰、顯示「載入中…」
      btn.disabled = true;
      var target = document.querySelector(box.dataset.target);
      var label = btn.querySelector("span");
      var idleText = label ? label.textContent : "";
      if (label) {
        label.textContent = "載入中…";
      }
      // endpoint 可能已經帶篩選 query（搜尋篩選面板），有 ? 就接 &
      var sep = box.dataset.endpoint.indexOf("?") === -1 ? "?" : "&";
      var url = box.dataset.endpoint + sep +
        "offset=" + encodeURIComponent(box.dataset.nextOffset || "0");
      fetch(url)
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.html && target) {
            target.insertAdjacentHTML("beforeend", data.html);
          }
          box.dataset.nextOffset = String(data.next_offset);
          if (data.has_next) {
            if (label) { label.textContent = idleText; }
          } else {
            markDone(box, btn);
          }
        })
        .catch(function () {
          if (label) { label.textContent = "載入失敗，點一下重試"; }
        })
        .finally(function () {
          busy = false;
          // markDone 已把按鈕永久 disable 的就別再打開
          if (box.dataset.hasNext === "true") { btn.disabled = false; }
        });
    });
  }

  function setupReveal(box, btn) {
    var step = parseInt(box.dataset.step, 10) || 12;
    btn.addEventListener("click", function () {
      var target = document.querySelector(box.dataset.target);
      if (!target || box.dataset.hasNext !== "true") {
        return;
      }
      var hidden = target.querySelectorAll("[data-load-more-hidden]");
      for (var i = 0; i < hidden.length && i < step; i++) {
        hidden[i].removeAttribute("hidden");
        hidden[i].removeAttribute("data-load-more-hidden");
      }
      if (target.querySelectorAll("[data-load-more-hidden]").length === 0) {
        markDone(box, btn);
      }
    });
  }

  document.querySelectorAll(".load-more").forEach(function (box) {
    var btn = box.querySelector("button");
    if (!btn) {
      return;
    }
    if (box.dataset.endpoint) {
      setupFetch(box, btn);
    } else {
      setupReveal(box, btn);
    }
  });
})();
