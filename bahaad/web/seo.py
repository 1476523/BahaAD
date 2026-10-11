"""公開模式的 SEO：`robots.txt`、`sitemap.xml`、每頁 meta 標籤用的 context。

使用者 2026-10-10：公開模式與各個番劇頁要有 SEO。原則：

- 只有公開模式放行、而且內容對訪客有意義的頁面才讓搜尋引擎索引（番劇列表、排行榜、
  新番快訊、各部公開番劇頁）；登入／訂閱者登入／無快取／未開放等頁面一律 `noindex`。
- 絕對網址以設定裡的「公開網域」（`public_domain`）為準；沒設就退回這次請求的網址。
- sitemap 只列「公開、且已經有本地快取」的番劇（跟訪客在番劇列表看得到的一致），
  不會為了產生 sitemap 去打站方。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from flask import Blueprint, Response, current_app, g, request

seo_bp = Blueprint("seo", __name__)

SITE_NAME = "BahaAD"
DEFAULT_DESCRIPTION = "BahaAD 番劇列表：本季追番、已收錄的動畫瘋番劇資訊、集數與評分一覽。"
_INDEXABLE_ENDPOINTS = {
    "browse.subscriptions",
    "browse.anime_detail",
    "stats.leaderboard_page",
    "newanime.index",
    "newanime.detail",
}
_PRIVATE_DISALLOW = (
    "/settings", "/login", "/logout", "/setup", "/forgot", "/reset", "/gamer", "/manual",
    "/downloads", "/notify", "/notifications", "/schedule", "/gossip", "/logs", "/dashboard",
    "/ui", "/subscriber", "/play", "/api",
)


def site_base_url(deps) -> str:
    domain = (deps.settings.get("public_domain", "") or "").strip().rstrip("/")
    if domain and not re.match(r"^https?://", domain):
        domain = "https://" + domain
    return domain or request.url_root.rstrip("/")


def clip_description(text: str | None, limit: int = 150) -> str:
    flat = re.sub(r"\s+", " ", text or "").strip()
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def seo_context() -> dict:
    """給 base.html 用的預設 meta：公開模式的可索引頁才 `index`，其餘 `noindex`。"""
    deps = current_app.config.get("DEPS")
    indexable = bool(
        getattr(g, "public_readonly", False)
        and request.endpoint in _INDEXABLE_ENDPOINTS
        and deps is not None
        and deps.settings.get("public_mode", False)
    )
    if not indexable:
        return {"seo_indexable": False}
    base = site_base_url(deps)
    return {
        "seo_indexable": True,
        "seo_site_name": SITE_NAME,
        "seo_description": DEFAULT_DESCRIPTION,
        "seo_canonical": base + request.path,
        "seo_base": base,
    }


def anime_seo(detail, display_title: str, synopsis: str | None, cover_path: str) -> dict | None:
    """番劇頁的 meta：簡介（截 150 字）、封面絕對網址、schema.org TVSeries／Movie JSON-LD。
    不是可索引頁（非公開模式）回 None。"""
    ctx = seo_context()
    if not ctx.get("seo_indexable"):
        return None
    base = ctx["seo_base"]
    description = clip_description(synopsis) or f"《{display_title}》番劇資訊、集數與評分。"
    image = cover_path if cover_path.startswith("http") else (base + cover_path if cover_path else "")
    genres = [g_ for g_ in (getattr(detail, "genres", None) or []) if g_]
    count = 0
    for cat in getattr(detail, "episode_categories", None) or []:
        count += len(getattr(cat, "episodes", None) or [])
    ld: dict = {
        "@context": "https://schema.org",
        "@type": "TVSeries",
        "name": display_title,
        "description": description,
        "url": ctx["seo_canonical"],
        "inLanguage": "ja",
    }
    if image:
        ld["image"] = image
    if genres:
        ld["genre"] = genres
    if count:
        ld["numberOfEpisodes"] = count
    score = str(getattr(detail, "rating_score", "") or "").strip()
    votes = re.sub(r"[^\d]", "", str(getattr(detail, "rating_count", "") or ""))
    try:
        if score and votes and float(score) > 0 and int(votes) > 0:
            ld["aggregateRating"] = {
                "@type": "AggregateRating",
                "ratingValue": float(score),
                "bestRating": 5,
                "ratingCount": int(votes),
            }
    except ValueError:
        pass
    return {"description": description, "image": image, "jsonld": ld}


def _public_sns(deps) -> list[int]:
    from bahaad.web.anime_data import anime_detail_data
    from bahaad.web.browse import _public_title_allowed
    from bahaad.web.settings import _DEFAULT_ANIME_CACHE_TTL_DAYS

    ttl_days = int(deps.settings.get("anime_cache_ttl_days", _DEFAULT_ANIME_CACHE_TTL_DAYS))
    sns: list[int] = []
    for entry in deps.schedule_store.get_entries().values():
        if entry.schedule_weekday is None or getattr(entry, "inactive", False):
            continue
        detail, _err = anime_detail_data(deps, entry.sn, ttl_days=ttl_days, cache_only=True)
        if detail is None or not _public_title_allowed(deps.settings, detail.title):
            continue
        sns.append(int(entry.sn))
    return sns


@seo_bp.route("/robots.txt")
def robots_txt():
    deps = current_app.config["DEPS"]
    if not deps.settings.get("public_mode", False) or deps.settings.get("_access_gate_blocked"):
        body = "User-agent: *\nDisallow: /\n"
    else:
        lines = ["User-agent: *", "Allow: /anime/", "Allow: /browse/subscriptions"]
        lines += [f"Disallow: {p}" for p in _PRIVATE_DISALLOW]
        lines += ["", f"Sitemap: {site_base_url(deps)}/sitemap.xml"]
        body = "\n".join(lines) + "\n"
    resp = Response(body, mimetype="text/plain")
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@seo_bp.route("/sitemap.xml")
def sitemap_xml():
    deps = current_app.config["DEPS"]
    if not deps.settings.get("public_mode", False) or deps.settings.get("_access_gate_blocked"):
        return Response("", status=404)
    base = site_base_url(deps)
    today = datetime.now(timezone.utc).date().isoformat()
    urls = [(f"{base}/browse/subscriptions", "daily"), (f"{base}/leaderboard", "daily")]
    urls += [(f"{base}/anime/{sn}", "weekly") for sn in _public_sns(deps)]
    items = "".join(
        f"<url><loc>{escape(loc)}</loc><lastmod>{today}</lastmod><changefreq>{freq}</changefreq></url>"
        for loc, freq in urls
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</urlset>'
    )
    resp = Response(xml, mimetype="application/xml")
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp
