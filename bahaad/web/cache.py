"""本地圖片快取的服務端點＋Jinja filter。規格見 docs/requirements/anime_cache.md。

- `GET /cache/img/<url_hash>`：磁碟上有這個檔 → 直接回（200，長期快取）；還沒抓 →
  **不阻塞、不 302**：補排進背景抓取佇列、回一張中性佔位圖（`no-store`，瀏覽器之後
  會重新請求），前端 `img_retry.js` 過幾秒也會自己重抓一次。帳上沒有這個 hash → 404。
- `cached_img(url)` filter：樣板把 `cover_url` 換成 `/cache/img/<hash>`。實際登記／排隊
  由各 route handler render 完之後**批次**做（`AnimeCacheStore.register_images()` 一個交易），
  filter 本身純算 URL、不碰 DB。

**這個端點絕對不能阻塞**：一頁 ~60 張封面同時打進來，只要 handler 卡住等網路（舊版
曾經在這裡同步抓圖），waitress 的 worker thread 很快被佔滿 → 後續連線被 RST
（使用者 2026-09-09：`net::ERR_CONNECTION_RESET` on /cache/img）。抓圖一律丟背景佇列。
"""

from __future__ import annotations

import logging

from flask import Blueprint, Response, abort, current_app, send_file

from bahaad.store.anime_cache import url_hash

logger = logging.getLogger(__name__)

cache_bp = Blueprint("cache", __name__)

_MAX_AGE_SECONDS = 90 * 86400

# 1x1 全透明 PNG（67 bytes，標準）——還沒抓好的封面先回這個，瀏覽器不會顯示破圖圖示。
# `Cache-Control: no-store` 讓瀏覽器下次進頁／img_retry.js 重抓時不吃到這張佔位。
_PLACEHOLDER_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c626001000000ffff03000006000557bfabd4000000"
    "0049454e44ae426082"
)


def _placeholder() -> Response:
    resp = Response(_PLACEHOLDER_PNG, mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@cache_bp.route("/cache/img/<url_hash>")
def image(url_hash: str):  # noqa: A002 - route 參數就叫這個名字最直白
    deps = current_app.config["DEPS"]
    store = getattr(deps, "anime_cache", None)
    fetcher = getattr(deps, "image_fetcher", None)
    if store is None:
        abort(404)

    rec = store.image_record(url_hash)

    # DB 查不到這一列（被清過／`register_images` 還沒寫進去／DB 鎖 race）但磁碟檔還在
    # ——檔名就是 `url_hash`，直接送，不要白白 404。
    if rec is None:
        if fetcher is not None:
            guessed = fetcher.path_for_hash(url_hash)
            if guessed.is_file():
                return send_file(guessed, mimetype="image/jpeg", max_age=_MAX_AGE_SECONDS)
        abort(404)

    # **磁碟上有這個檔就直接送**——`fetched_at` 只是「已抓過」的提示旗標，磁碟檔才是
    # 真相（DB 被重置／prune、或 `record_image` 曾因 DB 鎖失敗時，檔案還在但 fetched_at
    # 是 NULL）。順手回填 fetched_at。
    if fetcher is not None and fetcher.has_local(rec["url"]):
        path = fetcher.path_for(rec["url"])
        if not rec["fetched_at"]:
            try:
                store.record_image(rec["url"], rec.get("content_type"), path.stat().st_size)
            except Exception:  # noqa: BLE001 - 回填失敗不影響這次能不能送圖
                logger.debug("回填 fetched_at 失敗：%s", rec["url"], exc_info=True)
        return send_file(
            path, mimetype=rec.get("content_type") or "image/jpeg", max_age=_MAX_AGE_SECONDS
        )

    # 還沒抓 → 丟背景佇列、立刻回佔位圖（不阻塞、不 302）。
    if fetcher is not None:
        fetcher.enqueue(rec["url"])
    return _placeholder()


def cached_img_url(url: str) -> str:
    """`cover_url` → `/cache/img/<hash>`。快取沒開（或非 http URL）就原樣回傳。

    前提：呼叫這個 filter 的頁面，其 route handler render 之前要先
    `deps.anime_cache.register_images([...這些 cover_url...])`——不然
    `/cache/img/<hash>` 會 404。用 `web/anime_data.py` 的 helper 就會自動做。"""
    if not url or not str(url).startswith(("http://", "https://")):
        return url or ""
    try:
        deps = current_app.config["DEPS"]
    except (RuntimeError, KeyError):
        return url
    if getattr(deps, "anime_cache", None) is None:
        return url
    return f"/cache/img/{url_hash(url)}"
