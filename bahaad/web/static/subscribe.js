// 訂閱鈴鐺。見 docs/requirements/web_redesign.md「訂閱功能」——鈴鐺按鈕散落在
// 首頁卡片／週期表／番劇詳細頁／訂閱列表頁好幾個地方（樣板見 _bell.html），這裡
// 統一處理點擊行為：POST /browse/subscribe|unsubscribe/<sn>，成功後切換樣式，
// 訂閱列表頁的鈴鐺（data-remove-card-on-unsub="true"）取消訂閱後直接把卡片從畫面
// 移除，不用整頁重新整理。
(function () {
  "use strict";

  function setButtonState(btn, subscribed) {
    btn.dataset.subscribed = subscribed ? "true" : "false";
    btn.classList.toggle("subscribed", subscribed);
    btn.title = subscribed ? "取消訂閱" : "訂閱（依週期表時段自動下載新集數）";
  }

  // 同一部番劇的鈴鐺會出現在多處（首頁卡片＋週期表側板同一個 sn；番劇詳細頁；
  // 訂閱列表頁）——訂閱／退訂後把頁面上**所有**同 sn 的鈴鐺一起切換金色，不是只有
  // 剛按的那顆。見 web_redesign_round3.md 階段 4。
  function syncButtonsForSn(videoSn, subscribed) {
    document
      .querySelectorAll('.bell-btn[data-video-sn="' + videoSn + '"]')
      .forEach(function (b) { setButtonState(b, subscribed); });
    // 番劇詳細頁：訂閱後才顯示「更名」按鈕（使用者 2026-08-31 第 3 項）
    document
      .querySelectorAll('.rename-btn-wrap[data-rename-for="' + videoSn + '"]')
      .forEach(function (wrap) { wrap.hidden = !subscribed; });
  }

  // 對應 web/browse.py subscribe() 拒絕訂閱時回傳的 reason
  var REASON_MESSAGES = {
    schedule_lookup_failed: "查詢週期表失敗，請稍後再試一次",
    browse_unavailable: "目前無法查詢週期表，請稍後再試一次",
    ref_resolve_failed: "目前無法解析這部番劇的編號，請稍後再試一次",
  };

  // no_schedule_time 要帶出番劇名稱、分兩行（web_redesign_round5.md 項目 8）
  function noScheduleMessage(btn) {
    var title = (btn.dataset.animeTitle || "").trim();
    return {
      lines: [
        title ? "《" + title + "》" : "這部番劇",
        "該番劇不在本季新番／週期表範圍內，所以暫不支援訂閱鈴鐺",
      ],
    };
  }

  // 訂閱／退訂前的確認（文案／圖示見使用者 2026-08-26 回饋，modal.js 支援帶圖示標題
  // 的區塊、以及多區塊組在一個對話框）：
  //   - 首次點任何鈴鐺訂閱：跳一次「鈴鐺說明＋警示」二合一對話框，看過就不再跳，
  //     旗標存 bahaad.db（見 web_redesign_round2.md 階段 2-2／2-3）
  //   - 之後每次退訂：跳「警示」確認框（避免誤觸鈴鐺直接退訂＋刪掉紀錄）
  var WARNING_SECTION = {
    icon: "warning",
    heading: "請注意：",
    lines: [
      "再次按下鈴鐺後將視為取消訂閱，",
      "取消訂閱後將不再進行每週檢查，",
      "先前下載的歷史資料將全部刪除，",
      "下載的影片則不會進行刪除作業，",
      "如有需要刪除影片則請自行刪除。",
    ],
  };

  // 退訂確認：只顯示警示＋番劇名稱，不要一長串說明（使用者 2026-08-27）。
  function unsubscribeConfirm(btn) {
    var title = (btn.dataset.animeTitle || "").trim();
    return {
      icon: "warning",
      heading: "您正在取消訂閱：",
      lines: [title ? "《" + title + "》" : "這部番劇"],
    };
  }

  var SUBSCRIBE_INTRO = [
    {
      icon: "bell",
      heading: "初次使用訂閱公告說明：",
      lines: [
        "訂閱後將於每週固定時間進行自動檢查，",
        "若有新的集數將會自動下載。",
      ],
    },
    WARNING_SECTION,
  ];

  function handleClick(btn) {
    if (btn.disabled) {
      return;
    }
    var wasSubscribed = btn.dataset.subscribed === "true";

    if (wasSubscribed) {
      showConfirm(unsubscribeConfirm(btn), function () { doToggle(btn); });
      return;
    }

    var shell = document.getElementById("shell");
    var introSeen = shell && shell.dataset.subscribeIntroSeen === "true";
    if (!introSeen) {
      showConfirm(SUBSCRIBE_INTRO, function () {
        if (shell) {
          shell.dataset.subscribeIntroSeen = "true";
        }
        fetch("/ui/subscribe-intro-seen", { method: "POST" }).catch(function () {});
        doToggle(btn);
      });
      return;
    }

    doToggle(btn);
  }

  function doToggle(btn) {
    var videoSn = btn.dataset.videoSn;
    var refSn = btn.dataset.refSn;
    var wasSubscribed = btn.dataset.subscribed === "true";
    // ref_sn 還沒解析成 video_sn 的卡片鈴鐺（冷快取時一次 render 只解析前 12 個）：
    // 按下去先打 /browse/subscribe/ref/<ref_sn> 即時解析再訂閱（web/browse.py）。
    var url;
    if (!videoSn && refSn) {
      url = "/browse/subscribe/ref/" + refSn;
    } else {
      url = "/browse/" + (wasSubscribed ? "unsubscribe" : "subscribe") + "/" + videoSn;
    }

    btn.disabled = true;
    fetch(url, { method: "POST" })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("request failed");
        }
        return response.json();
      })
      .then(function (data) {
        // ref 解析成功 → 把這顆（跟頁面上同 ref 的）鈴鐺升級成正式 video_sn 的鈴鐺
        if (data.video_sn && !btn.dataset.videoSn) {
          document
            .querySelectorAll('.bell-btn[data-ref-sn="' + refSn + '"]')
            .forEach(function (b) {
              b.dataset.videoSn = data.video_sn;
              b.removeAttribute("data-ref-sn");
            });
        }
        syncButtonsForSn(btn.dataset.videoSn || videoSn, data.subscribed);
        if (wasSubscribed && !data.subscribed) {
          if (btn.dataset.removeCardOnUnsub === "true") {
            var card = btn.closest(".anime-card");
            if (card) {
              card.remove();
            }
          }
          if (data.cleaned_records) {
            showToast("已退訂，順便清掉 " + data.cleaned_records + " 筆相關的資料庫紀錄");
          }
        }
        // 訂閱被拒絕（不是取消訂閱的正常結果）：查不到週期表時段之類的原因，
        // 見 web/browse.py subscribe() 的 reason 欄位
        if (!wasSubscribed && !data.subscribed && data.reason) {
          if (data.reason === "no_schedule_time") {
            showAlert(noScheduleMessage(btn));
          } else {
            showAlert(REASON_MESSAGES[data.reason] || "訂閱失敗，請稍後再試一次");
          }
        }
      })
      .catch(function () {
        showAlert("訂閱狀態更新失敗，請稍後再試一次");
      })
      .finally(function () {
        btn.disabled = false;
      });
  }

  // document 層級事件委派（不是逐顆綁）——「最新上架」下滑無限捲動 append 進來的
  // 卡片鈴鐺也要能運作，逐顆綁只對第一次 render 的按鈕有效。
  document.addEventListener("click", function (event) {
    var btn = event.target.closest(".bell-btn[data-video-sn]");
    if (!btn) {
      return;
    }
    // 鈴鐺會疊在卡片封面連結上面／緊鄰番劇詳細頁連結旁邊，點擊不能觸發外層的導頁行為
    event.preventDefault();
    event.stopPropagation();
    handleClick(btn);
  });
})();
