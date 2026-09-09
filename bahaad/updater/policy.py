"""判斷這次更新是「沒有更新／一般更新／重點更新」，並提供定期檢查的背景執行緒。
規格見 docs/requirements/updater.md。

放在 `updater/` 底下、不是 `scheduler/` 的第七個模組——`docs/decisions/
0000-architecture-overview.md` 明確把 `updater/` 列成獨立於 `scheduler/` 的模組，這裡
的背景執行緒是「更新機制自己的」，不是「排程下載」的一種。

2026-08-28 收斂成兩級（使用者定案，見 web_redesign_round5 之後的討論）：拿掉原本的
「簡易更新／`core=false` 靜默熱套用」——正式版 Flask 會快取編譯後的樣板，熱套用其實
不生效，且範圍只有 `web/static`＋`web/templates`。所有差異更新一律「下載 → 提醒 →
重啟才生效」，只分「一般更新」跟「重點（強制）更新」。
"""

from __future__ import annotations

import logging
import threading
import time
from enum import Enum
from pathlib import Path

from bahaad.diagnostics.codes import ConnectionStatus, ErrorCode, FileIntegrityResult, OperationType, classify_connection_error
from bahaad.diagnostics.reporter import DiagnosticsReporter
from bahaad.store.settings import SettingsStore
from bahaad.updater.fetcher import HashMismatchError, fetch_files
from bahaad.updater.manifest import (
    HttpGetter,
    ManifestSignatureError,
    ManifestUnavailableError,
    UpdateManifest,
    fetch_manifest,
)

logger = logging.getLogger(__name__)

# 檢查週期預設 1 小時（使用者 2026-09-08，原本 12）——比照 scheduler/version_check.py。
# 抓一次 manifest.json 成本極低；有更新時才下載，而且下面的「同一版不重抓」擋著、
# 不會每小時重抓同一批檔案。仍留 `update_check_interval_hours` 設定當覆寫（無 UI）。
_DEFAULT_INTERVAL_HOURS = 1
_DEFAULT_MAX_RETRIES = 3
# 前綴底線標記「這不是使用者可編輯的偏好設定，是執行期快取結果」——跟
# scheduler/version_check.py 的 `_update_available` 同樣的命名慣例，但這兩個 key
# 意義不同：`_update_available` 是 GitHub Release 提醒，這個是差異更新真的準備好了
_SETTINGS_KEY = "_pending_update"
# scheduler/version_check.py 的「GitHub 有新版」快取 key——這裡的開機重驗一起清
_VERSION_AVAILABLE_KEY = "_update_available"


class UpdatePolicy(Enum):
    NONE = "none"  # 本機版本 == manifest 版本，沒有更新
    GENERAL = "general"  # 一般更新：下載後全站橫幅＋提醒視窗，使用者按下才重啟套用
    IMPORTANT = "important"  # 重點更新：mandatory=true 或本機版本 < minimum_required_version，鎖網頁 UI


def _numeric_tail(version: str) -> str:
    """版本字串最後一個點分段的數字部分（去掉 `-dev` 之類後綴）。"""
    return version.split("-", 1)[0].split(".")[-1]


def is_minor_version(version: str) -> bool:
    """`X.Y.ZZ`（patch 為兩位以上數字）＝「小更新」，只是橫幅／提醒視窗的文案標籤，
    行為跟一般更新完全一樣（一樣要重啟）。`1.2.10` → True、`1.2.9` → False。"""
    tail = _numeric_tail(version)
    return tail.isdigit() and len(tail) >= 2


def _parse_version(version: str) -> tuple[int, ...]:
    """`(1, 2, 0) < (1, 3, 0)` 這種簡單的數字 tuple 比較，前提是版本號是純數字
    `MAJOR.MINOR.PATCH` 格式。`bahaad._version.__version__` 自 v0.0.1 起就是乾淨的
    `X.Y.Z`（不帶前綴/後綴）；仍保留「切掉第一個 `-` 之後」的寬鬆處理，容忍 tag 帶
    `-beta.1` 這類 pre-release 後綴（見 docs/decisions/0001「Beta 期間版本方案」）。"""
    numeric_part = version.lstrip("vV").split("-", 1)[0]
    return tuple(int(part) for part in numeric_part.split("."))


