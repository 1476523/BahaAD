// 即時匿名使用統計的前端。規格見 docs/requirements/realtime_stats.md。
//
// 所有數字都「即時獨立刷新、不重載頁面」——打本地端點 /ui/stats/*（web/stats.py
// 代理去 access_gate_server 並短快取），伺服器沒上線就顯示「—」。
//
// 用 data- 屬性驅動，樣板只要標對地方：
//   [data-stats-summary]         內含 [data-stats-field="downloads_total"] 等 → 隱私浮層晶片
//   [data-stats-all-reports]     → 所有使用者已回報錯誤次數（設定頁）
//   [data-stats-anime][data-first-sn="N"]  內含 [data-stats-metric="subscription|favorite|views|completions|watching"]
//   [data-stats-leaderboard]     → BahaAD 排行榜（訂閱列表頁下方）
(function () {
  "use strict";

  var DASH = "—";

  function fmt(n) {
    if (n === null || n === undefined || isNaN(n)) return DASH;
    return Number(n).toLocaleString("en-US");
  }

  function getJSON(url, cb) {
    fetch(url, { headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { cb(d); })
      .catch(function () { cb(null); });
  }
  function postJSON(url, body, cb) {
    fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body || {}),
    })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { cb && cb(d); })
      .catch(function () { cb && cb(null); });
  }

  // ---- 播放器 ping（episode_picker.js 用）------------------------------
  var ping = {
    view: function (sn) { postJSON("/ui/stats/view/" + sn); },
    completion: function (sn) { postJSON("/ui/stats/completion/" + sn); },
    watching: function (sn) { postJSON("/ui/stats/watching/" + sn); },
    stop: function () { postJSON("/ui/stats/watching/stop"); },
    alive: function () { postJSON("/ui/stats/alive"); },
  };

  // ---- summary 晶片 + 所有使用者已回報次數 ----------------------------
  function fillFields(root, data) {
    var nodes = root.querySelectorAll("[data-stats-field]");
    Array.prototype.forEach.call(nodes, function (el) {
      var k = el.getAttribute("data-stats-field");
      el.textContent = data ? fmt(data[k]) : DASH;
    });
  }

  function initSummary() {
    var blocks = document.querySelectorAll("[data-stats-summary]");
    var reports = document.querySelectorAll("[data-stats-all-reports]");
    if (!blocks.length && !reports.length) return;
    function load() {
      getJSON("/ui/stats/summary", function (data) {
        Array.prototype.forEach.call(blocks, function (b) { fillFields(b, data); });
        Array.prototype.forEach.call(reports, function (el) {
          el.textContent = data ? fmt(data.diagnostics_reports_total) : DASH;
        });
      });
    }
    load();
    setInterval(load, 60000);
  }

  // ---- 番劇頁 / 播放器的番劇統計 -------------------------------------
  function initAnime() {
    var blocks = document.querySelectorAll("[data-stats-anime][data-first-sn]");
    if (!blocks.length) return;
    var sns = [];
    Array.prototype.forEach.call(blocks, function (b) {
      var sn = parseInt(b.getAttribute("data-first-sn"), 10);
      if (isFinite(sn) && sn > 0 && sns.indexOf(sn) === -1) sns.push(sn);
    });
    if (!sns.length) return;
    function load() {
      postJSON("/ui/stats/anime", { sns: sns }, function (map) {
        Array.prototype.forEach.call(blocks, function (b) {
          var sn = b.getAttribute("data-first-sn");
          var row = map && map[sn];
          var metrics = b.querySelectorAll("[data-stats-metric]");
          Array.prototype.forEach.call(metrics, function (el) {
            var k = el.getAttribute("data-stats-metric");
            el.textContent = row ? fmt(row[k]) : DASH;
          });
        });
      });
    }
    load();
    setInterval(load, 60000);
  }

  // ---- BahaAD 排行榜 -----------------------------------------------
  var BOARDS = [
    { key: "subscription", label: "番劇訂閱數" },
    { key: "favorite", label: "新番收藏數" },
    { key: "views", label: "番劇總觀看次數" },
    { key: "completions", label: "看完次數" },
  ];
  // 前五名的名次圖示：1-3 用 rank-N-white.png 當 CSS mask、金/銀/銅上色（不隨主題變）；
  // 4-5 用「數字圓牌」（CSS 畫的圓圈＋數字，灰色系）。6 名之後就純數字。
  function rankBadge(rank) {
    if (rank <= 3) {
      var m = document.createElement("span");
      m.className = "leaderboard-medal leaderboard-medal--" + rank;
      m.setAttribute("role", "img");
      m.setAttribute("aria-label", "第 " + rank + " 名");
      m.style.webkitMaskImage = 'url("/static/icons/rank-' + rank + '-white.png")';
      m.style.maskImage = 'url("/static/icons/rank-' + rank + '-white.png")';
      return m;
    }
    if (rank <= 5) {
      var c = document.createElement("span");
      c.className = "leaderboard-numbadge";
      c.setAttribute("aria-label", "第 " + rank + " 名");
      c.textContent = rank;
      return c;
    }
    var t = document.createElement("span");
    t.textContent = rank;
    return t;
  }

  // hover 番劇圖：一個共用的浮動 <img>，跟著滑鼠。
  var hoverImg = null;
  function ensureHoverImg() {
    if (hoverImg) return hoverImg;
    hoverImg = document.createElement("img");
    hoverImg.className = "leaderboard-cover-pop";
    hoverImg.hidden = true;
    hoverImg.alt = "";
    hoverImg.addEventListener("error", function () { hoverImg.hidden = true; });
    document.body.appendChild(hoverImg);
    return hoverImg;
  }
  function bindHoverCover(el, coverUrl) {
    if (!coverUrl) return;
    el.addEventListener("mouseenter", function () {
      var im = ensureHoverImg();
      im.src = coverUrl;
      im.hidden = false;
    });
    el.addEventListener("mousemove", function (e) {
      if (!hoverImg || hoverImg.hidden) return;
      var x = e.clientX + 16;
      var y = e.clientY + 16;
      if (x + 160 > window.innerWidth) x = e.clientX - 176;
      if (y + 220 > window.innerHeight) y = e.clientY - 236;
      hoverImg.style.left = x + "px";
      hoverImg.style.top = y + "px";
    });
    el.addEventListener("mouseleave", function () {
      if (hoverImg) hoverImg.hidden = true;
    });
  }

  function renderBoard(container, rows) {
    container.innerHTML = "";
    if (!rows || !rows.length) {
      var empty = document.createElement("p");
      empty.className = "help-text";
      empty.textContent = "尚無資料";
      container.appendChild(empty);
      return;
    }
    var ol = document.createElement("ol");
    ol.className = "leaderboard-list";
    rows.forEach(function (r, i) {
      var li = document.createElement("li");
      var rankEl = document.createElement("span");
      rankEl.className = "leaderboard-rank";
      rankEl.appendChild(rankBadge(i + 1));

      // 番劇名稱：可點擊前往番劇頁面，hover 顯示番劇圖。
      var name = document.createElement(r.url ? "a" : "span");
      name.className = "leaderboard-name";
      name.textContent = r.title || ("sn " + r.sn);
      if (r.url) {
        name.href = r.url;
        name.title = r.title || "";
      }
      bindHoverCover(name, r.cover);

      var count = document.createElement("span");
      count.className = "leaderboard-count";
      count.textContent = fmt(r.count);
      li.appendChild(rankEl);
      li.appendChild(name);
      li.appendChild(count);
      ol.appendChild(li);
    });
    container.appendChild(ol);
  }
  function initLeaderboard() {
    var host = document.querySelector("[data-stats-leaderboard]");
    if (!host) return;
    host.innerHTML = "";
    var cards = {};
    BOARDS.forEach(function (b) {
      var card = document.createElement("div");
      card.className = "leaderboard-board";
      var h = document.createElement("h3");
      h.textContent = b.label;
      var body = document.createElement("div");
      body.className = "leaderboard-body";
      body.textContent = DASH;
      card.appendChild(h);
      card.appendChild(body);
      host.appendChild(card);
      cards[b.key] = body;
    });
    function load() {
      getJSON("/ui/stats/leaderboard", function (data) {
        BOARDS.forEach(function (b) {
          renderBoard(cards[b.key], data ? data[b.key] : null);
        });
      });
    }
    load();
    setInterval(load, 120000);
  }

  window.BahaStats = { fmt: fmt, ping: ping, initSummary: initSummary };

  document.addEventListener("DOMContentLoaded", function () {
    initSummary();
    initAnime();
    initLeaderboard();
  });
})();
