// 訂閱者「訂閱通知」按鈕（見 _subscriber_bell.html）。跟 subscribe.js（擁有者的
// 週期表自動下載訂閱）完全獨立，靠不同的 class/data 屬性區分，不會互相攔截事件。
document.addEventListener("click", function (event) {
  var btn = event.target.closest(".subscriber-follow-btn[data-subscriber-follow-sn]");
  if (!btn) return;

  var sn = btn.dataset.subscriberFollowSn;
  var wasFollowed = btn.dataset.followed === "true";
  var url = "/subscriber/" + (wasFollowed ? "unfollow" : "follow") + "/" + sn;

  btn.disabled = true;
  fetch(url, { method: "POST" })
    .then(function (res) { return res.json(); })
    .then(function (data) {
      if (!data.ok) {
        if (typeof showAlert === "function") showAlert(data.info || "操作失敗");
        return;
      }
      var nowFollowed = !wasFollowed;
      btn.dataset.followed = nowFollowed ? "true" : "false";
      btn.classList.toggle("subscribed", nowFollowed);
      btn.title = nowFollowed ? "取消收藏" : "收藏（番劇更新時收到 Discord/Telegram 通知）";
      // 「我的收藏」列表（data-remove-card-on-unfollow="true"）取消收藏後直接把整張
      // 卡片移除，比照 subscribe.js 對 data-remove-card-on-unsub 的做法；番劇列表頁
      // （擁有者排程清單）的卡片不會有這個屬性，取消收藏不影響該頁卡片顯示。
      if (!nowFollowed && btn.dataset.removeCardOnUnfollow === "true") {
        var card = btn.closest(".anime-card");
        if (card) card.remove();
      }
    })
    .catch(function () {
      if (typeof showAlert === "function") showAlert("操作失敗，請稍後再試一次");
    })
    .finally(function () { btn.disabled = false; });
});
