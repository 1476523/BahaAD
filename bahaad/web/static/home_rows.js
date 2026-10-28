/* 公開首頁（使用者 2026-10-22）：
   1. 左右滑動的番劇列——不顯示捲軸；手機直接滑，電腦用兩側箭頭或滑鼠拖曳。
   2. 登入第三方帳號後的「繼續觀看番劇」：讀這個瀏覽器記的觀看紀錄（episode_picker.js 寫入，
      依帳號分開存），每張卡片封面右上角有丟棄鈕，只移除這筆觀看紀錄。 */
(function () {
  "use strict";

  function initScroller(root) {
    var track = root.querySelector(".hscroll-track");
    var prev = root.querySelector(".hscroll-prev");
    var next = root.querySelector(".hscroll-next");
    if (!track || root.dataset.hscrollReady) return;
    root.dataset.hscrollReady = "1";

    function sync() {
      var max = track.scrollWidth - track.clientWidth;
      if (prev) prev.hidden = track.scrollLeft <= 2;
      if (next) next.hidden = max <= 2 || track.scrollLeft >= max - 2;
    }
    function page(dir) {
      track.scrollBy({ left: dir * track.clientWidth * 0.85, behavior: "smooth" });
    }
    if (prev) prev.addEventListener("click", function () { page(-1); });
    if (next) next.addEventListener("click", function () { page(1); });

    // 滑鼠拖曳（觸控、觸控板用瀏覽器原生捲動）
    var drag = null;
    var moved = false;
    track.addEventListener("pointerdown", function (e) {
      if (e.pointerType !== "mouse" || e.button !== 0) return;
      if (e.target.closest && e.target.closest("button")) return;
      drag = { x: e.clientX, left: track.scrollLeft };
      moved = false;
    });
    window.addEventListener("pointermove", function (e) {
      if (!drag) return;
      var dx = e.clientX - drag.x;
      if (!moved && Math.abs(dx) > 5) {
        moved = true;
        track.classList.add("is-dragging");
      }
      if (moved) track.scrollLeft = drag.left - dx;
    });
    window.addEventListener("pointerup", function () {
      if (!drag) return;
      drag = null;
      track.classList.remove("is-dragging");
    });
    track.addEventListener("click", function (e) {
      if (moved) { e.preventDefault(); e.stopPropagation(); moved = false; }
    }, true);
    track.addEventListener("dragstart", function (e) { e.preventDefault(); });

    track.addEventListener("scroll", sync, { passive: true });
    window.addEventListener("resize", sync);
    if (window.ResizeObserver) new ResizeObserver(sync).observe(track);
    sync();
  }

  function safePath(value, prefix) {
    return typeof value === "string" && value.indexOf(prefix) === 0 && value.indexOf("//") !== 0 ? value : "";
  }

  function fmtClock(seconds) {
    var s = Math.max(0, Math.floor(seconds || 0));
    var m = Math.floor(s / 60);
    var h = Math.floor(m / 60);
    function two(n) { return n < 10 ? "0" + n : String(n); }
    return h > 0 ? h + ":" + two(m % 60) + ":" + two(s % 60) : m + ":" + two(s % 60);
  }

  function buildCard(entry, onDiscard) {
    var card = document.createElement("div");
    card.className = "anime-card";
    var cover = document.createElement("a");
    cover.className = "anime-card-cover";
    cover.href = safePath(entry.href, "/") || "#";
    var src = safePath(entry.cover, "/cache/");
    if (src) {
      var img = document.createElement("img");
      img.src = src;
      img.alt = "";
      img.loading = "lazy";
      img.decoding = "async";
      cover.appendChild(img);
    }
    var discard = document.createElement("button");
    discard.type = "button";
    discard.className = "anime-card-discard";
    discard.title = "移除這筆觀看紀錄";
    discard.setAttribute("aria-label", "移除這筆觀看紀錄");
    discard.textContent = "🗑";
    discard.addEventListener("click", function (e) {
      e.preventDefault();
      e.stopPropagation();
      onDiscard(entry);
    });
    cover.appendChild(discard);
    var foot = document.createElement("div");
    foot.className = "anime-card-foot";
    var text = document.createElement("div");
    text.className = "anime-card-text";
    var title = document.createElement("p");
    title.className = "anime-card-title";
    title.textContent = entry.title || "";
    var meta = document.createElement("p");
    meta.className = "anime-card-meta";
    var ep = entry.label ? "第 " + entry.label + " 集" : "";
    meta.textContent = entry.done ? "看完 " + ep : "看到 " + ep + (entry.pos > 0 ? " ・ " + fmtClock(entry.pos) : "");
    text.appendChild(title);
    text.appendChild(meta);
    foot.appendChild(text);
    card.appendChild(cover);
    card.appendChild(foot);
    return card;
  }

  function initContinue(section) {
    var storeKey = "bahaad:continue:" + section.dataset.continueKey;
    var track = section.querySelector("[data-continue-track]");
    var empty = section.querySelector("[data-continue-empty]");
    var scroller = section.querySelector("[data-hscroll]");
    if (!track) return;

    function load() {
      try {
        var list = JSON.parse(localStorage.getItem(storeKey) || "[]");
        return Array.isArray(list) ? list.filter(function (e) { return e && e.anime; }) : [];
      } catch (e) { return []; }
    }
    function discard(entry) {
      // 只移除這筆觀看紀錄（不影響集數的已看標記、播放記憶點、收藏或其他資料）
      try {
        var rest = load().filter(function (e) { return e.anime !== entry.anime; });
        localStorage.setItem(storeKey, JSON.stringify(rest));
      } catch (e) { /* 私密視窗等 */ }
      render();
    }
    function render() {
      var list = load();
      track.textContent = "";
      list.forEach(function (entry) { track.appendChild(buildCard(entry, discard)); });
      if (empty) empty.hidden = list.length > 0;
      if (scroller) {
        scroller.hidden = list.length === 0;
        initScroller(scroller);
        track.dispatchEvent(new Event("scroll"));
      }
    }
    render();
  }

  function init() {
    document.querySelectorAll("[data-hscroll]").forEach(function (root) {
      if (!root.closest("[data-continue-section]")) initScroller(root);
    });
    document.querySelectorAll("[data-continue-section]").forEach(initContinue);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
