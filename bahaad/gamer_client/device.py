"""裝置身分（deviceid）的申請與持久化。規格見 docs/requirements/gamer_client_device.md。

deviceid 是跟 ajax/token.php／ajax/checklock.php 溝通用的必要欄位。啟動時先問
IdentityStore 有沒有既有值，有就直接重用（官方前端也是「本地有值就不重打」的邏輯，見
docs/api-observations/device-id.md）；沒有才去 ajax/getdeviceid.php 申請一組新的並存回去。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from bahaad.gamer_client.session import GamerSession, session_auth_digest
from bahaad.store.identity import IdentityStore

logger = logging.getLogger(__name__)

_GETDEVICEID_URL = "https://ani.gamer.com.tw/ajax/getdeviceid.php"


class DeviceIdError(Exception):
    pass


class DeviceIdManager:
    def __init__(self, identity_store: IdentityStore, session: GamerSession) -> None:
        self._identity_store = identity_store
        self._session = session

    def get_or_create(self) -> str:
        existing = self._identity_store.get_device_id()
        if existing is not None:
            now = session_auth_digest(self._session)
            issued_under = existing[1] or "?"
            if issued_under != "?" and issued_under != now:
                logger.info(
                    "沿用既有 device_id …%s，但登入 cookie 已從核發時的 %s 輪換成 %s"
                    "——接下來打 video_src.php 可能 1007",
                    existing[0][-6:], issued_under, now,
                )
            else:
                logger.debug(
                    "沿用既有 device_id …%s（核發時登入態 %s，目前 %s）",
                    existing[0][-6:], issued_under, now,
                )
            return existing[0]
        return self._request_new()

    def ensure_fresh(self) -> str:
        """給「開始一集新下載」用：cookie 沒換過就沿用既有 device_id，沒有或 cookie
        真的換過（核發時登入態雜湊 != 目前雜湊）才重新申請。

        **2026-09-06 改的，取代原本「每次下載都 invalidate() 再要新的」**：真實前端本來
        就是「`localStorage` 有值就不重打 `getdeviceid.php`」（見
        docs/api-observations/device-id.md），同帳號同時間應該只有一組有效 device_id——
        每次下載都硬換一組，並發下載／連續重試時彼此的 device_id 互踢，才是使用者
        2026-09-06 回報「日誌顯示每次都是全新 device_id、登入 cookie 全程沒變、仍然
        1007」的真正原因（原本以為是 cookie 輪換跟 device_id 核發時機錯開，這批診斷日誌
        证明不是）。真的 1007 時仍呼叫 `invalidate()` 換新，那是站方已經明確拒絕這組
        device_id 的情況，跟這裡「沒事先不要換」不衝突。"""
        existing = self._identity_store.get_device_id()
        if existing is None:
            return self._request_new()
        device_id, issued_digest = existing
        now = session_auth_digest(self._session)
        if issued_digest and issued_digest != now:
            logger.info(
                "device_id …%s 核發時的登入態（%s）跟目前（%s）不一樣，重新申請一組",
                device_id[-6:], issued_digest, now,
            )
            return self._request_new()
        logger.debug("沿用既有 device_id …%s（登入態 %s 沒變，不重打 getdeviceid.php）",
                     device_id[-6:], now)
        return device_id

    def invalidate(self) -> None:
        """清掉目前的 device_id，下次 get_or_create() 會重新申請一組。收到 code 1007
        「裝置驗證異常」時呼叫——那組 device_id 本身被站方標記了，重試不會好，換一組新的。"""
        logger.info("作廢目前的 device_id（下次重新申請一組）")
        self._identity_store.invalidate_device_id()

    def _request_new(self) -> str:
        before = session_auth_digest(self._session)
        try:
            response = self._session.get(_GETDEVICEID_URL)
            device_id = response.json()["deviceid"]
        except Exception as exc:
            raise DeviceIdError(f"申請裝置 ID 失敗: {exc}") from exc
        if not device_id:
            raise DeviceIdError("申請裝置 ID 失敗：回應裡沒有 deviceid")
        after = session_auth_digest(self._session)
        # device_id 綁在「核發當下的登入 cookie」上——存下核發時的登入態雜湊，之後
        # video_src.php 碰 1007 時就能比對是不是 cookie 在這中間被輪換了。
        # getdeviceid.php 自己的回應就把 cookie 換掉的話（before≠after），這組 device_id
        # 一出生就對不上目前 cookie ＝ 下一步 video_src.php 幾乎必 1007（使用者 2026-09-06）。
        if before != after:
            logger.warning(
                "getdeviceid.php 的回應就把登入 cookie 換掉了（%s→%s）——新 device_id …%s "
                "可能一核發就對不上目前 cookie",
                before, after, device_id[-6:],
            )
        self._identity_store.set_device_id(device_id, fingerprint=after)
        logger.info("新申請 device_id …%s（核發時登入態 %s）", device_id[-6:], after)
        return device_id


class GuestDeviceIdManager:
    """訪客身分（未登入動畫瘋）的 device id——**只放在記憶體**，不進 IdentityStore。

    訪客身分整套（cookie、device id）都不持久化，理由同 GuestSession：不污染「應該只放
    登入身分」的 IdentityStore，也避免「登入 device id 被訪客 device id 覆蓋 → 下次登入
    下載對不上 → code 1007 churn」。程式重啟後重新申請。介面（get_or_create / invalidate）
    跟 DeviceIdManager 一致，GuestAccessClient 兩種都吃。"""

    def __init__(self, session: Any) -> None:
        self._session = session
        self._device_id: str | None = None
        self._lock = threading.Lock()

    def get_or_create(self) -> str:
        with self._lock:
            if self._device_id is None:
                self._device_id = self._request_new()
            return self._device_id

    def ensure_fresh(self) -> str:
        """訪客身分沒有 cookie_warmup 那種背景輪換，跟 `get_or_create()` 同語意（有就沿用、
        沒有才申請）——這裡只是給 `PlaylistClient` 統一介面，兩種 device manager 都能吃
        `refresh_device_id=True`。"""
        return self.get_or_create()

    def invalidate(self) -> None:
        with self._lock:
            self._device_id = None

    def _request_new(self) -> str:
        try:
            response = self._session.get(_GETDEVICEID_URL)
            device_id = response.json()["deviceid"]
        except Exception as exc:
            raise DeviceIdError(f"申請訪客裝置 ID 失敗: {exc}") from exc
        if not device_id:
            raise DeviceIdError("申請訪客裝置 ID 失敗：回應裡沒有 deviceid")
        return device_id
