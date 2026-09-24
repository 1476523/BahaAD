"""番劇資料本地快取。規格見 docs/requirements/anime_cache.md。

- `bahaad/store/anime_cache.py`：持久化（表在 bahaad.db）。
- `home_policy.py`：純函式——開首頁時「用快取還是重抓」的決策。
- `images.py`：`ImageCacheFetcher`，佇列＋背景執行緒把圖片 bytes 抓到本地檔。
"""

CACHE_IMAGES_DIRNAME = "cache/images"
