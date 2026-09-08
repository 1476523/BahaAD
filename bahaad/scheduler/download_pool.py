"""跨執行緒共用的下載並發執行器。規格見 docs/requirements/scheduler_main_loop.md「最大並發
下載數」一節。

`scheduler/main_loop.py`／`scheduler/custom_schedule.py` 都會觸發下載，如果各自開一個獨立
的 thread pool，使用者設定的「最多同時下載幾部」形同虛設——兩條各自開到上限，總數變成兩倍。
`DownloadPool` 是單一共用實例，由呼叫端（`app_shell.build_services()`）建立一次、同時傳給
兩邊，確保上限是全域的。

**2026-08-28（round 6 第 15 項）**：底層 `ThreadPoolExecutor` 固定開到上限 5，實際同時
在跑幾部由一個可調整的號誌（semaphore）閘門控制——閘門的許可數每次 `submit` 都重新從
`SettingsStore` 讀，使用者在設定頁改「最大一次下載數」下一部就生效，不用重開程式。
調小時是「best effort」：正在跑的下載不會被中斷，多出來的許可會在它們陸續跑完時收回。

上限固定為 5，不管設定值多大都會被夾住——同時開太多條下載對動畫瘋的 CDN 也是負擔。
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import Callable

from bahaad.store.settings import SettingsStore

_DEFAULT_MAX_CONCURRENT_DOWNLOADS = 2
_MAX_ALLOWED_CONCURRENT_DOWNLOADS = 5


class _AnyEvent:
    """把「全域關閉 stop_event」跟「這一個 video_sn 的中止 event」包成一個物件，
    介面只有 `is_set()`——`segment.py` 的 `_check_stop()` 就是這樣用的，不用改它的簽章。
    任一個 set 了，下載流程就會拋 `DownloadInterrupted`。"""

    def __init__(self, *events: threading.Event) -> None:
        self._events = events

    def is_set(self) -> bool:
        return any(event.is_set() for event in self._events)


class DownloadPool:
    def __init__(self, settings: SettingsStore) -> None:
        self._settings = settings
        self._executor = ThreadPoolExecutor(max_workers=_MAX_ALLOWED_CONCURRENT_DOWNLOADS)
        self._lock = threading.Lock()
        self._futures: list[Future] = []
        # 關閉時 set：Stage C 會把它傳進下載流程，讓進行中的下載能提早中斷
        self.stop_event = threading.Event()
        # round 7 第 18 項：每個提交下載的 video_sn 一個中止旗標。使用者在下載列表按
        # 「中止」→ cancel(video_sn) → 這一個 event set → 只有那一集的下載會拋
        # DownloadInterrupted，其他集照跑（跟全域 stop_event 不同）。
        self._task_events: dict[int, threading.Event] = {}
        self._gate = threading.Semaphore(0)
        self._issued = 0
        self._sync_permits()
        # round 7 第 10 項：設定改了要「即時」生效——光靠 submit / 任務完成時才 _sync_permits
        # 的話，3 部正在下載時把上限調 5，得等其中一部跑完才會放行 parked 的任務。一條低頻
        # daemon 定期對齊就好（純記憶體操作，2 秒一次沒有負擔）。
        self._poller = threading.Thread(target=self._poll_permits, daemon=True)
        self._poller.start()

    def _poll_permits(self) -> None:
        while not self.stop_event.wait(2.0):
            try:
                self._sync_permits()
            except Exception:  # noqa: BLE001
                pass

    def _current_limit(self) -> int:
        raw = self._settings.get("max_concurrent_downloads", _DEFAULT_MAX_CONCURRENT_DOWNLOADS)
        return max(1, min(raw, _MAX_ALLOWED_CONCURRENT_DOWNLOADS))

    def _sync_permits(self) -> None:
        """把號誌的許可數對齊目前設定值。放大立即生效；縮小只在此刻有空閒許可時收回
        （拿不到就等下一個下載跑完再收），不中斷進行中的下載。"""
        target = self._current_limit()
        with self._lock:
            while self._issued < target:
                self._gate.release()
                self._issued += 1
            while self._issued > target and self._gate.acquire(blocking=False):
                self._issued -= 1

    def submit(self, fn: Callable, *args, task_key: int | None = None, **kwargs) -> Future:
        self._sync_permits()
        if task_key is not None:
            with self._lock:
                self._task_events.setdefault(task_key, threading.Event())

        def _gated():
            self._gate.acquire()
            try:
                # 關閉中也照樣呼叫 fn（`_download_worker`）——它會馬上碰到 stop_event、
                # 拋 DownloadInterrupted、把 registry 收乾淨，不會真的開始下載
                return fn(*args, **kwargs)
            finally:
                self._gate.release()
                self._sync_permits()  # 一部跑完，套用先前拿不到而延後的縮小
                if task_key is not None:
                    with self._lock:
                        self._task_events.pop(task_key, None)

        future = self._executor.submit(_gated)
        with self._lock:
            self._futures = [f for f in self._futures if not f.done()]
            self._futures.append(future)
        return future

    def cancel(self, task_key: int) -> bool:
        """中止某個提交中／進行中的下載。回傳它是否還在（True＝有這個任務、旗標已 set；
        False＝已經跑完或從沒提交過）。實際的中斷由下載流程檢查 `stop_event_for()` 達成。"""
        with self._lock:
            event = self._task_events.get(task_key)
            if event is None:
                return False
            event.set()
            return True

    def is_task_cancelled(self, task_key: int) -> bool:
        with self._lock:
            event = self._task_events.get(task_key)
        return event is not None and event.is_set()

    def stop_event_for(self, task_key: int) -> _AnyEvent:
        """傳給 `segment.download(stop_event=...)`：全域關閉 or 這一集被中止，任一成立
        就中斷。`task_key` 沒登記過（例如測試不透過 submit 直接呼叫）時只看全域 stop_event。"""
        with self._lock:
            event = self._task_events.get(task_key)
        return _AnyEvent(self.stop_event) if event is None else _AnyEvent(self.stop_event, event)

    def wait_idle(self, timeout: float | None = None) -> None:
        """等待目前所有已提交的任務完成。測試用來讓非同步下載變成可斷言的同步結果，
        正式執行時也可以在程式關閉前呼叫，等進行中的下載真的結束再退出。"""
        with self._lock:
            futures = list(self._futures)
        wait(futures, timeout=timeout)

    def shutdown(self, wait_for_tasks: bool = False) -> None:
        """關閉。預設不等進行中的下載——`stop_event` 已 set，下載流程會自己提早中斷
        （見 Stage C）。`wait_for_tasks=True` 時仍等（測試／少數需要）。"""
        self.stop_event.set()
        # 喚醒所有卡在 _gate.acquire() 的 parked 執行緒，讓它們看到 stop_event 收工
        self._gate.release(_MAX_ALLOWED_CONCURRENT_DOWNLOADS)
        self._executor.shutdown(wait=wait_for_tasks, cancel_futures=True)
