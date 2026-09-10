// .0 改進.txt 第 21 項：停在首頁時，週期表時段一到就自動抓最新首頁資料。
//
// 首頁原本的設計是「只有開首頁才會重抓」（沒有背景輪詢）——頁面一直開著就永遠是
// 開頁當下那份。這裡加一個計時器：算出「下一個週期表時段 + 3 分鐘」（跟後端
// home_policy.POST_SLOT_DELAY 一致，給站方資料更新的緩衝），到點就 location.reload()。
// reload 後端會重新跑 home_refresh_decision——有時段剛過就抓新的、否則照樣用快取，
// 所以這個 reload 不會平白多打站方。分頁在背景時先不動，等回到前景再重載。
(function () {
  "use strict";

  var POST_SLOT_DELAY_MS = 3 * 60 * 1000;
  // 下限拉到 5 分鐘：番劇時段密集（一天幾十部），某個時段剛過的那幾分鐘內 target 會
  // 很小，30 秒下限會讓首頁在使用者沒預期時就整頁 reload（使用者 2026-09-08 回報
  // 「明明還沒到更新時候就自己變載入中」）。5 分鐘後再查，後端 home_refresh_decision
  // 該抓就抓、否則照樣秒回快取，不會平白多打站方。
  var MIN_DELAY_MS = 5 * 60 * 1000;
  var MAX_DELAY_MS = 6 * 60 * 60 * 1000; // 沒有可解析的時段時的保底，順便把 24h 不刷新的問題封頂
  var WEEK_MS = 7 * 24 * 60 * 60 * 1000;

  function nowWeekOffsetMs() {
    var now = new Date();
    var jsDow = now.getDay(); // 0=週日…6=週六
    var isoDow = jsDow === 0 ? 7 : jsDow; // 1=週一…7=週日
    return (
      (isoDow - 1) * 24 * 60 * 60 * 1000 +
      now.getHours() * 60 * 60 * 1000 +
      now.getMinutes() * 60 * 1000 +
      now.getSeconds() * 1000
    );
  }

  // 距離「下一次某個時段 + 3 分鐘」還有多少 ms（把所有 .schedule-entry 掃過取最小）
  function msUntilRefresh() {
    var entries = document.querySelectorAll(".schedule-entry[data-weekday][data-time]");
    if (!entries.length) {
      return null;
    }
    var nowOff = nowWeekOffsetMs();
    var best = Infinity;
    entries.forEach(function (el) {
      var wd = parseInt(el.dataset.weekday, 10); // 1=週一…7=週日
      var parts = String(el.dataset.time).split(":");
      var h = parseInt(parts[0], 10);
      var m = parseInt(parts[1], 10);
      if (isNaN(wd) || isNaN(h) || isNaN(m)) {
        return;
      }
      var slotOff = (wd - 1) * 24 * 60 * 60 * 1000 + h * 60 * 60 * 1000 + m * 60 * 1000;
      var target = slotOff - nowOff + POST_SLOT_DELAY_MS;
      while (target <= 0) {
        target += WEEK_MS; // 這個時段的重載時機已經過了 → 下一週
      }
      if (target < best) {
        best = target;
      }
    });
    return best === Infinity ? null : best;
  }

  function reloadNow() {
    if (document.hidden) {
      document.addEventListener("visibilitychange", function once() {
        if (!document.hidden) {
          document.removeEventListener("visibilitychange", once);
          window.location.reload();
        }
      });
    } else {
      window.location.reload();
    }
  }

  function scheduleNext() {
    var until = msUntilRefresh();
    var delay = until === null ? MAX_DELAY_MS : Math.min(MAX_DELAY_MS, Math.max(MIN_DELAY_MS, until));
    window.setTimeout(reloadNow, delay);
  }

  document.addEventListener("DOMContentLoaded", scheduleNext);
})();
