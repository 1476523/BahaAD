"""每日簽到 ＋ 雙倍巴幣。參考 brownsugar/baha-auto-sign（瀏覽器擴充），改成純 HTTP。

擴充是開真的網頁、覆寫頁面 JS 去觸發簽到；這裡直接打頁面 JS（`signin.js`／`signin_ad.js`）
背後的四支 API，用 BahaAD 既有的登入 session（cookie 保活、CSRF、瀏覽器指紋都現成）：

1. `GET  api.gamer.com.tw/user/v1/signin.php?action=2` 查今天簽到／雙倍獎勵狀態
2. `POST api.gamer.com.tw/user/v1/signin.php`（`action=1`）簽到
3. `POST api.gamer.com.tw/mobile_app/bahamut/v1/sign_in_ad_start.php` 開始看廣告
4. `POST api.gamer.com.tw/mobile_app/bahamut/v1/sign_in_ad_finished.php` 回報廣告看完
5. `GET  api.gamer.com.tw/common/v1/navigation.php` 讀實際巴幣（`data.user_info.gold`，導覽列
   顯示的那個數字）——簽到前後各讀一次，**用真實餘額有沒有增加來驗證簽到是否真的生效**，
   而不是只看 API 回 200／`finished=1`。

**時間**：預設 01:00，可自訂。避開 00:00～00:06（站方跨日、擴充預設也是 00:06 以後）。

**自動調整時間**（`daily_signin_auto_adjust`，使用者 2026-10-20）：簽到不跟別的動作擠在一起——
下一次保活（`CookieWarmup`）或排程任務（訂閱的更新時段）開始前 `AVOID_LEAD` 分鐘內不簽到，
順延到該事件開始後 `AVOID_GAP` 再簽。事件由呼叫端用 `busy_events(now)` 提供。**不處理重新登入**
——登入失效由既有的 `CookieWarmup`／登入橫幅負責，這裡遇到「尚未登入」只回報、不自己登入。

**重試**：簽到失敗隔 `RETRY_DELAY` 再試，每天最多 `MAX_ATTEMPTS` 次。已簽到完成（含雙倍）的
日期記在 `daily_signin_last_date`，重開 BahaAD 不會重複簽。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable

from bahaad.scheduler import signin_site
from bahaad.store.settings import SettingsStore
from bahaad.vault import Vault

logger = logging.getLogger(__name__)

_STATUS_URL = "https://api.gamer.com.tw/user/v1/signin.php?action=2"
_SIGNIN_URL = "https://api.gamer.com.tw/user/v1/signin.php"
_AD_START_URL = "https://api.gamer.com.tw/mobile_app/bahamut/v1/sign_in_ad_start.php"
_AD_FINISHED_URL = "https://api.gamer.com.tw/mobile_app/bahamut/v1/sign_in_ad_finished.php"
_NAVIGATION_URL = "https://api.gamer.com.tw/common/v1/navigation.php"
_HOME_PROFILE_URL = "https://home.gamer.com.tw/profile/index.php"
_HEADERS = {
    "Origin": "https://home.gamer.com.tw",
    "Referer": "https://home.gamer.com.tw/homeindex.php",
}

ENABLED_KEY = "daily_signin_enabled"
TIME_KEY = "daily_signin_time"
DOUBLE_KEY = "daily_signin_double"
AUTO_ADJUST_KEY = "daily_signin_auto_adjust"
LAST_DATE_KEY = "daily_signin_last_date"
LAST_RESULT_KEY = "daily_signin_last_result"
SITE_STATE_KEY = "daily_signin_site_state"  # 站方簽到頁面文字／程式雜湊，偵測改版用

DEFAULT_TIME = "01:00"
AVOID_LEAD = timedelta(minutes=3)  # 保活／任務開始前這段時間不簽到
AVOID_GAP = timedelta(minutes=1)  # 事件開始後再等這麼久才簽
RETRY_DELAY = timedelta(minutes=5)
MAX_ATTEMPTS = 3
# 簽到成功到開始領雙倍之間、開始看廣告到回報看完之間各等一下——baha-auto-sign
# 1.2.0「避免自動操作過於密集導致無法正常領取雙倍巴幣」。
_STEP_DELAY_SECONDS = 3.0
_AD_FINISH_DELAY_SECONDS = 5.0
_AD_FINISH_ATTEMPTS = 3
_POLL_SECONDS = 30.0
# 站方入帳可能晚一兩秒——預期會增加卻沒增加時，再讀幾次才下「沒入帳」的結論。
_GOLD_VERIFY_ATTEMPTS = 3
_GOLD_VERIFY_DELAY_SECONDS = 2.0
NOTIFY_CATEGORY = "daily_signin"

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def parse_time(text: Any) -> tuple[int, int] | None:
    """`"01:00"` → `(1, 0)`；格式不對回 `None`。"""
    match = _TIME_RE.match(str(text or "").strip())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


class SigninResult:
    """`status`：`ok`（簽到成功／今天本來就簽了，且雙倍已處理）、`login_lost`（站方回
    NO_LOGIN）、`error`。`double`：`done`／`already`／`skipped`（沒開雙倍）／`failed`。

    `gold_before`／`gold_after`：**實際讀到**的巴幣餘額（讀不到為 `None`）。`verified`：
    這次有動作（剛簽到或剛領雙倍）時，餘額確實增加＝`True`、讀了幾次都沒增加＝`False`
    （API 說成功但沒入帳）；沒動作或餘額讀不到＝`None`（無從驗證）。"""

    def __init__(self, status: str, message: str = "", *, signed_now: bool = False,
                 double: str = "skipped", days: Any = None,
                 gold_before: int | None = None, gold_after: int | None = None,
                 verified: bool | None = None, cell: int | None = None,
                 reward: str = "", reward_coins: int | None = None,
                 notices: list[str] | None = None) -> None:
        self.status = status
        self.message = message
        self.signed_now = signed_now
        self.double = double
        self.days = days
        self.gold_before = gold_before
        self.gold_after = gold_after
        self.verified = verified
        # 今天簽到格子的獎勵（依站方範本規則算出）：第幾格、名稱（「100 巴幣」／「勳章」）、預期巴幣
        self.cell = cell
        self.reward = reward
        self.reward_coins = reward_coins
        # 站方說明文字／程式有改動、或簽到時站方附帶的訊息（bonus card），原樣帶進通知
        self.notices = list(notices or [])

    @property
    def complete(self) -> bool:
        return self.status == "ok" and self.double != "failed"

    @property
    def gold_delta(self) -> int | None:
        if self.gold_before is None or self.gold_after is None:
            return None
        return self.gold_after - self.gold_before

    @property
    def acted(self) -> bool:
        """這次是不是真的做了什麼事（剛簽到、或剛領到雙倍）——「今天本來就弄好了」不算。"""
        return self.signed_now or self.double == "done"

    def __repr__(self) -> str:
        return (f"SigninResult({self.status!r}, {self.message!r}, double={self.double!r}, "
                f"gold={self.gold_before}->{self.gold_after}, verified={self.verified})")


class DailySignin:
    def __init__(
        self,
        http,
        vault: Vault,
        settings: SettingsStore,
        *,
        busy_events: Callable[[datetime], list[datetime]] | None = None,
        on_login_checked: Callable[[bool], None] | None = None,
        notifier: Callable[[SigninResult, bool], None] | None = None,
        now: Callable[[], datetime] = datetime.now,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http
        self._vault = vault
        self._settings = settings
        # `busy_events(now)`：接下來會開始的保活／排程任務時間點（自動調整用）
        self._busy_events = busy_events
        self._on_login_checked = on_login_checked
        # `notifier(result, final)`：`final` ＝ 這是今天最後一次嘗試（成功，或已用完重試次數）。
        self._notifier = notifier
        self._now = now
        self._sleep = sleep
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # 排程狀態（只在背景執行緒讀寫）
        self._deferred_for: set[datetime] = set()
        self._run_lock = threading.Lock()  # 排程與手動「立即簽到」不能同時送出
        self._attempts_date: str | None = None
        self._attempts = 0
        self._next_retry: datetime | None = None

    # ---- 四支 API --------------------------------------------------------------------

    def _json(self, response) -> dict:
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"回應不是 JSON（HTTP {getattr(response, 'status_code', '?')}）") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("回應格式不符預期")
        return payload

    @staticmethod
    def _error_of(payload: dict) -> dict | None:
        error = payload.get("error")
        return error if isinstance(error, dict) else None

    def run_once(self, *, double: bool | None = None) -> SigninResult:
        """跑一次簽到（含雙倍）。不拋例外：站方錯誤、網路錯誤都轉成 `SigninResult`。"""
        if double is None:
            double = bool(self._settings.get(DOUBLE_KEY, True))
        try:
            return self._run(double)
        except Exception as exc:  # noqa: BLE001 - 背景任務失敗不能讓執行緒掛掉
            logger.warning("每日簽到發生例外：%s", exc, exc_info=True)
            return SigninResult("error", str(exc))

    def fetch_gold(self) -> int | None:
        """讀實際巴幣餘額；讀不到（網路、未登入、格式變了）回 `None`，不拋例外。"""
        try:
            payload = self._json(self._http.get(_NAVIGATION_URL, headers=dict(_HEADERS)))
            if self._error_of(payload) is not None:
                return None
            return int(payload["data"]["user_info"]["gold"])
        except Exception:  # noqa: BLE001
            logger.debug("讀取巴幣餘額失敗", exc_info=True)
            return None

    def _visit_home(self) -> SigninResult | None:
        """簽到前先像瀏覽器一樣走進巴哈個人首頁（使用者 2026-10-21：簽到要在 home.gamer.com.tw 登入後才有效，
        動畫瘋的登入不夠）。有登入會被導向帶 `owner=` 的個人頁；被導去登入頁就是沒登入。
        其他情況（網路錯誤、站方改版）一律放行，不因為這一步擋住簽到。"""
        try:
            response = self._http.get(_HOME_PROFILE_URL, headers={"Referer": _HEADERS["Referer"]})
        except Exception:  # noqa: BLE001
            logger.debug("簽到前造訪個人首頁失敗", exc_info=True)
            return None
        final = str(getattr(response, "url", "") or "")
        if "owner=" in final:
            logger.info("每日簽到：個人首頁已導向帳號頁（登入有效）")
            return None
        if "login" in final.lower():
            self._report_login(False)
            return SigninResult("login_lost", "巴哈個人首頁要求重新登入（登入狀態失效，請重新登入）")
        logger.info("每日簽到：個人首頁沒有導向帳號頁（落在 %s），照常繼續", final.split("gamer.com.tw")[-1][:60] or "?")
        return None

    def _run(self, double: bool) -> SigninResult:
        lost = self._visit_home()
        if lost is not None:
            return lost
        status = self._json(self._http.get(_STATUS_URL, headers=dict(_HEADERS)))
        error = self._error_of(status)
        if error is not None:
            return self._failure(error, "查詢簽到狀態")
        data = status.get("data") or {}
        notices, rules = self._check_site()
        gold_before = self.fetch_gold()
        signed_now = False
        site_reward = None
        days = data.get("days")
        weeks = data.get("totalWeeks")

        if int(data.get("signin", 0)) == 0:
            result = self._json(
                self._http.post(_SIGNIN_URL, data={"action": "1"}, headers=dict(_HEADERS))
            )
            error = self._error_of(result)
            if error is not None:
                return self._failure(error, "送出簽到")
            signed_now = True
            days = (result.get("data") or {}).get("days", days)
            weeks = (result.get("data") or {}).get("totalWeeks", weeks)
            info = (result.get("data") or {}).get("dialogInfo") or {}
            bonus = " ".join(str(info[k]) for k in ("title", "content") if info.get(k)).strip()
            site_reward = (signin_site.reward_from_message(str(info.get("title") or ""))
                           or signin_site.reward_from_message(bonus))
            data = {**data, "finishedAd": 0}
            logger.info("每日簽到成功（連續 %s 天）", days)
        self._report_login(True)

        outcome = "skipped"
        if double:
            if int(data.get("finishedAd", 0)) == 1:
                outcome = "already"
            else:
                if signed_now:
                    self._sleep(_STEP_DELAY_SECONDS)
                outcome = self._claim_double()

        reward = signin_site.reward_for(days, weeks, rules) if signed_now else None
        if reward is not None and site_reward:
            # 站方簽到訊息裡的獎勵優先（例如第 28 格是「500 巴幣 & 勳章」，範本規則只寫「勳章」）
            coins = re.match(r"(\d+)\s*巴幣", site_reward)
            reward = (reward[0], site_reward, int(coins.group(1)) if coins else reward[2])
        gold_after, verified = self._verify_gold(
            gold_before,
            expect_increase=signed_now or outcome == "done",
            # 今天的格子不是巴幣（例如「勳章」）：餘額沒增加是正常的，不能算「沒入帳」
            lenient=reward is not None and reward[2] == 0,
        )
        message = {
            "skipped": "已簽到" if signed_now else "今天已簽到",
            "already": "已簽到，雙倍巴幣今天已領過",
            "done": "已簽到並領取雙倍巴幣" if signed_now else "已領取雙倍巴幣",
            "failed": "已簽到，但雙倍巴幣領取失敗",
        }[outcome]
        if verified is False:
            message += "；但實際巴幣餘額沒有增加（可能沒入帳）"
            logger.warning("每日簽到：%s（巴幣 %s → %s）", message, gold_before, gold_after)
        return SigninResult(
            "ok", message, signed_now=signed_now, double=outcome, days=days,
            gold_before=gold_before, gold_after=gold_after, verified=verified,
            cell=reward[0] if reward else None, reward=reward[1] if reward else "",
            reward_coins=reward[2] if reward else None, notices=notices,
        )

    def _verify_gold(self, before: int | None, *, expect_increase: bool,
                     lenient: bool = False) -> tuple[int | None, bool | None]:
        """回傳 `(簽到後餘額, 是否驗證到增加)`。沒預期要增加就只讀一次、`verified=None`。"""
        after = self.fetch_gold()
        if not expect_increase or before is None or after is None:
            return after, None
        for _ in range(_GOLD_VERIFY_ATTEMPTS - 1):
            if after > before:
                break
            self._sleep(_GOLD_VERIFY_DELAY_SECONDS)
            latest = self.fetch_gold()
            if latest is None:
                break
            after = latest
        if after > before:
            return after, True
        return after, None if lenient else False

    def _check_site(self) -> tuple[list[str], dict]:
        """抓站方簽到頁面的範本／程式，回傳 `(變更說明, 獎勵規則)`。任何失敗都靜默退回後備規則。"""
        rules = signin_site.default_rules()
        try:
            fetched: dict[str, str] = {}
            for name, url in (("signin.js", signin_site.SIGNIN_JS_URL),
                              ("signin_ad.js", signin_site.SIGNIN_AD_JS_URL),
                              ("template", signin_site.TEMPLATE_URL)):
                body = getattr(self._http.get(url, headers={"Referer": _HEADERS["Referer"]}), "text", "")
                if isinstance(body, str) and body:
                    fetched[name] = body
            if "template" in fetched:
                rules = signin_site.parse_template(fetched["template"])
            if len(fetched) < 3:  # 有抓不到的就不比對、也不覆蓋舊基準，避免誤報
                return [], rules
            new_state = {
                "hashes": {k: signin_site.fingerprint(v) for k, v in fetched.items()},
                "texts": {k: rules[k] for k in ("notice", "links", "badge_hint")}
                | {"awards": {str(k): v for k, v in rules["awards"].items()}},
            }
            old_state = self._settings.get(SITE_STATE_KEY)
            notes = signin_site.diff_state(old_state, new_state)
            if old_state != new_state:
                self._settings.update({SITE_STATE_KEY: new_state})
            for note in notes:
                logger.info("每日簽到：%s", note)
            return notes, rules
        except Exception:  # noqa: BLE001 - 純輔助，失敗不影響簽到
            logger.debug("檢查站方簽到頁面失敗", exc_info=True)
            return [], rules

    def _claim_double(self) -> str:
        start = self._json(self._http.post(_AD_START_URL, headers=dict(_HEADERS)))
        if self._error_of(start) is not None:
            logger.warning("雙倍巴幣：開始看廣告失敗：%s", self._error_of(start))
            return "failed"
        if int((start.get("data") or {}).get("finished", 0)) == 1:
            return "already"
        for attempt in range(1, _AD_FINISH_ATTEMPTS + 1):
            self._sleep(_AD_FINISH_DELAY_SECONDS)
            done = self._json(self._http.post(_AD_FINISHED_URL, headers=dict(_HEADERS)))
            if self._error_of(done) is not None:
                logger.warning("雙倍巴幣：回報看完失敗（第 %d 次）：%s", attempt, self._error_of(done))
                continue
            if int((done.get("data") or {}).get("finished", 0)) == 1:
                return "done"
        return "failed"

    def _failure(self, error: dict, step: str = "") -> SigninResult:
        # 站方的 message 有時帶 HTML（例如簽到按鈕的圖示標籤），去掉標籤再顯示
        raw_message = re.sub(r"<[^>]+>", "", str(error.get("message") or "")).strip()
        message = raw_message or str(error.get("status") or error)
        code, status = error.get("code"), error.get("status")
        if status == "NO_LOGIN" or code == 401:
            self._report_login(False)
            return SigninResult("login_lost", "尚未登入動畫瘋／巴哈（登入狀態失效，請重新登入）")
        # 使用者 2026-10-21：簽到失敗只看到「驗證碼錯誤」，看不出是哪一步、站方回了什麼——把步驟與站方代碼
        # 一併寫進結果與日誌（這些是站方回的錯誤欄位，不含帳號或 Cookie）。
        logger.warning("每日簽到：站方在「%s」回報錯誤（代碼 %s／%s）：%s", step or "?", code, status, message)
        suffix = f"（{step}，站方代碼 {code}／{status}）" if step else ""
        return SigninResult("error", f"站方回報：{message}{suffix}")

    def _report_login(self, valid: bool) -> None:
        if self._on_login_checked is not None:
            try:
                self._on_login_checked(valid)
            except Exception:  # noqa: BLE001
                logger.debug("daily_signin on_login_checked 回呼發生例外", exc_info=True)

    # ---- 排程 ------------------------------------------------------------------------

    def _target_for(self, day: datetime) -> datetime | None:
        parsed = parse_time(self._settings.get(TIME_KEY, DEFAULT_TIME)) or parse_time(DEFAULT_TIME)
        if parsed is None:
            return None
        return day.replace(hour=parsed[0], minute=parsed[1], second=0, microsecond=0)

    def _record(self, result: SigninResult, date: str) -> None:
        stamp = self._now().strftime("%Y-%m-%d %H:%M")
        gold = ""
        if result.gold_before is not None and result.gold_after is not None:
            gold = f"（巴幣 {result.gold_before:,} → {result.gold_after:,}）"
        update: dict[str, Any] = {LAST_RESULT_KEY: f"{stamp} {result.status}：{result.message}{gold}"}
        if result.complete:
            update[LAST_DATE_KEY] = date
        self._settings.update(update)

    def _blocked_until(self, now: datetime) -> datetime | None:
        """`now` 落在某個保活／任務開始前 `AVOID_LEAD`（到開始後 `AVOID_GAP`）內，回傳可以簽的時間；否則 `None`。"""
        if self._busy_events is None:
            return None
        try:
            events = self._busy_events(now)
        except Exception:  # noqa: BLE001 - 取不到事件就照原時間簽
            logger.debug("每日簽到：取得保活／任務時間失敗", exc_info=True)
            return None
        resume: datetime | None = None
        for event in events:
            if event - AVOID_LEAD <= now < event + AVOID_GAP:
                end = event + AVOID_GAP
                resume = end if resume is None else max(resume, end)
                if event not in self._deferred_for:
                    self._deferred_for.add(event)
                    logger.info("每日簽到：%s 有保活／排程任務，避開前 3 分鐘，順延到 %s 後簽到",
                                event.strftime("%H:%M"), event.strftime("%H:%M"))
        return resume

    def tick(self) -> float:
        """做一次排程判斷，回傳建議睡多久（秒）。獨立成函式方便測試，不含 sleep。"""
        if not bool(self._settings.get(ENABLED_KEY, False)):
            return _POLL_SECONDS
        if not self._vault.get_status().get("configured"):
            return _POLL_SECONDS

        now = self._now()
        today = now.strftime("%Y-%m-%d")
        if self._attempts_date != today:
            self._attempts_date, self._attempts, self._next_retry = today, 0, None
        if self._settings.get(LAST_DATE_KEY) == today:
            return _POLL_SECONDS

        target = self._target_for(now)
        if target is None:
            return _POLL_SECONDS

        if now < target:
            return min(_POLL_SECONDS, max(1.0, (target - now).total_seconds()))
        if self._attempts >= MAX_ATTEMPTS:
            return _POLL_SECONDS
        if self._next_retry is not None and now < self._next_retry:
            return min(_POLL_SECONDS, max(1.0, (self._next_retry - now).total_seconds()))

        # 自動調整：保活／排程任務開始前 AVOID_LEAD 內不簽，順延到事件後
        if bool(self._settings.get(AUTO_ADJUST_KEY, True)):
            resume = self._blocked_until(now)
            if resume is not None:
                return min(_POLL_SECONDS, max(1.0, (resume - now).total_seconds()))

        self._attempts += 1
        with self._run_lock:
            result = self.run_once()
        logger.info("每日簽到結果（第 %d 次）：%s", self._attempts, result)
        self._record(result, today)
        final = result.complete or self._attempts >= MAX_ATTEMPTS
        if not result.complete:
            self._next_retry = self._now() + RETRY_DELAY
        self._notify(result, final)
        return _POLL_SECONDS

    def run_now(self) -> SigninResult | None:
        """設定頁「立即簽到」（使用者 2026-10-21：簽到失敗時想馬上重試、看到站方回了什麼，不用等隔天）。
        不計入每日重試次數；結果照樣寫進「上次結果」與日誌，並依通知設定發通知。已有簽到正在進行就回 `None`。"""
        if not self._run_lock.acquire(blocking=False):
            return None
        try:
            result = self.run_once()
            logger.info("每日簽到結果（手動）：%s", result)
            self._record(result, self._now().strftime("%Y-%m-%d"))
            self._notify(result, True)
            return result
        finally:
            self._run_lock.release()

    def _notify(self, result: SigninResult, final: bool) -> None:
        """有實際動作（剛簽到／剛領雙倍）或今天最後一次仍失敗才通知；「今天本來就弄好了」
        （例如重開 BahaAD 補跑）不吵人。登入失效不在這裡通知——`GamerLoginAlert` 會發。"""
        if self._notifier is None or result.status == "login_lost":
            return
        if not (result.acted or result.notices or (final and not result.complete)):
            return
        try:
            self._notifier(result, final)
        except Exception:  # noqa: BLE001 - 通知失敗不能影響簽到本身
            logger.warning("每日簽到通知發送失敗", exc_info=True)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="daily-signin")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                wait = self.tick()
            except Exception:
                logger.exception("daily_signin 這一輪發生未預期的例外")
                wait = _POLL_SECONDS
            self._stop_event.wait(wait)


def notification_context(result: SigninResult) -> dict[str, str]:
    """`daily_signin` 通知範本的 token 值。巴幣一律是**實際讀到**的餘額，不是預估。"""

    def fmt(value: int | None) -> str:
        return "未知" if value is None else f"{value:,}"

    if not result.complete:
        status = "簽到失敗"
    elif result.signed_now or result.double == "done":
        status = "簽到成功"
    else:
        status = "今天已簽到"
    delta = result.gold_delta
    delta_text = "無法比對" if delta is None else f"{delta:+,}"
    if result.verified is True:
        verify = f"✅ 已確認入帳（實際餘額增加 {delta:,}）"
    elif result.verified is False:
        verify = "⚠️ 警告：API 回報成功，但實際巴幣餘額沒有增加，可能沒有真的簽到成功"
    elif result.status != "ok":
        verify = f"❌ {result.message}"
    elif result.gold_before is None or result.gold_after is None:
        verify = "（讀不到巴幣餘額，無法驗證）"
    else:
        verify = "（這次沒有新的簽到動作，不需驗證）"
    double_text = {
        "done": "已領取", "already": "今天已領過", "skipped": "未啟用", "failed": "領取失敗",
    }.get(result.double, result.double)
    if result.status != "ok":
        double_text = "未執行"
    if result.reward:
        reward_text = f"{result.reward} (第 {result.cell} 格)"
    elif result.status != "ok":
        reward_text = "（未簽到）"
    else:
        reward_text = "（今天稍早已簽到，未重新取得）" if not result.signed_now else "未知"
    if delta is not None:
        gold_today = f"{delta:+,}" if delta else "0"
    elif result.reward_coins == 0:
        gold_today = "0"  # 今天的格子不是巴幣（勳章）
    else:
        gold_today = "未知"
    notice_text = "".join(f"\n{n}" for n in result.notices)
    return {
        "signin_reward": reward_text,
        "gold_today": gold_today,
        "signin_notice": notice_text,
        "signin_status": status,
        "gold_before": fmt(result.gold_before),
        "gold_after": fmt(result.gold_after),
        "gold_delta": delta_text,
        "signin_double": double_text,
        "signin_verify": verify,
        "signin_days": "未知" if result.days is None else str(result.days),
        "signin_badge": badge_hint(result.days),
    }


BADGE_MILESTONES = (1000, 2000, 3000)  # 累計簽到滿這些天數，站方會再給額外的永久勳章


def badge_hint(days: int | None) -> str:
    """通知下方的永久勳章提示（前面帶換行，沒有可提示的就回空字串，讓範本不多出空行）。
    使用者 2026-10-22：累計簽到滿 1000、2000、3000 天有額外永久勳章，要提示還差幾天。"""
    try:
        total = int(days)
    except (TypeError, ValueError):
        return ""
    if total in BADGE_MILESTONES:
        return f"\n🎖 今天累計簽到滿 {total:,} 天，可獲得額外永久勳章"
    for milestone in BADGE_MILESTONES:
        if total < milestone:
            return f"\n再簽到 {milestone - total:,} 天即可獲取額外永久勳章"
    return ""


def make_notifier(notify_store, settings, http) -> Callable[[SigninResult, bool], None]:
    """給 `DailySignin(notifier=...)` 用：走既有的 `notify.dispatch.send_notification`
    （類別開關、Telegram／Discord 憑證、歷史紀錄都共用）。"""

    def _notify(result: SigninResult, final: bool) -> None:
        from bahaad.notify.dispatch import send_notification

        send_notification(notify_store, settings, http, NOTIFY_CATEGORY, **notification_context(result))

    return _notify
