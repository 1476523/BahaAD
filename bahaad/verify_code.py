"""高風險網頁動作（首次設定、忘記密碼救援）的「本機驗證碼」關卡。

**威脅模型**（使用者 2026-09-06 回報）：公開模式／透過 Cloudflare Tunnel 之類的反向代理
曝露在外時，`/forgot-password/reset`／`/reset-everything`／`/setup` 原本靠「只接受
127.0.0.1」擋外部裝置——但反向代理讓 Flask 看到的 `request.remote_addr`永遠是代理本機的
位址，這層保護對「透過代理連進來的遠端訪客」完全沒用。首次設定表單還會先洩漏下載目錄
路徑（帶使用者名稱），所以連「進表單前」都要先擋。

**做法**：驗證碼只送到**本機系統匣通知**（`pystray.Icon.notify()`）——遠端訪客看得到
網頁、按得到按鈕，但系統匣通知只有坐在這台電腦前面才看得到，等於要求「操作者真的在
現場」才能完成這些動作。純記憶體、不落地：一組碼的生命週期只有幾分鐘，不需要跨重啟
存活，也不必為了這個引入資料庫欄位。
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from typing import Callable

CODE_TTL_SECONDS = 300  # 5 分鐘
MAX_VERIFY_ATTEMPTS = 5
RESEND_COOLDOWN_SECONDS = 30


class VerifyCodeError(Exception):
    """發送驗證碼失敗（冷卻中、或這個環境沒有系統匣可用）。"""


class VerifyCodeGate:
    def __init__(
        self,
        *,
        ttl_seconds: float = CODE_TTL_SECONDS,
        max_attempts: int = MAX_VERIFY_ATTEMPTS,
        cooldown_seconds: float = RESEND_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_attempts = max_attempts
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._purpose: str | None = None
        self._code_hash: str | None = None
        self._issued_at: float = float("-inf")
        self._attempts = 0

    def issue(self, purpose: str, notify: Callable[[str, str], None] | None) -> None:
        """發一組新的 6 位數驗證碼、送進系統匣通知。舊碼（不管什麼用途）立刻作廢。

        冷卻期不分用途——不然遠端訪客可以交替打不同 `purpose` 繞過冷卻，對著使用者的
        系統匣連續轟炸通知。`notify` 沒有給（這個環境沒有系統匣，例如開發用的
        `dev_web_server.py`）就直接失敗，不假裝發送成功。"""
        with self._lock:
            now = self._clock()
            if now - self._issued_at < self._cooldown:
                remaining = int(self._cooldown - (now - self._issued_at)) + 1
                raise VerifyCodeError(f"請等 {remaining} 秒後再重新發送")
            code = f"{secrets.randbelow(1_000_000):06d}"
            self._purpose = purpose
            self._code_hash = _hash(code)
            self._issued_at = now
            self._attempts = 0
        if notify is None:
            raise VerifyCodeError("這個環境沒有系統匣，無法顯示驗證碼")
        try:
            notify(f"驗證碼：{code}（5 分鐘內有效，勿提供給他人）", "BahaAD 驗證碼")
        except Exception as exc:  # noqa: BLE001 - 系統匣通知本身失敗，直接讓呼叫端知道
            raise VerifyCodeError(f"系統匣通知發送失敗：{exc}") from exc

    def verify(self, purpose: str, code: str) -> bool:
        """驗證碼一次性——驗證成功、過期、或超過重試次數，都會讓這組碼作廢（不能拿同一組
        再試一次）。`purpose` 要跟發送時的用途一致，防止「用忘記密碼的碼去驗證首次設定」
        這種張冠李戴。"""
        with self._lock:
            if (
                not code
                or self._code_hash is None
                or self._purpose != purpose
                or self._clock() - self._issued_at > self._ttl
            ):
                self._reset_locked()
                return False
            self._attempts += 1
            ok = secrets.compare_digest(self._code_hash, _hash(code.strip()))
            if ok or self._attempts >= self._max_attempts:
                self._reset_locked()
            return ok

    def _reset_locked(self) -> None:
        self._purpose = None
        self._code_hash = None
        self._issued_at = float("-inf")
        self._attempts = 0


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


# 漸進式鎖定（使用者 2026-09-06 定案）：第 n 次猜錯鎖 (10 + 60*floor(n/3)) 分鐘——
# 第 1~2 次錯誤鎖 10 分鐘；第 3~5 次錯誤鎖 70 分鐘（1 小時 10 分）；第 6~8 次錯誤鎖
# 130 分鐘（2 小時 10 分）；之後每滿 3 次再疊加 1 小時，沒有上限。
_LOCKOUT_BASE_MINUTES = 10
_LOCKOUT_STEP_MINUTES = 60
_LOCKOUT_STEP_EVERY = 3


def _lockout_minutes(wrong_count: int) -> int:
    return _LOCKOUT_BASE_MINUTES + _LOCKOUT_STEP_MINUTES * (wrong_count // _LOCKOUT_STEP_EVERY)


def format_lockout_duration(seconds: float) -> str:
    """秒數 → 「N 小時 M 分」／「M 分」的中文說明，鎖定訊息用。無條件進位到分鐘，
    不要讓使用者看到「還要等 0 分」卻其實還鎖著。"""
    minutes = max(1, -(-int(seconds) // 60))  # 無條件進位
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours} 小時 {minutes} 分"
    if hours:
        return f"{hours} 小時"
    return f"{minutes} 分"


class VerifyCodeLockout:
    """驗證碼「連續猜錯」的漸進式鎖定——防止有心人士對著系統匣通知的 6 位數碼硬猜
    （使用者 2026-09-06）。

    以「這個瀏覽器（`bahaad_client_id` cookie）＋來源 IP」為單位計錯誤次數，猜錯一次
    鎖一段時間、猜錯次數愈多鎖愈久（見 `_lockout_minutes`）。**鎖定期間內直接拒絕**、
    不會讓「趁鎖定時繼續送」再拉長鎖定——只有真的送出一次驗證（拿到明確對／錯結果）才
    算一次嘗試。純記憶體、不落地：程式重啟＝鎖定重置，這是可接受的取捨（不值得為了
    這個開資料庫表；重啟本來就需要坐在這台電腦前面才辦得到，風險有限）。
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        # key -> (累積錯誤次數, 鎖到什麼時候（monotonic 秒數）)
        self._state: dict[str, tuple[int, float]] = {}

    def remaining_seconds(self, key: str) -> float:
        """回傳這個 key 還要鎖多久（秒）；0 表示現在可以嘗試。"""
        with self._lock:
            _count, locked_until = self._state.get(key, (0, 0.0))
            return max(0.0, locked_until - self._clock())

    def record_failure(self, key: str) -> float:
        """記一次猜錯，回傳這次算出的鎖定秒數。"""
        with self._lock:
            count, _ = self._state.get(key, (0, 0.0))
            count += 1
            lock_seconds = _lockout_minutes(count) * 60
            self._state[key] = (count, self._clock() + lock_seconds)
            return lock_seconds

    def record_success(self, key: str) -> None:
        """驗證成功——這個 key 的錯誤紀錄歸零（不是「解鎖」：鎖定期間本來就進不了
        `record_failure`／`record_success`，這裡只是讓下一輪重新從第 1 次算）。"""
        with self._lock:
            self._state.pop(key, None)
