"""安裝實例 ↔ 中繼之間的 HMAC 簽章。規格見 docs/requirements/subscriber_notify.md。

刻意跟 `bahaad/access_gate_server/subscriber_relay.py` 各自維護一份一樣的 HMAC 邏輯，
不共用 import——`access_gate_server/` 是獨立部署、不隨 `main.exe` 散布的服務（見該
套件 `__init__.py`），`app_shell.py`／`main.py` 不能 import 那個套件底下任何東西。
這支函式很小，重複維護的成本遠低於打破那條隔離界線。
"""

from __future__ import annotations

import hashlib
import hmac


def sign_payload(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    if not signature:
        return False
    expected = sign_payload(secret, body)
    return hmac.compare_digest(expected, signature)
