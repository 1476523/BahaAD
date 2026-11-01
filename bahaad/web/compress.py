"""回應壓縮＋靜態檔精簡（PageSpeed：載入優化，使用者 2026-10-10）。

- HTML／CSS／JS／JSON／XML 回應用 gzip 壓縮（瀏覽器有送 `Accept-Encoding: gzip` 才壓；
  Cloudflare 之類的邊緣若已壓過就不會重複，因為會看到 `Content-Encoding`）。
- 靜態檔（`direct_passthrough`）依「路徑＋修改時間」把壓縮後的內容記在記憶體，不用每次重壓。
  CSS 另外先去掉註解與多餘空白（不碰字串內容）。
- 串流／分段（Range）／已壓縮／小於 1 KB 的回應原樣放行，播放與下載不受影響。
"""

from __future__ import annotations

import gzip
import os
import re
import threading

from flask import Flask, request

_COMPRESSIBLE = ("text/html", "text/css", "text/plain", "text/xml", "application/javascript",
                 "text/javascript", "application/json", "application/xml", "application/ld+json",
                 "image/svg+xml")
_MIN_SIZE = 1024
_static_cache: dict[tuple[str, float, bool], bytes] = {}
_lock = threading.Lock()


def minify_css(css: str) -> str:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    css = re.sub(r"\s+", " ", css)
    css = re.sub(r"\s*([{}])\s*", r"\1", css)
    return css.strip()


def _wants_gzip() -> bool:
    return "gzip" in (request.headers.get("Accept-Encoding", "") or "").lower()


def init_compression(app: Flask) -> None:
    @app.after_request
    def _compress(response):  # noqa: ANN001
        try:
            if (
                response.status_code != 200
                or "Content-Encoding" in response.headers
                or request.headers.get("Range")
                or not _wants_gzip()
            ):
                return response
            mimetype = (response.mimetype or "").lower()
            if mimetype not in _COMPRESSIBLE:
                return response
            if response.direct_passthrough:
                return _compress_static(app, response, mimetype)
            if response.is_streamed:
                return response
            data = response.get_data()
            if len(data) < _MIN_SIZE:
                return response
            _apply(response, gzip.compress(data, compresslevel=5))
        except Exception:  # noqa: BLE001 - 壓縮失敗就回原樣，不能弄壞頁面
            pass
        return response


def _apply(response, body: bytes) -> None:
    response.direct_passthrough = False
    response.set_data(body)
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Length"] = str(len(body))
    response.headers.add("Vary", "Accept-Encoding")
    response.headers.pop("ETag", None)  # 內容變了，原本檔案的 ETag 不能沿用


def _compress_static(app: Flask, response, mimetype: str):  # noqa: ANN001
    if request.endpoint != "static" or not app.static_folder:
        return response
    rel = request.view_args.get("filename", "") if request.view_args else ""
    path = os.path.realpath(os.path.join(app.static_folder, rel))
    if not path.startswith(os.path.realpath(app.static_folder) + os.sep) or not os.path.isfile(path):
        return response
    mtime = os.path.getmtime(path)
    key = (path, mtime, mimetype == "text/css")
    with _lock:
        body = _static_cache.get(key)
    if body is None:
        with open(path, "rb") as fh:
            raw = fh.read()
        if mimetype == "text/css":
            raw = minify_css(raw.decode("utf-8")).encode("utf-8")
        if len(raw) < _MIN_SIZE:
            return response
        body = gzip.compress(raw, compresslevel=9)
        with _lock:
            _static_cache[key] = body
    response.response.close() if hasattr(response.response, "close") else None
    _apply(response, body)
    return response
