"""設定類表單端點的共用回應（round 7 第 5 項）。

同一個端點要同時服務兩種呼叫端：
- 有 JS：`static/ajax_forms.js` 用 fetch 送出、帶 `X-Requested-With: fetch`，期待
  JSON `{ok, message}`，收到後彈 toast、**不重載頁面**。
- 沒 JS：一般表單送出，端點 `flash()` + `redirect()` 回原頁（既有行為，當退路）。
"""

from __future__ import annotations

from flask import Response, flash, jsonify, redirect, request, url_for


def wants_json() -> bool:
    return request.headers.get("X-Requested-With") == "fetch"


def form_result(
    message: str, *, endpoint: str, ok: bool = True, json_extra: dict | None = None, **redirect_kwargs
) -> Response:
    """`ok=True` → 成功；`ok=False` → 驗證失敗等（JS 端彈錯誤 toast、無 JS 一樣 flash）。
    `endpoint` / `redirect_kwargs` 是無 JS 時 `redirect(url_for(endpoint, **kwargs))` 用的。
    `json_extra` 在有 JS 時併進回應 JSON（例：`{"restart": True}` 讓前端顯示重啟提示）。"""
    if wants_json():
        payload = {"ok": ok, "message": message}
        if json_extra:
            payload.update(json_extra)
        return jsonify(payload)
    flash(message)
    return redirect(url_for(endpoint, **redirect_kwargs))
