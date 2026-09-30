"""新番快訊（New Anime Bulletin）——抓動畫瘋每季的「新番節目資訊」GNN 文章，整理成
可追蹤的清單。規格見 docs/requirements/new_anime_bulletin.md。

跟 gossip／catalog／anime_cache 完全平行：獨立的抓取器（無 cookie）、獨立的資料表
（`store/newanime_cache.py`）、獨立的 route（`/newanime`）。
"""
