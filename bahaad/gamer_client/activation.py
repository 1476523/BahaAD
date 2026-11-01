"""動畫瘋登入憑證（BAHARUNE）輪換的序列化——比照 aniGamerPlus 的 `_device_activation_lock`。

`getdeviceid.php` → `token.php` / `video_src.php` 這段下載交握，全程綁在同一組 device_id
＋同一份 cookie 上。期間若有**別的**請求打 `ani.gamer.com.tw`／`api.gamer.com.tw` 讓
伺服器在回應裡輪換 BAHARUNE，交握拿到的 device_id 就跟換過的 cookie 對不上 →
`code 1007 裝置驗證異常`（使用者 2026-09-05 回報：BahaAD 仍偶發 1007）。

BahaAD 用**單一 `GamerSession`**，公告掃描（`GossipWatcher`）、週期表檢查
（`CompletionWatcher`）、cookie 保活（`CookieWarmup`）都共用它打首頁 → 都會輪換
BAHARUNE。用一把共用鎖把「會輪換 BAHARUNE 的請求」序列化：

- **下載交握**：`with lock:`（阻塞等待——一定要拿到，下載不能因為背景在忙就放棄）
- **背景請求**：`with skip_if_busy(lock) as free: if not free: 這輪跳過`（下載優先，
  背景讓路，下一輪再來——公告 30 分鐘一輪、保活 30 分鐘一輪，跳一次沒差）
"""

from __future__ import annotations

import contextlib
import threading


@contextlib.contextmanager
def skip_if_busy(lock: "threading.Lock | None"):
    """非阻塞搶鎖。搶到 → `yield True`（離開 `with` 時釋放）；搶不到（下載交握正在跑）
    → `yield False`，呼叫端該跳過會輪換 BAHARUNE 的請求。`lock=None`（測試）一律 `True`。"""
    if lock is None:
        yield True
        return
    acquired = lock.acquire(blocking=False)
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()


class LockedHttp:
    """把帶登入 cookie 的 session 包起來：每個請求都先拿下載交握鎖。

    網頁的首頁重抓（登入 session，為了補 18 禁番劇）原本直接用 `GamerSession`，沒走上面那把鎖——
    首頁重抓的請求可能插進下載交握（getdeviceid → video_src）中間，輪換 BAHARUNE，下載就碰
    code 1007（使用者 2026-10-12：11:59:49 首頁重抓、12:00:08 排程下載 1007，連換兩組新的裝置
    識別碼都失敗）。這裡每個請求各自拿鎖（不是整段重抓一次鎖到底），下載交握最多等一個請求的
    時間，重抓的請求也不會在交握進行中發出。`lock=None`（測試）＝不加鎖。其餘屬性原樣轉給內層。"""

    def __init__(self, inner, lock: "threading.Lock | None") -> None:
        self._inner = inner
        self._lock = lock

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def _locked(self, method: str, *args, **kwargs):
        fn = getattr(self._inner, method)
        if self._lock is None:
            return fn(*args, **kwargs)
        with self._lock:
            return fn(*args, **kwargs)

    def get(self, *args, **kwargs):
        return self._locked("get", *args, **kwargs)

    def post(self, *args, **kwargs):
        return self._locked("post", *args, **kwargs)

    def browse_get(self, *args, **kwargs):
        return self._locked("browse_get", *args, **kwargs)
