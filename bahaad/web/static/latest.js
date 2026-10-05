// 「最新上架」內容載入。見 docs/requirements/web_redesign_round4.md。
//
// 資料來源是首頁 #blockAnimeNewArrive（代理商依上架日排列，~21 筆一次給完、無分頁、
// 無「看更多」）。跟近期熱播不同——這裡不做無限捲動，進頁打一次 /browse/latest/api
// 把卡片 HTML 片段塞進 #latest-grid 就結束。畫面先出殼＋置中載入動畫（#latest-status），
// 避免 server-render 冷快取時整頁卡住讓使用者以為沒點到。
(function () {
  "use strict";

  var grid = document.getElementById("latest-grid");
  var status = document.getElementById("latest-status");
  var count = document.getElementById("latest-count");
  if (!grid || !status) {
    return;
  }

  function setStatus(mode, text) {
    // mode: "loading" | "message" | "hidden"
    status.classList.toggle("is-loading", mode === "loading");
    status.hidden = mode === "hidden";
    var label = status.querySelector("span");
    if (label) {
      label.textContent = text || "載入中…";
    }
  }

  fetch("/browse/latest/api")
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (data.error) {
        setStatus("message", "最新上架資料抓取失敗，稍後重新整理再試一次。（" + data.error + "）");
        return;
      }
      if (data.html && data.html.trim()) {
        grid.insertAdjacentHTML("beforeend", data.html);
      }
      if (grid.children.length === 0) {
        setStatus("message", "目前沒有抓到最新上架的資料。");
        return;
      }
      if (count) {
        count.textContent = "代理商新授權上架的番劇 共 " + grid.children.length + " 部";
        count.hidden = false;
      }
      setStatus("hidden");
    })
    .catch(function () {
      setStatus("message", "載入失敗，重新整理再試一次。");
    });
})();
