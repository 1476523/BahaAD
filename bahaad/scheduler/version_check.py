"""定期查 GitHub Release 提醒使用者有新版本。規格見 docs/requirements/scheduler_version_check.md。

跟 `updater/`（Phase 5 的差異更新機制，manifest 簽章驗證/逐檔套用）是完全獨立的兩條路——
這裡只查 GitHub 上這個 repo 最新的 Release tag 跟 `bahaad.__version__` 比對，不一樣就記錄
「有新版本可用」，不下載、不安裝、不強迫使用者做任何事，純粹是資訊來源。

刻意不做語意化版本比較：`__version__` 是開發階段字串，不保證嚴格遵守 semver，GitHub tag
也可能帶 `v` 前綴。字串不相等就視為有差異，使用者自己點進 Release 頁面判斷要不要更新。
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime
from typing import Protocol

from bahaad import is_beta
from bahaad.diagnostics.codes import ConnectionStatus, classify_connection_error
from bahaad.notify.dispatch import send_notification
from bahaad.store.notify import NotifyStore
from bahaad.store.settings import SettingsStore

logger = logging.getLogger(__name__)

_GITHUB_RELEASES_URL = "https://api.github.com/repos/1476523/BahaAD/releases/latest"
# `/releases/latest` 會跳過 prerelease；Beta channel 要看得到 prerelease，改列全部
# release 取最新一筆（GitHub 依 created_at 由新到舊排序，未授權看不到 draft）
_GITHUB_RELEASES_LIST_URL = "https://api.github.com/repos/1476523/BahaAD/releases?per_page=10"
# Beta channel 開關：主版本 0（Beta）預設 True、正式版預設 False，使用者可在設定頁改
_CHANNEL_SETTINGS_KEY = "version_check_include_prereleases"
# 檢查週期固定 1 小時，不開放自訂（使用者 2026-08-29）——只是打一次 GitHub Releases
# API、成本極低，沒有讓使用者調的意義
_INTERVAL_HOURS = 1
# 前綴底線標記「這不是使用者可編輯的偏好設定，是執行期快取結果」——SettingsStore 本身
# 沒有區分這兩種用途的機制，這是呼叫端自己用命名慣例做區隔
_SETTINGS_KEY = "_update_available"
# Telegram 單則訊息 4096 字上限，release notes 全文常常超過；超過時裁切保留開頭方便
# 掌握重點，完整內容使用者仍可自行到 GitHub 查看。不做舊專案那種「掐頭去尾各留一段」
# 的更精緻裁切（那是錦上添花，不影響「有沒有通知到」這個核心價值），見 notify.md
# 「刻意精簡」風格
_VERSION_BODY_MAX_CHARS = 2000


class HttpGetter(Protocol):
    def get(self, url: str) -> "_ResponseLike": ...


class _ResponseLike(Protocol):
    def json(self): ...


def _strip_v(tag: str) -> str:
    """GitHub Release tag 慣例帶 `v` 前綴（`v0.0.1`），`bahaad.__version__` 不帶
    （`0.0.1`）。比對前把兩邊的前綴都拿掉，否則第一個版本就會被誤判成「有新版」。"""
    return tag[1:] if tag[:1] in ("v", "V") else tag


import html as _html

# 轉換過程用的暫時哨兵——先把「要變成標籤的地方」標成 \x01…\x02，整段 html.escape
# 之後（把 prose 裡的 < > & 跳脫掉）再換回真正的 Telegram HTML 標籤，避免 release
# notes 內文裡萬一有 `<` / `&` 破壞 parse_mode=HTML。
_S_OPEN, _S_CLOSE = "\x01", "\x02"


def _release_notes_to_tg_html(text: str) -> str:
    """GitHub Release 內文是 markdown ＋ `<details>` / `<summary>` HTML——直接塞進通知
    會顯示一堆生標籤、`**`、`## `、`[文字](網址)`（使用者 2026-09-10）。轉成 Telegram
    HTML：`<summary>` 的「細節」摺疊區 → `<blockquote expandable>`，連結 → `<a href>`，
    `**粗體**` → `<b>`。Discord 版由 `notify/dispatch._send_prebuilt_html` 再轉 markdown。"""
    if not text:
        return ""
    t = text.replace("\r\n", "\n")

    # 1) 內層「細節」摺疊：`<details><summary>細節</summary> … </details>`（沒有再巢狀
    #    details 的那種）→ 可展開引用。內文去掉 markdown 的縮排。
    def _bq(m):
        inner = re.sub(r"(?m)^[ \t]+", "", m.group(1).strip())
        return f"\n{_S_OPEN}blockquote expandable{_S_CLOSE}{inner}{_S_OPEN}/blockquote{_S_CLOSE}\n"

    t = re.sub(r"<details>\s*<summary>[^<]*</summary>((?:(?!<details).)*?)</details>",
               _bq, t, flags=re.S | re.I)

    # 2) 外層 <details> 換行 <summary>類別</summary> → 粗體小標；剩下的 </details> 拿掉
    t = re.sub(r"<details>\s*<summary>\s*(.*?)\s*</summary>",
               lambda m: f"{_S_OPEN}b{_S_CLOSE}{m.group(1)}{_S_OPEN}/b{_S_CLOSE}", t, flags=re.S | re.I)
    t = re.sub(r"</?details>", "", t, flags=re.I)
    t = re.sub(r"</?summary>", "", t, flags=re.I)

    # 3) markdown 連結 [文字](網址) → 超連結
    t = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        lambda m: f'{_S_OPEN}a href="{m.group(2)}"{_S_CLOSE}{m.group(1)}{_S_OPEN}/a{_S_CLOSE}',
        t,
    )
    # 4) **粗體** → <b>
    t = re.sub(r"\*\*(.+?)\*\*",
               lambda m: f"{_S_OPEN}b{_S_CLOSE}{m.group(1)}{_S_OPEN}/b{_S_CLOSE}", t, flags=re.S)
    # 5) 標題整行拿掉（`## v0.0.5` 的版本號已經在通知第一行）
    t = re.sub(r"^#{1,6}[ \t].*$", "", t, flags=re.M)
    # 6) 整行分隔線
    t = re.sub(r"^[ \t]*[-*_]{3,}[ \t]*$", "", t, flags=re.M)
    # 7) 條列 `- ` / `* ` → `• `（去掉子項的縮排，Telegram 不吃）
    t = re.sub(r"^[ \t]*[-*][ \t]+", "• ", t, flags=re.M)

    # 8) 整段跳脫（把 prose 裡殘留的 < > & 變安全），再把哨兵換回真標籤
    t = _html.escape(t, quote=False)
    t = t.replace(_S_OPEN, "<").replace(_S_CLOSE, ">")

    # 9) 收尾：去每行尾端空白、壓多餘空行、拿掉 • 項目跟它的摺疊引用之間的空行、
    #    平衡未關的 blockquote
    t = "\n".join(line.rstrip() for line in t.split("\n"))
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"\n\n(<blockquote expandable>)", r"\n\1", t)
    t = t.strip()
    opened = t.count("<blockquote expandable>")
    closed = t.count("</blockquote>")
    if opened > closed:
        t += "</blockquote>" * (opened - closed)
    return t


def _truncate_body(text: str, max_chars: int = _VERSION_BODY_MAX_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n...(內容過長，完整內容請至 GitHub 查看)"


class VersionChecker:
    def __init__(
        self,
        settings: SettingsStore,
        http: HttpGetter,
        current_version: str,
        notify_store: NotifyStore | None = None,
        on_new_version=None,
    ) -> None:
        self._settings = settings
        self._http = http
        self._current_version = current_version
        self._notify_store = notify_store
        # 偵測到 GitHub 上有新版本（先前沒看過的標籤）時呼叫一次——app_shell 掛成
        # 「立刻叫 UpdateCoordinator 抓差異檔」，不用等它自己每小時那輪（使用者
        # 2026-09-08：「不然根本不會出現套用更新選項」）。
        self._on_new_version = on_new_version
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def _include_prereleases(self) -> bool:
        # 預設值依「這個 build 本身是不是 Beta」——跑 Beta 版的人本來就想收 Beta 更新，
        # 跑正式版的人預設只收正式版。使用者可在設定頁覆寫。
        return bool(self._settings.get(_CHANNEL_SETTINGS_KEY, is_beta(self._current_version)))

    def _fetch_latest_release(self) -> dict | None:
        """回傳最新 release 的 payload dict（含 `tag_name`/`body`）；查詢失敗或沒有任何
        release 就回 None。Beta channel 時列全部 release 取第一筆（含 prerelease）。"""
        if self._include_prereleases():
            releases = self._http.get(_GITHUB_RELEASES_LIST_URL, timeout=15).json()
            if not isinstance(releases, list) or not releases:
                return None
            return releases[0]
        return self._http.get(_GITHUB_RELEASES_URL, timeout=15).json()

    def check_once(self) -> str | None:
        """跑一次檢查，回傳偵測到的新版本字串；沒有新版本或查詢失敗都回傳 None。"""
        try:
            release = self._fetch_latest_release()
            if release is None:
                return None
            payload = release
            latest_version = payload["tag_name"]
        except Exception as exc:
            # 涵蓋網路錯誤、非預期的 JSON 格式、以及 releases/latest 回 404（GitHub 的
            # 404 回應本身就沒有 tag_name 欄位，KeyError 自然落進這裡，不用另外判斷狀態碼）
            if classify_connection_error(exc) is not ConnectionStatus.OTHER:
                # 純粹連不到 api.github.com（VPN／過濾軟體）——記 INFO、不每輪噴 WARNING
                logger.info("version_check 這輪查不到 GitHub Release（連線問題），下輪再試")
            else:
                logger.warning("version_check 查詢 GitHub Release 失敗：%s", exc)
            return None

        if _strip_v(latest_version) == _strip_v(self._current_version):
            self._settings.reset([_SETTINGS_KEY])
            return None

        # 只在「這個版本標籤先前沒偵測到過」時才通知——每輪檢查（預設 12 小時一次）
        # 都會重新寫入同一個 _update_available，不做這個比對的話，同一個新版本會每輪
        # 都重複推播一次
        previously_seen = self._settings.get(_SETTINGS_KEY)
        is_new_detection = previously_seen is None or previously_seen.get("latest_version") != latest_version

        self._settings.update(
            {_SETTINGS_KEY: {"latest_version": latest_version, "checked_at": datetime.now().isoformat()}}
        )

        if is_new_detection and self._notify_store is not None:
            notes_md = _truncate_body(str(payload.get("body") or ""))
            notes_html = _release_notes_to_tg_html(notes_md)
            tag = _html.escape(latest_version, quote=False)
            prebuilt = f"發現 GitHub 上有新版本: <b>{tag}</b>"
            if notes_html:
                prebuilt += f"\n更新內容:\n{notes_html}"
            send_notification(
                self._notify_store, self._settings, self._http, "system_new_version",
                prebuilt_html=prebuilt,
            )

        if is_new_detection and self._on_new_version is not None:
            try:
                self._on_new_version(latest_version)
            except Exception:  # noqa: BLE001 - 觸發下載失敗不影響「有新版」這個事實
                logger.debug("on_new_version 回呼發生例外", exc_info=True)

        return latest_version

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
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
                logger.exception("version_check 這一輪檢查發生未預期的例外")
            self._stop_event.wait(_INTERVAL_HOURS * 3600)
