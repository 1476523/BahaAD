"""youranimes.tw（你的動畫）季度頁解析出來的資料模型。規格見 docs/requirements/youranimes.md。

全部 frozen dataclass、缺值一律空字串／空 tuple（比照 gamer_client/browse.py 的
「解析不到就給空、不 raise」風格）。tuple 而非 list：這些物件會被存進 DB 又讀回來、
在 request 內傳給模板，不可變比較安全。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class YourAnimesStaff:
    """製作陣容的一列：一個職稱對到一到多個人名。"""

    job: str
    names: tuple[str, ...] = ()


@dataclass(frozen=True)
class YourAnimesCastMember:
    """配音的一列：角色 → 聲優。"""

    character: str
    actor: str


@dataclass(frozen=True)
class YourAnimesMusic:
    """音樂的一列：片頭曲／片尾曲／插入曲 → 曲名（保留「」）＋演唱者／團體。"""

    slot: str
    song: str
    artist: str = ""


@dataclass(frozen=True)
class YourAnimesRecord:
    """季度頁裡一部番劇的完整解析結果。"""

    anime_id: int
    zh_title: str
    jp_title: str = ""
    # 標題明確寫出的季別數字（`gossip_watch.season_number`）；沒寫回 None＝視為第一季
    season_number: int | None = None
    synopsis: str = ""
    staff: tuple[YourAnimesStaff, ...] = field(default_factory=tuple)
    cast: tuple[YourAnimesCastMember, ...] = field(default_factory=tuple)
    music: tuple[YourAnimesMusic, ...] = field(default_factory=tuple)
    season_slug: str = ""
