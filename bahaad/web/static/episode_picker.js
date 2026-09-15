// 番劇詳細頁的集數選取器：多選手勢＋提交 loading 態。見
// docs/requirements/web_redesign.md「集數選取器」。
//
// 手勢定案：一般點擊＝單選（清掉其他已選的）、再點一次已選的那一格＝取消選取
// （使用者 2026-08-26 回饋）、Ctrl/Cmd+點擊＝個別加選（不動其他已選的）、
// Shift+點擊＝從上一次點擊的位置區間選取、左鍵按住拖曳＝連續加選。
// Shift 的「上一次點擊位置」跟拖曳的範圍都只在**同一個類別的集數格**內生效（本篇／
// 中文配音／特別篇分開算），不會跨類別——跨類別做區間選取沒有意義。
(function () {
  "use strict";

  function initGrid(grid) {
    // 手勢邏輯抽到 static/multi_select.js（資料庫整頓頁共用）。Shift 區間與拖曳範圍
    // 都只在同一個 grid（＝同一個集數類別）內生效——一個 grid 一組。
    if (window.initMultiSelect) {
      window.initMultiSelect(grid, {
        itemSelector: ".episode-cell",
        selectedClass: "selected",
        toggleLoneClick: true,
        onChange: function () {
          if (window._episodePickerRefreshPlay) window._episodePickerRefreshPlay();
        },
      });
    }
  }

  // 選到「已下載」（藍框，＝有紀錄且檔案還在）的單一集數 → 冒出「播放」鈕，開瀏覽器
  // 內建播放器（/anime/episode/<sn>/play 直接串本地 .mp4，支援 range 拖曳）。一般狀態
  // 不顯示播放鈕。使用者 2026-09-04。
  function initPlayer() {
    var form = document.getElementById("episode-picker-form");
    var playBtn = document.getElementById("episode-play-btn");
    var overlay = document.getElementById("episode-player-overlay");
    var video = document.getElementById("episode-player-video");
    var caption = document.getElementById("episode-player-caption");
    if (!form || !playBtn || !overlay || !video) {
      return;
    }

    var SKIP = 10;          // 快轉／倒轉秒數
    var SAVE_EVERY = 5;     // 記憶點寫入節流（秒）
    var NEAR_END = 15;      // 距離結尾這麼近就不記憶（下次從頭）
    var current = { sn: null, label: "" };

    // 看完判定：播到 80% 就算看完（使用者 2026-09-05，原本 95% 太晚，改成 80%）——
    // 跟播放記憶點一樣存 localStorage（只在這個瀏覽器，不同裝置各自記），集數格加
    // .watched 上色。
    var WATCHED_RATIO = 0.8;
    function watchedKey(sn) { return "bahaad:watched:" + sn; }
    function markWatched(sn) {
      try { localStorage.setItem(watchedKey(sn), "1"); } catch (e) { /* 私密視窗等 */ }
      applyWatchedClass(sn);
      statsMarkCompletion(sn);
    }
    function applyWatchedClass(sn) {
      var cb = form.querySelector('.episode-checkbox[value="' + sn + '"]');
      var cell = cb && cb.closest(".episode-cell");
      if (cell) cell.classList.add("watched");
    }
    function maybeMarkWatched() {
      if (!current.sn || !isFinite(video.duration) || video.duration <= 0) return;
      if (video.currentTime / video.duration >= WATCHED_RATIO) markWatched(current.sn);
    }

    // 即時匿名使用統計：開始播放 → 觀看次數 +1、標記「正在收看」；播放中每分鐘再 ping
    // 一次維持「正在收看」窗口（也讓伺服器知道「還在看番劇」不要閒置登出）；看完（80%）
    // → 看完次數 +1（每 sn 一次）；關閉 → 停止。
    var statsWatchTimer = null;
    var statsCompletedSn = null;
    function isPlaying() {
      // 「有沒有在播」——不管本機還是投放。投放走 Remote Playback API：投放中 video
      // 元素會反映遠端的播放狀態，所以遠端暫停時 video.paused 也是 true，跟本機一致。
      // 暫停（本機或投放）就不算「正在看」，只是播放器還開著。
      return !!video && !video.paused && !video.ended;
    }
    function statsPing() {
      if (!window.BahaStats) return;
      if (current.sn && isPlaying()) {
        // 真的在看（本機或投放）→ 更新「正在收看」統計 ＋ 維持登入 session
        window.BahaStats.ping.watching(current.sn);
      } else if (!overlay.hidden) {
        // 播放器開著、只是暫停 → 只維持登入 session，不動統計數字
        // （使用者 2026-09-08：播放器開著就算動作、不要閒置登出；✕ 關閉才停）
        window.BahaStats.ping.alive();
      }
    }
    function statsStartWatch(sn) {
      if (window.BahaStats) window.BahaStats.ping.view(sn);
      statsCompletedSn = null;
      if (statsWatchTimer) clearInterval(statsWatchTimer);
      statsWatchTimer = setInterval(statsPing, 60000);
    }
    function statsStopWatch() {
      if (statsWatchTimer) { clearInterval(statsWatchTimer); statsWatchTimer = null; }
      if (window.BahaStats) window.BahaStats.ping.stop();
    }
    function statsMarkCompletion(sn) {
      if (statsCompletedSn === String(sn)) return;
      statsCompletedSn = String(sn);
      if (window.BahaStats) window.BahaStats.ping.completion(sn);
    }

    function posKey(sn) { return "bahaad:playpos:" + sn; }
    function savePos() {
      if (!current.sn || !isFinite(video.duration) || video.duration <= 0) return;
      try {
        if (video.currentTime > 3 && video.duration - video.currentTime > NEAR_END) {
          localStorage.setItem(posKey(current.sn), String(Math.floor(video.currentTime)));
        } else {
          localStorage.removeItem(posKey(current.sn));
        }
      } catch (e) { /* 私密視窗等 */ }
    }
    function savedPos(sn) {
      try {
        var v = parseInt(localStorage.getItem(posKey(sn)), 10);
        return isFinite(v) && v > 0 ? v : 0;
      } catch (e) { return 0; }
    }

    // 目前 DOM 裡「已下載」的集數，依畫面順序（跨分類）——上一話／下一話用
    function downloadedList() {
      return Array.prototype.slice
        .call(form.querySelectorAll(".episode-cell.downloaded"))
        .map(function (c) {
          return { sn: c.querySelector(".episode-checkbox").value, label: c.textContent.trim() };
        });
    }
    function indexOfSn(list, sn) {
      for (var i = 0; i < list.length; i++) { if (list[i].sn === String(sn)) return i; }
      return -1;
    }

    var publicRO = document.documentElement.dataset.publicReadonly === "true";

    function refresh() {
      // 播放鈕：只在「剛好選了一格、且那格是已下載」時出現。
      // 公開模式例外：集數格全是「已下載」、也沒有下載/改名等動作，唯一能做的就是播放
      // → 沒選任何一格時也直接顯示，預設指向第一集（使用者 2026-09-04）。
      var sel = form.querySelectorAll(".episode-cell.selected");
      var cell = sel.length === 1 && sel[0].classList.contains("downloaded") ? sel[0] : null;
      if (!cell && publicRO) {
        cell = form.querySelector(".episode-cell.downloaded");
      }
      if (cell) {
        playBtn.dataset.sn = cell.querySelector(".episode-checkbox").value;
        playBtn.dataset.label = cell.textContent.trim();
        playBtn.hidden = false;
      } else {
        playBtn.hidden = true;
      }
      // 播放器開著時，上一話／下一話按鈕依目前「已下載清單」更新可用狀態
      if (!overlay.hidden) {
        var list = downloadedList();
        var i = indexOfSn(list, current.sn);
        setActState("prev", i > 0);
        setActState("next", i >= 0 && i < list.length - 1);
      }
    }
    window._episodePickerRefreshPlay = refresh;

    function setActState(act, enabled) {
      var b = overlay.querySelector('[data-player-act="' + act + '"]');
      if (b) b.disabled = !enabled;
    }

    var remote = form.dataset.remotePlayback === "true";
    var hls = null;

    function teardownHls() {
      if (hls) { try { hls.destroy(); } catch (e) { /* */ } hls = null; }
    }

    // ---- 網路狀態呼吸燈 -----------------------------------------------------
    var net = (function () {
      var lightEl = document.getElementById("episode-player-net");
      var tipEl = document.getElementById("episode-player-net-tip");
      var timer = null;
      var probeAt = 0;
      var stats = { latency: null, speed: null, verdict: "off" }; // speed: bytes/s
      // iOS Safari 原生 HLS 播順的時候也會亂發 waiting/stalled（抓下一段、ABR 切換），
      // 立刻亮紅是誤判（使用者 2026-09-06：蘋果播一陣子就紅、但不影響觀看）。改成：
      // waiting 先進「待判定」，撐過寬限期、且 currentTime 完全沒動，才算真的卡。
      var STALL_GRACE_MS = 2500;
      var stalling = false;      // 確定在卡（撐過寬限期）
      var pendingStallAt = -1;   // waiting/stalled 發生時間；-1 = 沒有待判定的卡頓
      var pendingStallTime = 0;  // 進入待判定時的 currentTime
      var recoveredAt = -1e9;    // 上次從「確定在卡」恢復的時間（恢復後短暫顯示「普通」）
      var lastTime = 0;          // 上一 tick 的 currentTime——有前進就是真的在播

      function fmtSpeed(bps) {
        if (bps == null) return "—";
        var mbps = (bps * 8) / 1e6;
        return mbps >= 1 ? mbps.toFixed(1) + " Mbps" : Math.round(mbps * 1000) + " kbps";
      }
      function bufferedAhead() {
        try {
          for (var i = 0; i < video.buffered.length; i++) {
            if (video.buffered.start(i) <= video.currentTime && video.currentTime <= video.buffered.end(i)) {
              return video.buffered.end(i) - video.currentTime;
            }
          }
        } catch (e) { /* */ }
        return 0;
      }
      function computeVerdict() {
        // 沒在放（暫停／還沒開始／播完）就不評判網路——維持中性、不亮紅
        if (video.paused || video.ended || video.readyState === 0) return "idle";
        // 紅燈：只有「確定在卡」（waiting 撐過寬限期、currentTime 沒動）。**不看**
        // video.buffered／readyState（iOS 原生 HLS 這兩個值不可靠）。
        if (stalling) return "bad";
        if (performance.now() - recoveredAt < 4000) return "ok";  // 剛從卡頓恢復，再觀察一下
        if (stats.latency != null && stats.latency > 1200) return "ok";
        // hls.js 才有可靠的緩衝數字；有的話拿來當「普通」的補充判斷（不當紅燈）
        if (hls) {
          var ahead = bufferedAhead();
          if (ahead > 0 && ahead < 3) return "ok";
        }
        return "good";
      }
      var LABEL = { good: "良好", ok: "普通", bad: "不佳", off: "偵測中", idle: "待機" };
      function render() {
        stats.verdict = computeVerdict();
        if (lightEl) lightEl.dataset.net = stats.verdict;
        if (tipEl) {
          tipEl.textContent =
            "網路判斷：" + LABEL[stats.verdict] +
            "\n網路延遲：" + (stats.latency == null ? "—" : Math.round(stats.latency) + " ms") +
            "\n網路速度：" + fmtSpeed(stats.speed) +
            "\n緩衝：" + bufferedAhead().toFixed(1) + " 秒";
        }
      }
      function probeMp4() {
        // mp4 原生播放：定期抓一小段測延遲／速度（128KB，每 8 秒一次，可忽略）
        var t0 = performance.now();
        var got = 0;
        fetch(current.sn ? "/anime/episode/" + current.sn + "/play" : "", {
          headers: { Range: "bytes=0-131071" }, cache: "no-store",
        })
          .then(function (r) {
            stats.latency = performance.now() - t0;
            return r.arrayBuffer();
          })
          .then(function (buf) {
            got = buf.byteLength;
            var secs = (performance.now() - t0) / 1000;
            if (secs > 0 && got > 0) stats.speed = got / secs;
          })
          .catch(function () {});
      }
      function tick() {
        var advanced = !video.paused && video.currentTime > lastTime + 0.05;
        if (advanced) {
          // 有在前進 ＝ 沒卡（不管有沒有收到 playing 事件——iOS 常不補發）
          if (stalling) recoveredAt = performance.now();
          stalling = false;
          pendingStallAt = -1;
        } else if (
          pendingStallAt >= 0 &&
          performance.now() - pendingStallAt > STALL_GRACE_MS &&
          video.currentTime <= pendingStallTime + 0.05
        ) {
          stalling = true;  // waiting 撐過寬限期、currentTime 完全沒動 ＝ 真的卡
        }
        lastTime = video.currentTime;

        if (hls) {
          if (typeof hls.bandwidthEstimate === "number" && hls.bandwidthEstimate > 0) {
            stats.speed = hls.bandwidthEstimate / 8; // hls.js 給 bit/s
          }
        } else if (video.src && current.sn && performance.now() - probeAt > 8000) {
          probeAt = performance.now();
          probeMp4();
        }
        render();
      }

      function onHlsFrag(evt, data) {
        var s = data && data.frag && data.frag.stats;
        if (!s || !s.loading) return;
        if (s.loading.first && s.loading.start) stats.latency = s.loading.first - s.loading.start;
        var dur = (s.loading.end - s.loading.start) / 1000;
        if (dur > 0 && s.total) stats.speed = s.total / dur;
      }

      return {
        start: function () {
          stalling = false;
          pendingStallAt = -1;
          recoveredAt = -1e9;
          lastTime = 0;
          stats = { latency: null, speed: null, verdict: "off" };
          probeAt = 0;
          if (hls && window.Hls) hls.on(window.Hls.Events.FRAG_LOADED, onHlsFrag);
          clearInterval(timer);
          timer = setInterval(tick, 500);
          tick();
        },
        stop: function () {
          clearInterval(timer);
          timer = null;
          if (lightEl) lightEl.dataset.net = "off";
        },
        markStall: function (v) {
          if (v) {
            // 進「待判定」，先不亮紅——交給 tick 看撐不撐得過寬限期
            if (pendingStallAt < 0) {
              pendingStallAt = performance.now();
              pendingStallTime = video.currentTime;
            }
          } else {
            if (stalling) recoveredAt = performance.now();
            stalling = false;
            pendingStallAt = -1;
            render();
          }
        },
      };
    })();

    (function bindNetTip() {
      var lightEl = document.getElementById("episode-player-net");
      var tipEl = document.getElementById("episode-player-net-tip");
      if (!lightEl || !tipEl) return;
      var holdTimer = null;
      function show() { tipEl.hidden = false; }
      function hide() { tipEl.hidden = true; }
      lightEl.addEventListener("mouseenter", show);
      lightEl.addEventListener("mouseleave", hide);
      lightEl.addEventListener("focus", show);
      lightEl.addEventListener("blur", hide);
      // 行動裝置：長按 350ms 顯示，放開隱藏
      lightEl.addEventListener("touchstart", function () {
        holdTimer = setTimeout(show, 350);
      }, { passive: true });
      lightEl.addEventListener("touchend", function () { clearTimeout(holdTimer); hide(); });
      lightEl.addEventListener("touchcancel", function () { clearTimeout(holdTimer); hide(); });
    })();
    function ensureHlsLib(cb) {
      if (window.Hls) return cb();
      var s = document.createElement("script");
      s.src = "/static/vendor/hls.min.js";
      s.onload = cb;
      s.onerror = function () { cb(); }; // 載不到就退回 mp4
      document.head.appendChild(s);
    }

    function afterSourceSet(sn) {
      var resume = savedPos(sn);
      video.addEventListener("loadedmetadata", function once() {
        video.removeEventListener("loadedmetadata", once);
        if (resume && (!isFinite(video.duration) || resume < video.duration - NEAR_END)) {
          video.currentTime = resume;
        }
        video.play().catch(function () {});
      });
      net.start();
      refresh();
    }

    function load(sn, label) {
      savePos();
      teardownHls();
      current = { sn: String(sn), label: label || "" };
      statsStartWatch(sn);
      caption.textContent = label ? "第 " + label + " 集" : "";
      var mp4 = "/anime/episode/" + sn + "/play";
      var m3u8 = "/anime/episode/" + sn + "/hls/index.m3u8";

      if (!remote) {
        video.src = mp4;                // 本機／區網：直接串 mp4（moov 已前置）
        video.load();
        afterSourceSet(sn);
        return;
      }
      // 遠端／公開模式：優先 hls.js（桌面 Chrome/Firefox/Edge 都要它，Chrome 的
      // canPlayType('...mpegurl') 回 'maybe' 但其實不能播）；hls.js 不支援的環境
      // （iOS Safari）才退回原生 HLS，都不行才用 mp4。
      ensureHlsLib(function () {
        if (window.Hls && window.Hls.isSupported()) {
          hls = new window.Hls({ maxBufferLength: 30 });
          hls.on(window.Hls.Events.ERROR, function (evt, data) {
            if (data && data.fatal) { teardownHls(); video.src = mp4; video.load(); }
          });
          hls.loadSource(m3u8);
          hls.attachMedia(video);
        } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
          video.src = m3u8;             // iOS / Safari 原生
          video.load();
        } else {
          video.src = mp4;
          video.load();
        }
        afterSourceSet(sn);
      });
    }
    function open(sn, label) {
      overlay.hidden = false;
      load(sn, label);
    }
    function close() {
      savePos();
      statsStopWatch();
      net.stop();
      overlay.hidden = true;
      video.pause();
      teardownHls();
      video.removeAttribute("src");
      video.load(); // 真的中斷下載
      current = { sn: null, label: "" };
    }

    ["waiting", "stalled"].forEach(function (ev) {
      video.addEventListener(ev, function () { net.markStall(true); });
    });
    ["playing", "canplay", "canplaythrough"].forEach(function (ev) {
      video.addEventListener(ev, function () { net.markStall(false); });
    });
    function step(delta) {
      if (isFinite(video.duration)) {
        video.currentTime = Math.min(video.duration, Math.max(0, video.currentTime + delta));
      }
    }
    function sibling(dir) {
      var list = downloadedList();
      var i = indexOfSn(list, current.sn);
      if (i < 0) return;
      var next = list[i + dir];
      if (next) load(next.sn, next.label);
    }

    playBtn.addEventListener("click", function () {
      if (playBtn.dataset.sn) open(playBtn.dataset.sn, playBtn.dataset.label);
    });
    // 只有 ✕ 能關——點框外不關，避免看到一半誤觸（使用者 2026-09-04）。
    overlay.addEventListener("click", function (e) {
      if (e.target.classList.contains("player-close")) close();
    });
    overlay.querySelectorAll("[data-player-act]").forEach(function (b) {
      b.addEventListener("click", function () {
        var a = b.dataset.playerAct;
        if (a === "back") step(-SKIP);
        else if (a === "fwd") step(SKIP);
        else if (a === "prev") sibling(-1);
        else if (a === "next") sibling(1);
        else if (a === "cast" && video.remote) video.remote.prompt().catch(function () {});
      });
    });
    document.addEventListener("keydown", function (e) {
      if (overlay.hidden) return;
      if (e.key === "Escape") close();
      else if (e.key === "ArrowLeft") step(-SKIP);
      else if (e.key === "ArrowRight") step(SKIP);
    });

    // 投放（Remote Playback API）——只有瀏覽器支援、且真的有可投放的裝置時才顯示按鈕。
    var castBtn = overlay.querySelector('[data-player-act="cast"]');
    if (castBtn && video.remote && typeof video.remote.watchAvailability === "function") {
      video.remote
        .watchAvailability(function (available) { castBtn.hidden = !available; })
        .catch(function () { castBtn.hidden = true; });
    }

    var lastSave = 0;
    video.addEventListener("timeupdate", function () {
      if (video.currentTime - lastSave >= SAVE_EVERY || lastSave - video.currentTime > 2) {
        lastSave = video.currentTime;
        savePos();
      }
      maybeMarkWatched();
    });
    video.addEventListener("pause", savePos);
    video.addEventListener("ended", function () {
      if (current.sn) markWatched(current.sn);  // 保險：萬一 timeupdate 沒抓到門檻那一刻
      try { localStorage.removeItem(posKey(current.sn)); } catch (e) { /* */ }
      // 播完自動接下一話（使用者 2026-09-06）——`sibling(1)` 會挑「已下載清單」裡的
      // 下一集；已經是最後一集就停在這裡（播放器只有 ✕ 能關）。
      if (!overlay.hidden) sibling(1);
    });
    window.addEventListener("beforeunload", function () { if (!overlay.hidden) savePos(); });

    refresh();
  }

  function initSubmitFlow() {
    const form = document.getElementById("episode-picker-form");
    if (!form) {
      return;
    }
    form.addEventListener("submit", function (e) {
      // e.submitter = 真正被按下的那顆 submit（下載／某分類全部下載／整部全部下載）。
      var submitter = e.submitter;

      // 階段 6-5：非訂閱番劇——第一次提交先跳 showPrompt 問資料夾名稱，把答案塞成
      // 隱藏欄位 rename 再重新提交（帶回同一顆 submitter，才知道是「下載」還是「全部下載」）。
      if (form.dataset.subscribed !== "true" && form.dataset.renamePrompted !== "true") {
        e.preventDefault();
        var title = form.dataset.animeTitle || "這部番劇";
        showPrompt("《" + title + "》要下載到哪個資料夾名稱？", "留空＝用番劇原始標題", function (name) {
          var hidden = document.createElement("input");
          hidden.type = "hidden";
          hidden.name = "rename";
          hidden.value = (name || "").trim();
          form.appendChild(hidden);
          form.dataset.renamePrompted = "true";
          form.requestSubmit(submitter || undefined);
        });
        return;
      }

      // 停用按鈕要延到下一個 tick——在 submit 事件裡同步停用，瀏覽器可能就不把被按下
      // 那顆的 name=value 序列化進表單了（等於整包送出「沒選、沒按全部下載」）。
      window.setTimeout(function () {
        form.querySelectorAll("button[type=submit]").forEach(function (b) {
          b.disabled = true;
        });
        if (submitter && submitter.id === "episode-submit-btn") {
          submitter.textContent = "正在提交...";
        }
      }, 0);
    });
  }

  // .0 改進.txt 第 13 項：停在番劇頁時，集數的下載狀態（完成／失敗／被刪）要即時
  // 反映到每一格的顏色，不用重新整理。輪詢 data-episode-states-url，只讀本地快取＋
  // registry 記憶體狀態、不打網路，所以 3 秒一次很輕（下載列表頁的輪詢是每秒一次）。
  var STATE_CLASSES = ["not-downloaded", "downloading", "downloaded", "removed", "failed"];

  function initLiveStates() {
    var form = document.getElementById("episode-picker-form");
    var url = form && form.dataset.episodeStatesUrl;
    if (!url) {
      return;
    }
    function apply(states) {
      Object.keys(states).forEach(function (sn) {
        var cb = form.querySelector('.episode-checkbox[value="' + sn + '"]');
        var cell = cb && cb.closest(".episode-cell");
        if (!cell) return;
        var next = states[sn];
        if (cell.classList.contains(next)) return;
        STATE_CLASSES.forEach(function (c) { cell.classList.remove(c); });
        cell.classList.add(next);
      });
      // 下載完成／檔案被刪 → 選中的那格狀態變了，「播放」鈕跟著出現或收起
      if (window._episodePickerRefreshPlay) window._episodePickerRefreshPlay();
    }
    function poll() {
      if (document.hidden) return;  // 分頁切到背景就不輪詢（可能同時開很多番劇分頁）
      fetch(url)
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) { if (data && data.states) apply(data.states); })
        .catch(function () {});
    }
    poll();  // 進頁面先對一次（初次 render 用的是 server 當下狀態，可能已經有變化）
    window.setInterval(poll, 3000);
  }

  // 進頁面時把上次記在 localStorage 的「已看完」套回集數格（跟播放記憶點同一套
  // 儲存方式，只在這個瀏覽器有效）。
  function initWatchedRestore() {
    document.querySelectorAll(".episode-checkbox").forEach(function (cb) {
      var watched;
      try { watched = localStorage.getItem("bahaad:watched:" + cb.value); } catch (e) { return; }
      if (!watched) return;
      var cell = cb.closest(".episode-cell");
      if (cell) cell.classList.add("watched");
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    initPlayer();
    document.querySelectorAll("[data-episode-grid]").forEach(initGrid);
    initSubmitFlow();
    initLiveStates();
    initWatchedRestore();
  });
})();