def _contains_hash_mismatch(exc: BaseException) -> bool:
    """跟 diagnostics/codes.py 的 `classify_connection_error()` 同樣的鏈式走法，找
    `HashMismatchError` 而不是連線例外型別——見 diagnostics.md「誰呼叫 report()」。"""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, HashMismatchError):
            return True
        current = current.__cause__ or current.__context__
    return False


def classify(manifest: UpdateManifest, current_version: str) -> UpdatePolicy:
    """純函式，不做任何 I/O，方便測試。版本比較沿用 scheduler/version_check.py 已經
    定案的做法——字串不相等就視為有差異，不做語意化版本大小比較；`minimum_required_
    version` 的比較例外：這個一定要能判斷大小，用 `_parse_version()`。"""
    # 已經是 manifest 描述的版本了 → 沒有要更新的東西。**這一條一定要在 mandatory /
    # minimum_required 之前**：不然 `mandatory=true` 的更新在套用完、重啟成新版之後，
    # `classify()` 還是會回 IMPORTANT，網頁 UI 就永遠鎖著（2026-09-08 強制更新 e2e
    # 實測抓到）。你不可能被強制「更新到你已經在跑的版本」。
    if manifest.version == current_version:
        return UpdatePolicy.NONE

    if manifest.mandatory:
        return UpdatePolicy.IMPORTANT

    try:
        if _parse_version(current_version) < _parse_version(manifest.minimum_required_version):
            return UpdatePolicy.IMPORTANT
    except ValueError:
        # 版本字串不是純數字格式（超出目前約定的解析能力），保守起見不視為強制更新
        pass

    return UpdatePolicy.GENERAL


def revalidate_update_flags(settings: SettingsStore, current_version: str) -> None:
    """程式啟動時呼叫一次（純本機、不碰網路），清掉已經不再適用的更新旗標。

    `_pending_update`（差異檔已下載好）跟 `_update_available`（GitHub 有新版）都是寫進
    `SettingsStore` 的執行期快取。套用更新到新版、或使用者自己換了 exe 之後，新版程式
    啟動時這些旗標還留著——要等 `UpdateCoordinator.check_once()` / `VersionChecker.
    check_once()` 下一輪成功連上網路才會被清掉（最多一小時）。那段空窗期系統匣會顯示
    「套用更新並重新啟動」、網頁橫幅顯示「有更新可套用」，指向的卻是**你已經在跑的
    版本**（使用者 2026-09-08：「明明沒下載也沒檢查更新卻出現這按鈕」）。

    這裡在開機時先比一次版本號：旗標指的版本 <= 目前版本＝已經是這一版或更舊，直接
    清掉。版本字串不是純數字時保守處理，只在字串完全相等時才清。"""
    for key in (_SETTINGS_KEY, _VERSION_AVAILABLE_KEY):
        entry = settings.get(key)
        if not isinstance(entry, dict):
            continue
        flagged_version = entry.get("version") or entry.get("latest_version")
        if not flagged_version:
            continue
        flagged_version = str(flagged_version)
        try:
            stale = _parse_version(flagged_version) <= _parse_version(current_version)
        except ValueError:
            stale = _strip_prefix(flagged_version) == _strip_prefix(current_version)
        if stale:
            settings.reset([key])
            logger.info(
                "開機清掉過期的更新旗標 %s（旗標版本 %s ≤ 目前 %s）",
                key, flagged_version, current_version,
            )


def _strip_prefix(version: str) -> str:
    return version[1:] if version[:1] in ("v", "V") else version


