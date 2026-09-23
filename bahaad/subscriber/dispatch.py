"""番劇更新／公告／完結／每日新番通知時通知訂閱者。規格見「真正的推播發送」筆記。

封裝「查追蹤者→渲染範本→呼叫中繼發送→寫歷史」整套流程；`scheduler/main_loop.py`／
`gossip_watch.py`／`completion_watch.py` 因此只需要多一個建構子參數
（`subscriber_notifier`），不用塞好幾個零散依賴進去。

範本渲染直接重用 `bahaad/notify/render.py` 的純函式：Telegram 用
`escape_html=True`、Discord 先轉 markdown 不跳脫，跟現有 `notify/dispatch.py`
（給擁有者自己的管理通知用）完全同一套規則，這裡不重新發明一套。

範本來源是 `store/subscribers.py` 的 `subscriber_notify_templates` 表（見
`subscriber/categories.py`）。`notify_episode()`／`notify_gossip()`／
`notify_anime_completed()` 都是「已知是哪個 sn」的單一番劇事件，共用
`_dispatch()`；`notify_daily_digest()` 形狀不同（要對每個訂閱者各自算出他追蹤的
番劇裡有哪些在今天的清單），另外處理。

**新番快訊**（newanime_added/time_changed/removed）目前沒有接：那幾個類別用的是
「虛構 sn」（新番快訊季度追蹤，見 `bahaad/newanime/`），訂閱者現在完全沒有管道能
追蹤一個還沒上架的新番項目（`newanime_detail.html` 只有擁有者自己的「追蹤」鈕，
沒有 `_subscriber_bell.html`）——沒有訂閱關係可以篩，所以沒有東西可以照 sn 分派。
要嘛之後幫新番快訊頁面也加一顆訂閱者版的追蹤鈕，要嘛乾脆做成「廣播給所有訂閱者」，
是設計決定不是這裡漏做，先不接。
"""

from __future__ import annotations

import logging
from datetime import date

from bahaad.notify.render import render_message, to_discord_markdown
from bahaad.subscriber import relay_client
from bahaad.subscriber.categories import DEFAULT_TEMPLATES

logger = logging.getLogger(__name__)

EPISODE_UPDATE_CATEGORY = "episode_update"
DAILY_DIGEST_CATEGORY = "daily_digest"
ANIME_COMPLETED_CATEGORY = "anime_completed"
# 訂閱通知範本沒有「官方公告：其他」（使用者 2026-09-12 定案的十個類別裡沒有這項）——
# 「其他」公告太籠統、不確定訂閱者會不會想收到，這幾種才有對應範本可以送。
GOSSIP_CATEGORIES = frozenset({"gossip_pause", "gossip_delay", "gossip_extra_episode", "gossip_takedown"})
# 保留舊名稱給還在用它的呼叫端（沿用既有匯入路徑）
DEFAULT_TEMPLATE = DEFAULT_TEMPLATES[EPISODE_UPDATE_CATEGORY]

# 「發送測試訊息」鈕用的固定文字（使用者 2026-09-23）——不是訂閱者可自訂的範本，
# 不進 subscriber_notify_templates 表，直接寫死在這裡。`@verify_code@` 是
# `subscriber/verify.py` 現場產生的一次性驗證碼，訂閱者要把這裡看到的碼貼回
# 網站才算真的確認收到（不是只看中繼 API 回應「送出成功」）。
TEST_MESSAGE_TEMPLATE = (
    "[BahaAD 測試通知]\n"
    "您已經成功接收到我們的通知\n"
    "未來可在網站上更改您的設定\n"
    "您設定的網站：\n"
    "@site_url@\n"
    "您的驗證碼：\n"
    "@verify_code@"
)


def _mention_for(identity: dict) -> str:
    """`@subscriber_mention@` 的實際內容——依管道決定要怎麼標註這個訂閱者自己。
    Discord 的 `<@id>` 是平台語法，無論是否同一伺服器都能正確渲染成提及；Telegram
    沒有等價的「用純數字 id 提及」語法，改用 `tg://user?id=` 深連結掛上顯示名稱
    （對方已經對官方 Bot 傳過 `/start`，這個 id 一定看得到）。沒有顯示名稱時退回
    「您」，不留下奇怪的空連結文字。"""
    if identity["channel"] == "discord":
        return f"<@{identity['external_id']}>"
    name = identity.get("display_name") or "您"
    return f'<a href="tg://user?id={identity["external_id"]}">{name}</a>'


