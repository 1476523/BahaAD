// 「近期熱播」——第一頁自動載入，之後「點擊載入更多」補下一頁（round 7 第 3 項：
// 改成跟「相關動畫」一樣手動點擊，不再下滑無限捲動）。
//
// 資料來源是 animeList.php?sort=2（依月人氣排序）；/browse/trending/api?page=N 回
// {html, page, has_next}，html 片段 append 進 #trending-grid（卡片片段跟其他頁共用
// _cards.html macro，鈴鐺靠 subscribe.js 的 document 事件委派）。
(function () {
  "use strict";

  var grid = document.getElementById("trending-grid");
  var status = document.getElementById("trending-status");
  var more = document.getElementById("trending-load-more");
  if (!grid || !status) {
    return;
  }
  var moreBtn = more ? more.querySelector("button") : null;
  var nextPage = 1;
  var hasNext = true;
  var loading = false;

  function setStatus(mode, text) {
    status.classList.toggle("is-loading", mode === "loading");
    status.hidden = mode === "hidden";
    var label = status.querySelector("span");
    if (label) {
      label.textContent = text || "載入中…";
    }
  }

  function load() {
    if (loading || !hasNext) {
      return;
    }
    loading = true;
    if (moreBtn) moreBtn.disabled = true;
    var firstPage = grid.children.length === 0;
    if (firstPage) {
      setStatus("loading", "載入中…");
    }
    var label = moreBtn && moreBtn.querySelector("span");
    var idleText = label ? label.textContent : "";
    if (label) {
      label.textContent = "載入中…";
    }

    fetch("/browse/trending/api?page=" + encodeURIComponent(nextPage))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.error) {
          throw new Error(data.error);
        }
        if (data.html) {
          grid.insertAdjacentHTML("beforeend", data.html);
        }
        nextPage = data.page + 1;
        hasNext = !!data.has_next;
        if (grid.children.length === 0) {
          setStatus("message", "目前沒有抓到近期熱播的資料");
          if (more) more.hidden = true;
        } else {
          setStatus("hidden");
          if (more) more.hidden = !hasNext;
          if (label) label.textContent = idleText;
        }
      })
      .catch(function () {
        if (grid.children.length === 0) {
          setStatus("message", "載入失敗，重新整理再試一次");
        }
        if (label) label.textContent = "載入失敗，點一下重試";
      })
      .finally(function () {
        loading = false;
        if (moreBtn) moreBtn.disabled = false;
      });
  }

  if (moreBtn) {
    moreBtn.addEventListener("click", load);
  }
  load(); // 第一頁自動
})();
