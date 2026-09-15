"""把累積的使用統計增量送到 `access_gate_server` 的 `/stats/report`。規格見
docs/requirements/realtime_stats.md。

背景執行緒，比照 `access_gate/heartbeat.py` 的 `check_once()`／`start()`／`stop()`：

- 每輪檢查「有沒有東西要送」：`StatsPendingStore` 非空、或旗標（has_schedule／
  public_mode）跟上次送的不一樣、或「已回報錯誤的次數」相對上次有增加、或**史上
  還沒送過**（首次啟動立刻回報一組匿名識別碼，使用者 2026-09-08）。
- 送出成功 → `pending.clear()`、記下這次送的旗標／診斷計數，重試狀態歸零。
- 送出失敗 → `pending.note_failure()`：照 3分x10 → 1時x10 → 3時無限 的節奏退避
  （`access_gate_server` 可能因更新停機，資料不能丟）。
- 開關 `realtime_stats_enabled` 關掉 → 整輪 no-op（不送、不動 pending、不動重試狀態）
  ——重新打開就從上次的緩衝繼續送（使用者：pending 保留不丟）。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Protocol

from bahaad.store.activation import ActivationStore
from bahaad.store.schedule_list import ScheduleListStore
from bahaad.store.settings import SettingsStore
from bahaad.store.stats_pending import StatsPendingStore

logger = logging.getLogger(__name__)

_SETTINGS_KEY = "realtime_stats_enabled"
_DIAG_COUNT_KEY = "diagnostics_reported_count"
# 執行期狀態（前綴底線＝跟 _pending_update 等同慣例，不是使用者設定）
_LAST_FLAGS_KEY = "_stats_last_sent_flags"
_LAST_DIAG_KEY = "_stats_last_sent_diag_count"
_EVER_SENT_KEY = "_stats_ever_sent"

_DEFAULT_LOOP_SECONDS = 60
_DEFAULT_REQUEST_TIMEOUT = 5.0


class HttpPoster(Protocol):
    def post(self, url: str, **kwargs) -> object: ...


class StatsReporter:
    def __init__(
        self,
        settings: SettingsStore,
        activation_store: ActivationStore,
        pending: StatsPendingStore,
        schedule_store: ScheduleListStore,
        http: HttpPoster,
        base_url: str,
        *,
        request_timeout: float = _DEFAULT_REQUEST_TIMEOUT,
        loop_seconds: int = _DEFAULT_LOOP_SECONDS,
    ) -> None:
        self._settings = settings
        self._activation_store = activation_store
        self._pending = pending
        self._schedule_store = schedule_store
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._request_timeout = request_timeout
        self._loop_seconds = loop_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- 旗標 ------------------------------------------------------------

    def _current_flags(self) -> dict:
        try:
            has_schedule = bool(self._schedule_store.get_entries())
        except Exception:  # noqa: BLE001
            has_schedule = False
        return {
            "has_schedule": has_schedule,
            "public_mode": bool(self._settings.get("public_mode", False)),
        }

    def _diag_count(self) -> int:
        try:
            return int(self._settings.get(_DIAG_COUNT_KEY, 0) or 0)
        except (TypeError, ValueError):
            return 0

    # ---- 一輪 ------------------------------------------------------------

    def check_once(self) -> None:
        if not self._settings.get(_SETTINGS_KEY, True):
            return

        attempt, phase, next_at = self._pending.get_retry_state()
        if next_at and time.time() < next_at:
            return  # 退避中，還沒到下次可以試的時間

        pending_snap = self._pending.snapshot()
        flags = self._current_flags()
        diag_count = self._diag_count()
        ever_sent = bool(self._settings.get(_EVER_SENT_KEY, False))
        last_flags = self._settings.get(_LAST_FLAGS_KEY, None)
        last_diag = int(self._settings.get(_LAST_DIAG_KEY, 0) or 0)

        has_payload = bool(pending_snap["counters"] or pending_snap["anime"])
        flags_changed = last_flags != flags
        diag_delta = max(0, diag_count - last_diag)
        if not (has_payload or flags_changed or diag_delta or not ever_sent):
            return  # 沒東西要送，這輪不動任何狀態

        payload = {
            "activation_id": self._activation_store.get_or_create(),
            "counters": dict(pending_snap["counters"]),
            "anime": pending_snap["anime"],
            "flags": flags,
        }
        if diag_delta:
            payload["counters"]["diagnostics_reports"] = (
                payload["counters"].get("diagnostics_reports", 0) + diag_delta
            )

        ok = self._send(payload)
        if ok:
            # 只扣掉這次真的送出去的量——送出的網路往返裡並發新加的事件保留、下輪再送
            self._pending.commit_sent(pending_snap)
            self._settings.update(
                {
                    _LAST_FLAGS_KEY: flags,
                    _LAST_DIAG_KEY: diag_count,
                    _EVER_SENT_KEY: True,
                }
            )
        else:
            self._pending.note_failure(time.time())

    def _send(self, payload: dict) -> bool:
        try:
            resp = self._http.post(
                f"{self._base_url}/stats/report", json=payload, timeout=self._request_timeout
            )
        except Exception as exc:  # noqa: BLE001 - 連不上就當失敗、退避後再試
            logger.debug("stats 回報送出失敗（會之後補送）：%s", exc)
            return False
        status = getattr(resp, "status_code", 0)
        if 200 <= status < 300:
            return True
        logger.debug("stats 回報伺服器回 HTTP %s（會之後補送）", status)
        return False

    # ---- 執行緒 ----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="stats-reporter")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.check_once()
            except Exception:
                logger.exception("stats 回報這一輪發生未預期的例外")
            self._stop_event.wait(self._loop_seconds)