def _render_for(identity: dict, template: str, context: dict) -> str:
    mention = _mention_for(identity)
    if identity["channel"] == "telegram":
        # `subscriber_mention` 本身就是要送出的 HTML（`<a href="tg://...">`）——不能
        # 跟其他一般文字一起走 escape_html=True，會被跳脫成沒作用的死標籤。先照常
        # 渲染其他 token，再單獨換上這個不跳脫的值。
        message = render_message(template, escape_html=True, **context)
        return message.replace("@subscriber_mention@", mention)
    return render_message(to_discord_markdown(template), subscriber_mention=mention, **context)


class SubscriberNotifier:
    def __init__(
        self, subscriber_store, subscriber_relay_store, http, relay_base_url, settings, anime_cache=None,
    ) -> None:
        self._subscriber_store = subscriber_store
        self._subscriber_relay_store = subscriber_relay_store
        self._http = http
        self._relay_base_url = relay_base_url
        self._settings = settings
        # 更新通知的番劇封面圖片用（使用者 2026-09-12）——None 時這項功能自動停用
        # （沒有快取可查），不報錯。
        self._anime_cache = anime_cache

    def _relay_credentials(self):
        credentials = self._subscriber_relay_store.get_credentials()
        if credentials is None:
            logger.warning("有訂閱者要收通知，但這台安裝實例尚未完成中繼綁定，無法代發")
        return credentials

    def _anime_cover_url(self, sn: int) -> str | None:
        """番劇封面的原始外部網址（不是 `/cache/img/<hash>` 那個只有本機瀏覽器看得到
        的本地代理路徑）——Telegram/Discord 的伺服器要能直接從公開網路抓到這個網址，
        見「真正的推播發送」筆記的圖片附加小節。查不到（還沒快取過這部番的詳細頁）
        就回 `None`，呼叫端自然跳過附圖，不特別報錯。"""
        if self._anime_cache is None:
            return None
        try:
            # `get_detail_with_sibling_fallback`：這個 sn（首集）自己的詳細頁可能從沒
            # 被瀏覽過而沒有快取，借用同一部番劇其他有快取的集數（使用者 2026-09-17
            # 回報：部分通知只有集數封面、沒有番劇封面，根因就是裸 `get_detail()`
            # 快取未命中直接回 None，沒有這層 fallback）。
            hit = self._anime_cache.get_detail_with_sibling_fallback(sn)
        except Exception:  # noqa: BLE001
            logger.debug("查詢番劇封面失敗（sn=%s）", sn, exc_info=True)
            return None
        return hit[0].cover_url if hit is not None else None

    def _send_to(
        self, identity: dict, installation_id: str, secret: str, sn: int | None, message: str,
        image_urls: list[str] | None = None,
    ) -> None:
        result = relay_client.send_notification(
            self._http, self._relay_base_url, installation_id, secret,
            identity["channel"], identity["external_id"], message, image_urls=image_urls,
        )
        ok = bool(result and result.get("ok"))
        error = None if ok else ((result or {}).get("info") or "連不上中繼伺服器")
        self._subscriber_store.log_notify_history(identity["id"], sn, message, ok, error)

    def _recipients_for(self, primary_identity: dict) -> list[dict]:
        """使用者 2026-09-16：訂閱者可以把 Discord／Telegram 帳號連結成同一個人，
        依他自己在「我的訂閱」設定的通知管道偏好，展開成真正要送的每一個
        (channel, external_id) 收件對象——沒連結過任何帳號時就只有自己這一個。
        `identities_following()`／`all_identities_with_follows()` 回傳的一律是
        「主要」身分（見 `SubscribersStore.resolve_session()`），這裡才是實際
        決定訊息送去哪裡的地方。"""
        primary_id = primary_identity["id"]
        enabled = self._subscriber_store.get_notify_channels(primary_id)
        linked = self._subscriber_store.linked_identities_for(primary_id) or [primary_identity]
        return [identity for identity in linked if identity["channel"] in enabled]

    def _dispatch(
        self, sn: int, category: str, context: dict, image_urls: list[str] | None = None,
        template: str | None = None,
    ) -> None:
        """給「已知是哪個 sn」的單一番劇事件共用——查追蹤者、渲染、代發、寫歷史。
        `template` 讓呼叫端能重用自己已經查過一次的範本文字（`notify_episode()`
        需要先看過範本內容才能決定要不要附圖），不給就自己查一次。"""
        followers = self._subscriber_store.identities_following(sn)
        if not followers:
            return
        credentials = self._relay_credentials()
        if credentials is None:
            return
        installation_id, secret = credentials
        template = template if template is not None else self._subscriber_store.get_notify_template(category)
        for identity in followers:
            for recipient in self._recipients_for(identity):
                message = _render_for(recipient, template, context)
                self._send_to(recipient, installation_id, secret, sn, message, image_urls=image_urls)

    def notify_episode(self, sn: int, anime_title: str, episode_label: str, episode_cover_url: str | None = None) -> None:
        """`sn` 必須是已經正規化過的首集 sn（跟 `subscriber_follows.sn` 同一套），
        呼叫端負責正規化（見 `scheduler/main_loop.py` 用 `resolve_first_ep_sn()`）。
        `episode_cover_url`：剛下載的這一集自己的封面（動畫瘋 video.php 每集各自
        一張，見 `gamer_client/catalog.py` 的 `EpisodeSummary.cover`），呼叫端已經
        有這筆資料就直接傳進來，這裡不用再打一次 API 查。

        圖片要不要附、附哪張——使用者 2026-09-12 定案改成「插入 token」而不是設定頁
        開關：範本文字裡有 `@episode_cover@` 就附這一集的封面，有 `@anime_cover@`
        就附整部番劇的封面，**兩個都插入時兩張一起附**（不是二選一）；兩個都沒有
        就不附圖。這兩個 token 在訊息文字裡本身換成空字串——圖片走真正的附圖 API
        （Discord embed／Telegram sendPhoto／sendMediaGroup），不是把網址原文貼進
        內文。"""
        domain = self._settings.get("public_domain", "") or ""
        anime_link = f"{domain.rstrip('/')}/anime/{sn}" if domain else ""
        template = self._subscriber_store.get_notify_template(EPISODE_UPDATE_CATEGORY)

        image_urls: list[str] = []
        if "@episode_cover@" in template and episode_cover_url:
            image_urls.append(episode_cover_url)
        if "@anime_cover@" in template:
            anime_cover_url = self._anime_cover_url(sn)
            if anime_cover_url and anime_cover_url != episode_cover_url:
                image_urls.append(anime_cover_url)

        context = {
            "anime_title": anime_title, "episode": episode_label, "anime_link": anime_link,
            "anime_cover": "", "episode_cover": "",
        }
        self._dispatch(sn, EPISODE_UPDATE_CATEGORY, context, image_urls=image_urls or None, template=template)

    def notify_gossip(
        self, sn: int, category: str, anime_title: str, announcement_text: str,
        announcement_time: str = "", postpone_time: str | None = None,
    ) -> None:
        """`category` 是 `GOSSIP_CATEGORIES` 其中之一；`gossip_other`（其他公告）
        沒有對應的訂閱通知範本，呼叫端自己先過濾，這裡防呆再擋一次不報錯。"""
        if category not in GOSSIP_CATEGORIES:
            return
        context = {
            "anime_title": anime_title,
            "announcement_text": announcement_text,
            "announcement_time": announcement_time,
        }
        if postpone_time:
            context["postpone_time"] = postpone_time
        self._dispatch(sn, category, context)

    def notify_anime_completed(self, sn: int, anime_title: str) -> None:
        self._dispatch(sn, ANIME_COMPLETED_CATEGORY, {"anime_title": anime_title})

    def send_test_message(self, identity: dict, verify_code: str) -> tuple[bool, str]:
        """「發送測試訊息」鈕用（使用者 2026-09-23：與其被動等某個真正的番劇通知
        第一次成功送出才確認 Telegram `/start` 有沒有傳過，讓訂閱者自己按一下當場
        驗證）。`identity` 直接是要送去的那個管道身分本身（不是「主要」身分），
        呼叫端負責從 `linked_identities_for()` 查出正確的收件對象，不接受任意
        identity id（見 `web/subscriber_auth.py` `notify_test()` 的檢查）。
        `verify_code`：呼叫端已經先用 `subscriber/verify.py` 的 `SubscriberVerifyCodes.
        issue()` 產生好的一次性驗證碼，這裡只負責塞進訊息內文（`@verify_code@`）
        一起送出——訂閱者要把看到的碼貼回網站，`notify_test_confirm()` 核對通過
        才真的算「確認收到」（`store/subscribers.py` 的 `mark_verified()`），不是
        單看這裡送出成功與否。

        送成功的話照樣寫進 `subscriber_notify_history`（`sn=None`），純記錄用途，
        跟是否驗證成功無關。"""
        credentials = self._relay_credentials()
        if credentials is None:
            return False, "這台安裝實例尚未完成中繼綁定，暫時無法發送測試訊息"
        installation_id, secret = credentials
        domain = self._settings.get("public_domain", "") or ""
        context = {"site_url": domain, "verify_code": verify_code}
        if identity["channel"] == "telegram":
            message = render_message(TEST_MESSAGE_TEMPLATE, escape_html=True, **context)
        else:
            message = render_message(to_discord_markdown(TEST_MESSAGE_TEMPLATE), **context)
        result = relay_client.send_notification(
            self._http, self._relay_base_url, installation_id, secret,
            identity["channel"], identity["external_id"], message,
        )
        ok = bool(result and result.get("ok"))
        error = None if ok else ((result or {}).get("info") or "連不上中繼伺服器")
        self._subscriber_store.log_notify_history(identity["id"], None, message, ok, error)
        if ok:
            return True, "測試訊息已送出，請輸入訊息裡的驗證碼確認收到"
        return False, f"測試訊息發送失敗：{error}"

    def notify_daily_digest(self, today: date, rows: list[tuple[int, str]]) -> None:
        """`rows`：今天排定更新、且**尚未套用個別訂閱者篩選**的完整清單，
        `(sn, line_text)`——`line_text` 就是擁有者那份彙整裡同一行的完整文字
        （`"- 番劇名 第N集 20:30"` 這種，見 `gossip_watch.py._digest_content()` 的
        `schedule_lines`），直接重用、不重新組一次名稱/集數/時間。這裡自己對每個
        有追蹤數量 > 0 的訂閱者，篩出「他追蹤的番劇裡今天有更新」的子集，沒有交集
        就整個跳過（不寄空彙整騷擾人）。同一天只送一次（比照擁有者那份的日期去重，
        見 `store/subscribers.py` 的 `get_last_digest_date`/`set_last_digest_date`；
        不做擁有者那邊「內容變了就重發」的機動調整重發，訂閱者這邊先簡化，見
        dispatch.py 開頭說明）。"""
        credentials = self._relay_credentials()
        if credentials is None:
            return
        installation_id, secret = credentials
        today_str = today.isoformat()

        by_sn: dict[int, list[str]] = {}
        for sn, line_text in rows:
            by_sn.setdefault(sn, []).append(line_text)

        template = self._subscriber_store.get_notify_template(DAILY_DIGEST_CATEGORY)
        for identity in self._subscriber_store.all_identities_with_follows():
            own_sns = identity["sns"] & by_sn.keys()
            if not own_sns:
                continue
            if self._subscriber_store.get_last_digest_date(identity["id"]) == today_str:
                continue  # 今天已經送過

            lines = [line for sn in sorted(own_sns) for line in by_sn[sn]]
            context = {
                "digest_date": today_str,
                "today_schedule_count": str(len(lines)),
                "today_schedule_list": "\n".join(lines),
            }
            for recipient in self._recipients_for(identity):
                message = _render_for(recipient, template, context)
                self._send_to(recipient, installation_id, secret, None, message)
            self._subscriber_store.set_last_digest_date(identity["id"], today_str)
