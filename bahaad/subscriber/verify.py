"""「發送測試訊息」的驗證碼關卡（使用者 2026-09-23）——與其只看中繼 API 回應
「送出成功」就當作這個管道確認可以收到通知，改成隨機產生一組 6 位數驗證碼、
連同測試訊息一起送出，訂閱者要自己把在 Telegram／Discord 看到的碼貼回網站才算
真的確認收到。

比照 `bahaad/verify_code.py`（擁有者端「本機驗證碼」關卡）同一套設計——TTL、
單次有效、猜錯次數上限——但這裡要同時服務多個訂閱者身分，所以是「identity_id
對應一組碼」而不是單一全域槽。純記憶體、不落地：碼的生命週期只有幾分鐘，
不需要跨重啟存活，不值得為了這個開資料庫欄位（比照 `verify_code.py` 的理由）。
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from typing import Callable

CODE_TTL_SECONDS = 300  # 5 分鐘
MAX_VERIFY_ATTEMPTS = 5


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


class SubscriberVerifyCodes:
    def __init__(
        self,
        *,
        ttl_seconds: float = CODE_TTL_SECONDS,
        max_attempts: int = MAX_VERIFY_ATTEMPTS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_attempts = max_attempts
        self._clock = clock
        self._lock = threading.Lock()
        # identity_id -> (code_hash, issued_at, attempts)
        self._slots: dict[int, tuple[str, float, int]] = {}

    def issue(self, identity_id: int) -> str:
        """發一組新碼給這個身分——舊的（不管有沒有驗證過）立刻作廢，不能拿舊訊息
        裡的碼驗證新一輪。回傳明碼（呼叫端塞進訊息內文送出，這裡只留雜湊）。"""
        code = f"{secrets.randbelow(1_000_000):06d}"
        with self._lock:
            self._slots[identity_id] = (_hash(code), self._clock(), 0)
        return code

    def verify(self, identity_id: int, code: str) -> bool:
        """驗證碼一次性——驗證成功、過期、或超過重試次數，都會讓這組碼作廢。"""
        with self._lock:
            slot = self._slots.get(identity_id)
            if slot is None:
                return False
            code_hash, issued_at, attempts = slot
            if not code or self._clock() - issued_at > self._ttl:
                self._slots.pop(identity_id, None)
                return False
            attempts += 1
            ok = secrets.compare_digest(code_hash, _hash(code.strip()))
            if ok or attempts >= self._max_attempts:
                self._slots.pop(identity_id, None)
            else:
                self._slots[identity_id] = (code_hash, issued_at, attempts)
            return ok
