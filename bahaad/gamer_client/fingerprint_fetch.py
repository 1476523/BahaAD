"""環境偽裝指紋（UA / JA3 / Akamai）採集。規格見 docs/requirements/gamer_login_setup.md。

開一個獨立、全新暫存設定檔的**有頭**瀏覽器視窗（開在畫面外、使用者看不到），導到
https://ja3.zone/check，透過 CDP 讀出頁面上量測到的 JA3 / Akamai / User-Agent，寫回
IdentityStore（DPAPI 加密），抓完自動關閉瀏覽器。全程不需使用者操作。

**必須是有頭瀏覽器**：headless Chrome 的 TLS ClientHello 指紋本身就跟一般瀏覽器不同、
會被辨識出來，採集到的指紋若來自 headless 就失去意義。**但沒必要讓使用者看到**——
用 `--window-position=-32000,-32000` 開在畫面外（off-screen 的視窗照樣正常算圖／跑 JS，
跟「最小化」不同），避免首次設定後突然彈一個視窗影響體驗。登入流程
（`cookie_rotation.py`）另當別論——那個要讓使用者看到並完成 CAPTCHA，不能藏。

設計沿用舊專案 aniGamerPlus `Dashboard/quick_fetch.py` 的 fingerprint 模式（使用者已同意
「環境偽裝設定」這項可以參考舊專案，見 docs/custom-features.md 第 9 項），重用
cookie_rotation.py 已經寫好、也實測過的瀏覽器啟動 / CDP 連線零件。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from typing import Callable

from bahaad.gamer_client.cookie_rotation import (
    CdpLike,
    ProcessLike,
    _default_connect_cdp,
    _find_free_port,
    _terminate,
    find_browser,
)
from bahaad.gamer_client.ja3_compat import validate_ja3

_FINGERPRINT_URL = "https://ja3.zone/check"
_DEVTOOLS_READY_TIMEOUT_SECONDS = 15
_SCRAPE_WAIT_TIMEOUT_SECONDS = 30
_POLL_INTERVAL_SECONDS = 0.5

# ja3.zone/check 把量測結果放在多個 <textarea>：aria-label="Raw" 的第 1 個是 JA3、
# 第 4 個是 Akamai HTTP/2 指紋；UA 是那之後第一個沒有 aria-label 的 textarea。
# DOM 結構沿用舊專案觀察，站方改版可能要調整。
_SCRAPE_JS = """
(function(){
    var raws = Array.prototype.slice.call(document.querySelectorAll('textarea[aria-label="Raw"]'));
    var ja3 = raws[0] ? raws[0].value : '';
    var akamai = raws[3] ? raws[3].value : '';
    var allTextareas = Array.prototype.slice.call(document.querySelectorAll('textarea'));
    var lastRawIndex = allTextareas.indexOf(raws[3]);
    var ua = '';
    for (var i = lastRawIndex + 1; i < allTextareas.length; i++) {
        if (!allTextareas[i].getAttribute('aria-label')) { ua = allTextareas[i].value; break; }
    }
    return JSON.stringify({ja3: ja3, akamai: akamai, ua: ua});
})();
"""

_HAS_RESULT_JS = (
    'document.querySelectorAll(\'textarea[aria-label="Raw"]\').length >= 4 '
    '&& !!document.querySelector(\'textarea[aria-label="Raw"]\').value'
)


class FingerprintFetchError(Exception):
    pass


class FingerprintFetcher:
    def __init__(
        self,
        browser_path: str | None = None,
        launch_browser: Callable[[list[str]], ProcessLike] = subprocess.Popen,
        connect_cdp: Callable[[int, float], CdpLike] = _default_connect_cdp,
        scrape_timeout: float = _SCRAPE_WAIT_TIMEOUT_SECONDS,
    ) -> None:
        self._browser_path = browser_path
        self._launch_browser = launch_browser
        self._connect_cdp = connect_cdp
        self._scrape_timeout = scrape_timeout

    def fetch(self) -> dict[str, str]:
        """回傳 {"ua": ..., "ja3": ..., "akamai": ...}。JA3 curl_cffi 這版套不下去時
        回傳空字串（只用 UA + 內建 TLS 指紋）。抓不到內容就丟 FingerprintFetchError。"""
        browser_path = self._browser_path or find_browser()
        port = _find_free_port()
        user_data_dir = tempfile.mkdtemp(prefix="bahaad_fingerprint_")

        process = self._launch_browser(
            [
                browser_path,
                f"--remote-debugging-port={port}",
                f"--remote-allow-origins=http://127.0.0.1:{port}",
                f"--user-data-dir={user_data_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--new-window",
                "--incognito",
                # 開在畫面外——指紋採集必須是「有頭」瀏覽器（headless 的 TLS 指紋會被
                # 辨識），但沒必要讓使用者看到視窗突然彈出來。off-screen 的視窗 Chrome
                # 一樣正常算圖、跑 JS（跟「最小化」不同，最小化可能被節流），ja3.zone
                # 照樣量測得到。登入流程（cookie_rotation.py）另當別論——那個要讓使用者
                # 看到並完成 CAPTCHA，不能藏。
                "--window-position=-32000,-32000",
                "--window-size=1,1",
                _FINGERPRINT_URL,
            ]
        )

        cdp: CdpLike | None = None
        try:
            cdp = self._connect_cdp(port, _DEVTOOLS_READY_TIMEOUT_SECONDS)
            if not self._wait_for(cdp, process, _HAS_RESULT_JS, self._scrape_timeout):
                raise FingerprintFetchError("ja3.zone 沒有在時間內回傳量測結果")

            raw = cdp.evaluate(_SCRAPE_JS)
            try:
                parsed = json.loads(raw) if raw else {}
            except (TypeError, ValueError) as exc:
                raise FingerprintFetchError(f"解析指紋頁面內容失敗：{exc}") from exc

            ua = (parsed.get("ua") or "").strip()
            ja3 = (parsed.get("ja3") or "").strip()
            akamai = (parsed.get("akamai") or "").strip()
            if not (ua and ja3 and akamai):
                raise FingerprintFetchError("指紋頁面內容不完整（ua/ja3/akamai 有缺）")

            # 採集到的 JA3 curl_cffi 這版套不下去的話（真實 Chrome 常帶 GREASE／較新的
            # TLS 擴展、握手時 libcurl 會回 curl:(35)），實際發一次請求試試看，不行就
            # 只用 UA、退回內建 TLS 指紋，不要拿一組會讓所有請求壞掉的 JA3。
            if not validate_ja3(ja3, akamai):
                ja3 = ""
            return {"ua": ua, "ja3": ja3, "akamai": akamai}
        finally:
            if cdp is not None:
                cdp.close()
            _terminate(process)
            shutil.rmtree(user_data_dir, ignore_errors=True)

    def _wait_for(
        self, cdp: CdpLike, process: ProcessLike, js_expr: str, timeout: float
    ) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if process.poll() is not None:
                return False
            if cdp.evaluate(js_expr, timeout=3):
                return True
            time.sleep(_POLL_INTERVAL_SECONDS)
        return False