class UpdateCoordinator:
    def __init__(
        self,
        settings: SettingsStore,
        http: HttpGetter,
        current_version: str,
        staging_dir: Path,
        install_root: Path | None = None,
        diagnostics: DiagnosticsReporter | None = None,
        on_update_ready=None,
    ) -> None:
        self._settings = settings
        self._http = http
        self._current_version = current_version
        self._staging_dir = Path(staging_dir)
        # `install_root` 自 2026-08-28 起沒有用途（原本給已移除的 core=false 靜默套用）；
        # 保留參數避免動到 app_shell.py 的組裝與既有測試，套用更新一律走小幫手行程。
        self._install_root = Path(install_root) if install_root is not None else None
        self._diagnostics = diagnostics
        # `on_update_ready(version: str, policy_value: str)`：差異檔剛下載完、`_pending_update`
        # 從「這個版本還沒下載」變成「已就緒」時呼叫一次（app_shell 掛系統匣氣泡通知
        # ——使用者 2026-09-08：「好歹也提示說有更新」）。同一版本只通知一次。
        self._on_update_ready = on_update_ready
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def _report(self, error_code: ErrorCode, exc: BaseException, latency_ms: float, **extra) -> None:
        if self._diagnostics is None:
            return
        self._diagnostics.report(
            error_code,
            OperationType.APPLY_UPDATE,
            connection_status=classify_connection_error(exc),
            network_latency_ms=latency_ms,
            **extra,
        )

    def check_once(self) -> UpdatePolicy | None:
        """跑一次檢查。NONE（本機已是最新）時清掉 `_pending_update`；GENERAL／IMPORTANT
        時下載整批檔案到暫存目錄後停手，寫進 `_pending_update` 讓 base.html 的全站橫幅
        跟提醒視窗顯示。查詢/下載失敗都記警告、這輪跳過，等下一輪——更新機制不是關鍵
        路徑功能。既有 except 分支各自補一次診斷回報（Phase 7，`diagnostics` 可選依賴，
        `None` 時完全不影響原本行為），見 docs/requirements/diagnostics.md。"""
        started_at = time.monotonic()
        try:
            manifest = fetch_manifest(self._http)
        except ManifestUnavailableError as exc:
            # 差異更新分支還沒發布（整個 Beta 期間都是這個狀態）——這不是故障，記 debug
            # 就好（logger 等級是 INFO，等於不寫檔案 log／不進日誌頁），也不回報診斷。
            logger.debug("updater 略過本輪：%s", exc)
            return None
        except Exception as exc:
            logger.warning("updater 查詢 manifest 失敗：%s", exc)
            error_code = (
                ErrorCode.UPDATE_MANIFEST_SIGNATURE_INVALID
                if isinstance(exc, ManifestSignatureError)
                else ErrorCode.UPDATE_MANIFEST_FETCH_FAILED
            )
            self._report(error_code, exc, (time.monotonic() - started_at) * 1000)
            return None

        policy = classify(manifest, self._current_version)

        if policy is UpdatePolicy.NONE:
            self._settings.reset([_SETTINGS_KEY])
            return policy

        # 已經下載過「同一版、同一種更新等級」、而且暫存區的檔案還在 → 不用每輪重抓
        # 整批檔案（1 小時一輪，重抓完整 dist 會很浪費）。版本或等級變了（維護者重發、
        # 或把 general 升成 mandatory）、或暫存檔不見了（套用失敗、被外部清掉）才往下
        # 重抓、更新旗標。
        pending = self._settings.get(_SETTINGS_KEY)
        if (
            pending
            and pending.get("version") == manifest.version
            and pending.get("policy") == policy.value
            and all((self._staging_dir / entry.path).is_file() for entry in manifest.files)
        ):
            return policy

        max_retries = self._settings.get("max_retries", _DEFAULT_MAX_RETRIES)
        started_at = time.monotonic()
        try:
            fetch_files(self._http, manifest.files, self._staging_dir, max_retries)
        except Exception as exc:
            logger.exception("updater 下載更新檔案失敗")
            integrity = FileIntegrityResult.MISMATCH if _contains_hash_mismatch(exc) else None
            self._report(
                ErrorCode.UPDATE_FILE_FETCH_FAILED,
                exc,
                (time.monotonic() - started_at) * 1000,
                file_integrity_result=integrity,
            )
            return None

        prev = self._settings.get(_SETTINGS_KEY) or {}
        newly_ready = prev.get("version") != manifest.version
        self._settings.update(
            {
                _SETTINGS_KEY: {
                    "policy": policy.value,
                    "version": manifest.version,
                    "notes": manifest.notes,
                    "minor": is_minor_version(manifest.version),
                }
            }
        )
        if newly_ready and self._on_update_ready is not None:
            try:
                self._on_update_ready(manifest.version, policy.value)
            except Exception:  # noqa: BLE001 - 通知失敗不影響更新已下載好這個事實
                logger.debug("on_update_ready 回呼發生例外", exc_info=True)
        return policy

    def set_update_ready_callback(self, fn) -> None:
        """`app_shell` 在系統匣圖示建好之後才呼叫這個掛上通知回呼（建構這個物件的時候
        圖示還不存在）。"""
        self._on_update_ready = fn

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
                logger.exception("updater 這一輪檢查發生未預期的例外")
            interval_seconds = (
                self._settings.get("update_check_interval_hours", _DEFAULT_INTERVAL_HOURS) * 3600
            )
            self._stop_event.wait(interval_seconds)
