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
  // 點縮圖 → 置中放大浮層（觸控裝置沒有 hover、長按又會被瀏覽器接走變成「網頁預覽」，
  // 使用者 2026-09-10）。點浮層任一處關閉。
  var coverOverlay = null;
  function showCoverPop(coverUrl) {
    if (!coverUrl) return;
    if (!coverOverlay) {
      coverOverlay = document.createElement("div");
      coverOverlay.className = "leaderboard-cover-overlay";
      coverOverlay.hidden = true;
      coverOverlay.appendChild(document.createElement("img"));
      coverOverlay.addEventListener("click", function () { coverOverlay.hidden = true; });
      document.body.appendChild(coverOverlay);
    }
    coverOverlay.firstChild.src = coverUrl;
    coverOverlay.hidden = false;
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

      // 小封面縮圖：一律顯示（不靠 hover）。點它 → 放大浮層；`data-cache-src` 交給
      // img_loader.js 批次載入。
      var thumb = null;
      if (r.cover) {
        thumb = document.createElement("img");
        thumb.className = "leaderboard-thumb";
        thumb.setAttribute("data-cache-src", r.cover);
        thumb.alt = "";
        thumb.loading = "lazy";
        thumb.addEventListener("click", function (cover) {
          return function (e) { e.preventDefault(); showCoverPop(cover); };
        }(r.cover));
      }

      // 番劇名稱：可點擊前往番劇頁面，desktop hover 顯示番劇圖。
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
      if (thumb) li.appendChild(thumb);
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
    var REFRESH_MS = 120000;
    function load() {
      getJSON("/ui/stats/leaderboard", function (data) {
        BOARDS.forEach(function (b) {
          renderBoard(cards[b.key], data ? data[b.key] : null);
        });
        document.dispatchEvent(new CustomEvent("bahaad:leaderboard-refreshed"));
      });
    }
    load();
    setInterval(load, REFRESH_MS);
  }

  // ---- 排行榜頁「即時更新」前的呼吸燈：跟伺服器的同步狀態 --------------
  // 使用者 2026-09-13：不要獨立顯示，要看得出伺服器連線狀態／下次刷新秒數／
  // 本機還沒送出的資料筆數，設計比照播放器的網路狀態呼吸燈（同一組 CSS class）。
  function initSyncStatus() {
    var el = document.getElementById("stats-net");
    var tipEl = document.getElementById("stats-net-tip");
    if (!el || !tipEl) return;

    var LEADERBOARD_REFRESH_MS = 120000; // 跟 initLeaderboard 的輪詢間隔一致
    var nextRefreshAt = Date.now() + LEADERBOARD_REFRESH_MS;
    var state = { connected: null, pending: 0, nextRetry: null };

    function render() {
      var netState = "unknown";
      if (state.connected === true) netState = state.pending > 0 ? "ok" : "good";
      else if (state.connected === false) netState = "bad";
      el.dataset.net = netState;

      var secsLeft = Math.max(0, Math.round((nextRefreshAt - Date.now()) / 1000));
      var lines = [];
      lines.push(
        "伺服器連線狀態：" +
          (state.connected === null ? "—" : state.connected ? "正常" : "連線失敗（重試中）")
      );
      if (state.connected === false && state.nextRetry != null) {
        lines.push("下次重試：" + Math.max(0, state.nextRetry) + " 秒後");
      }
      lines.push("下次刷新：" + secsLeft + " 秒後");
      lines.push("未上傳資料筆數：" + state.pending + " 筆");
      tipEl.textContent = lines.join("\n");
    }

    function loadStatus() {
      getJSON("/ui/stats/status", function (data) {
        if (data) {
          state.connected = data.connected;
          state.pending = data.pending_count || 0;
          state.nextRetry = data.next_retry_seconds;
        }
        render();
      });
    }

    document.addEventListener("bahaad:leaderboard-refreshed", function () {
      nextRefreshAt = Date.now() + LEADERBOARD_REFRESH_MS;
      render();
    });

    loadStatus();
    setInterval(loadStatus, 30000); // 狀態本身比排行榜輪詢快一點，燈號比較即時
    setInterval(render, 1000); // 倒數用，不用每秒都打伺服器

    // 滑鼠移上／focus／行動裝置長按顯示提示——跟播放器網路燈同一套互動
    var holdTimer = null;
    function show() { tipEl.hidden = false; }
    function hide() { tipEl.hidden = true; }
    el.addEventListener("mouseenter", show);
    el.addEventListener("mouseleave", hide);
    el.addEventListener("focus", show);
    el.addEventListener("blur", hide);
    el.addEventListener("touchstart", function () { holdTimer = setTimeout(show, 350); }, { passive: true });
    el.addEventListener("touchend", function () { clearTimeout(holdTimer); hide(); });
    el.addEventListener("touchcancel", function () { clearTimeout(holdTimer); hide(); });
  }

  window.BahaStats = { fmt: fmt, ping: ping, initSummary: initSummary };

  document.addEventListener("DOMContentLoaded", function () {
    initSummary();
    initAnime();
    initLeaderboard();
    initSyncStatus();
  });
})();
