"""自訂 JA3 指紋的 curl_cffi 相容性檢查。

環境偽裝（見 docs/requirements/gamer_login_setup.md）讓使用者用真實瀏覽器採集的 JA3
指紋取代 curl_cffi 內建的罐頭指紋。真實 Chrome 的 JA3 常帶 curl_cffi 底層的
curl-impersonate 套不下去的 TLS 擴展編號（GREASE、較新的擴展如 encrypted_client_hello
等）——`set_ja3_options()` 本身不會炸（它只是 `setopt(TLS_EXTENSION_ORDER, ...)`），
但真的握手時 libcurl 會回 `curl: (35) Invalid TLS extension order`，讓**所有**對動畫瘋
的請求跟著壞掉。

所以 `validate_ja3()` **實際發一次請求**試試看——一組拿不去用的 JA3，寧可只用 UA、
退回內建 TLS 指紋。指紋採集本來就是重量級操作（開了一個瀏覽器），多一個請求無妨。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 拿來試 JA3 的目標——公開、穩定、回應小，跟動畫瘋無關
_PROBE_URL = "https://www.google.com/generate_204"
_PROBE_TIMEOUT = 8


def validate_ja3(ja3: str, akamai: str = "") -> bool:
    """實際用這組 JA3（＋ akamai，若有）發一次請求，成功才回 True。空字串／握手失敗／
    curl_cffi 版本不支援都回 False（保守——寧可退回內建指紋，也不要讓所有請求壞掉）。"""
    if not ja3:
        return False
    try:
        from curl_cffi import requests as curl_requests
    except Exception as exc:  # noqa: BLE001
        logger.warning("無法載入 curl_cffi，改用內建指紋：%s", exc)
        return False

    kwargs = {"ja3": ja3}
    if akamai:
        kwargs["akamai"] = akamai
    try:
        with curl_requests.Session(impersonate="chrome", **kwargs) as session:
            session.get(_PROBE_URL, timeout=_PROBE_TIMEOUT)
        return True
    except Exception as exc:  # noqa: BLE001 - CurlError（curl:(35) 等）／連線錯誤／
        # 版本差異都保守處理：一組拿不去用的 JA3 會讓所有請求壞掉，寧可退回內建指紋
        logger.warning("採集到的 JA3 無法套用（%s），改用內建 TLS 指紋", exc)
        return False
