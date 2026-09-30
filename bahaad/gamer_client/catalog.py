"""番劇/集數中繼資料查詢。規格見 docs/requirements/gamer_client_catalog.md。

呼叫 api.gamer.com.tw/anime/v1/video.php，把回應攤平成結構化的 VideoInfo，是排程邏輯
（scheduler/）跟播放清單邏輯（playlist.py）之間唯一的中繼資料來源，兩邊都不用自己重新
解析原始回應格式。欄位依據 docs/api-observations/anime-page.md 的觀察。

原始回應把集數依「季度分組」放在 anime.episodes（一個 dict），目前只觀察過單季作品，
分組的意義還沒搞懂，這裡對外一律攤平成單一列表，不保留分組結構。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bahaad.gamer_client.session import GamerSession
from bahaad.gamer_client.titles import clean_anime_title, is_special_episode_title

_VIDEO_INFO_URL = "https://api.gamer.com.tw/anime/v1/video.php"

# 動畫瘋登入身分的持久 cookie——有這個就代表「這個 session 曾經登入過某個帳號」。
# 用來把「拿不到權限」分成「登入態失效」跟「這帳號本來就沒 VIP」兩種。
_LOGIN_ID_COOKIE = "BAHAID"


def _session_has_login(session) -> bool:
    try:
        return bool(session.current_cookies().get(_LOGIN_ID_COOKIE))
    except Exception:  # noqa: BLE001 - session 型別不對／jar 讀失敗都當「不確定＝沒登入」
        return False


class CatalogError(Exception):
    pass


class WatchingPermissionDenied(CatalogError):
    pass


class GamerLoginStale(WatchingPermissionDenied):
    """帶登入 cookie 查詢仍拿不到觀看權限，而且 session 裡確實有登入 cookie（`BAHAID`）
    → 登入態在站方那邊失效了（cookie 過期／被撤），**不是**「這個帳號本來就沒權限」。
    呼叫端據此提示「重新登入」、掛「登入態失效」橫幅，而不是「你沒有 VIP」，也不該
    當成站方暫時性錯誤一直重試（使用者 2026-09-08：VIP 集數因 cookie 失效被當站方
    維護每 5 秒重試 30 次、狂噴例外；重新登入後就正常下載了）。是
    `WatchingPermissionDenied` 的子類別，既有 `except WatchingPermissionDenied` 一律
    仍能接住。"""


class ParentPasswordRequired(CatalogError):
    pass


@dataclass(frozen=True)
class EpisodeSummary:
    episode: int
    video_sn: int
    state: int
    cover: str


@dataclass(frozen=True)
class VideoInfo:
    video_sn: int
    anime_sn: int
    title: str
    duration: int
    up_time: str
    prev_video_sn: int
    next_video_sn: int
    watching_permission_pass: bool
    request_parent_password: bool
    anime_title: str
    total_episode: int
    episode_index: int
    episodes: list[EpisodeSummary] = field(default_factory=list)
    # `anime.seasonStart`（`YYYY/MM/DD`，可能空）——季別資料夾分類用，見
    # docs/api-observations/anime-page.md 與 scheduler/seasons.py
    season_start: str = ""
    # 這一集是不是番劇內的特別篇（`video.title` 結尾 `[特別篇]`）——檔名用 SP 編號
    is_special_episode: bool = False

    @property
    def episode_number(self) -> int | str:
        """人看的集數——季不從第 1 集開始時 `episode_index + 1` 會錯（round 7 第 9 項）。
        優先從 `episodes` 裡找這個 video_sn 對應的 `EpisodeSummary.episode`（站方原值，
        可能是 `"38"` 甚至 `"特別篇"`），找不到（電影／沒有集數清單）才退回 index+1。"""
        for ep in self.episodes:
            if ep.video_sn == self.video_sn:
                return ep.episode
        return self.episode_index + 1


class CatalogClient:
    def __init__(self, session: GamerSession, *, member_session: "GamerSession | None" = None) -> None:
        # `session`：一般用（列集數、非年齡限制番劇）——正式環境傳的是**無 cookie** 的
        # GuestSession，`api/anime/v1/video.php` 不需要登入態就回得到完整集數清單，只有
        # `watchingPermission.pass` 對訪客一律 False（使用者 2026-09-08：不必要的帶 cookie
        # 請求會一直輪換 BAHARUNE、干擾保活）。
        # `member_session`：只有 `session` 查到某集 `pass=False` 時，才帶登入 cookie 再查
        # 一次——可能是年齡限制番劇，登入後才看得到。沒傳＝`session` 就是全部（舊行為）。
        self._session = session
        self._member_session = member_session

    def get_video(self, video_sn: int) -> VideoInfo:
        """查單集資料。這集若沒有觀看權限／需要家長密碼，會拋出明確的例外，不回傳殘缺資料，
        讓呼叫端能區分「這集就是看不了，跳過」跟「網路錯誤，該重試」。"""
        video, anime = self._fetch_raw(video_sn)
        info = _build_video_info(video, anime)
        used_member_session = False
        if (
            not info.watching_permission_pass
            and self._member_session is not None
            and self._member_session is not self._session
        ):
            # 訪客身分看不到 → 帶登入 cookie 再查一次（可能是年齡限制番劇／VIP）
            video, anime = self._fetch_raw(video_sn, session=self._member_session)
            info = _build_video_info(video, anime)
            used_member_session = True
        # 日誌顯示番劇名不顯示 sn（使用者 2026-09-05）——這裡已經有解析好的 info
        label = f"《{info.anime_title}》第 {info.episode_number} 集" if info.anime_title else f"video_sn={video_sn}"
        if not info.watching_permission_pass:
            # 帶著登入 cookie 還是拿不到權限，而且 session 裡確實有登入身分（`BAHAID`）
            # → 登入態在站方那邊失效了（cookie 過期／被撤），不是「這帳號沒 VIP」。
            # 呼叫端據此提示重新登入、不當站方錯誤一直重試。
            if used_member_session and _session_has_login(self._member_session):
                raise GamerLoginStale(f"{label} 沒有觀看權限（動畫瘋登入態可能已失效，請重新登入）")
            raise WatchingPermissionDenied(f"{label} 沒有觀看權限")
        if info.request_parent_password:
            raise ParentPasswordRequired(f"{label} 需要家長密碼")
        return info

    def get_latest_episode(self, video_sn: int) -> VideoInfo:
        """排程清單 mode=latest 用：拿這部番劇「本篇」目前最新的一集。

        **只看 `episodes["0"]`（本篇），依集數「數字」取最大**——舊寫法是把所有分季代碼
        （本篇＋中文配音＋特別篇＋電影）攤平後 `max(key=ep.episode)`，有兩個嚴重問題
        （使用者 2026-09-04 回報「收藏的番劇沒下載到最新集」）：
        1. `ep.episode` 是站方原值，**字串比大小**：`"9" > "10"`、`"9" > "12"` → 番劇一到
           第 10 集以後就永遠停在第 9 集。
        2. 特別篇的 `episode` 是 `"特別篇1"` 這種非數字字串，跟本篇的整數放一起 `max` →
           `TypeError`（str vs int），整個排程檢查拋例外、這部番劇再也不會更新。
        """
        _, anime = self._fetch_raw(video_sn)
        main = anime.get("episodes", {}).get("0", [])
        numbered = [
            ep for ep in main if str(ep.get("episode", "")).strip().isdigit()
        ]
        if numbered:
            latest = max(numbered, key=lambda ep: int(str(ep["episode"]).strip()))
            return self.get_video(latest["videoSn"])
        if main:
            # 本篇清單全是非數字（極少見）——用陣列最後一項，站方本篇陣列本來就是播出順序
            return self.get_video(main[-1]["videoSn"])
        # 完全沒有本篇集數清單（電影／單集特別篇作品）——這個 video_sn 本身就是唯一一集
        return self.get_video(video_sn)

    def latest_main_episode_number(self, video_sn: int) -> int | None:
        """「本篇」目前最新的數字集數——每日新番彙整拿來預測「這次要更新第幾集」
        （比照 aniGamerPlus `_fetch_live_anime_info`：只看純數字的「本篇」集數，忽略
        特別篇／中文配音／電影）。`episodes` 物件的 key `"0"` 是本篇（見
        docs/api-observations/anime-page.md），其他 key 是電影/特別篇/中文配音。
        沒有數字集數（電影／特別篇作品，或查詢不到）回 None。"""
        _, anime = self._fetch_raw(video_sn)
        main = anime.get("episodes", {}).get("0", [])
        nums = [
            int(str(ep.get("episode", "")).strip())
            for ep in main
            if str(ep.get("episode", "")).strip().isdigit()
        ]
        return max(nums) if nums else None

    def get_all_episodes(self, video_sn: int) -> list[VideoInfo]:
        """排程清單 mode=all 用：拿這部番劇所有能看的集數，看不了的集數（權限被拒／需要家長
        密碼）直接跳過，不讓整批查詢因為其中一集看不了就整個失敗。"""
        _, anime = self._fetch_raw(video_sn)
        episodes = _flatten_episodes(anime.get("episodes", {}))
        if not episodes:
            # 電影／特別篇這類站方沒有分季集數清單的作品——退回把「這個 video_sn 本身」
            # 當成唯一一集（比照 get_latest_episode 的 fallback，round 6 第 13 項）
            try:
                return [self.get_video(video_sn)]
            except (WatchingPermissionDenied, ParentPasswordRequired):
                return []
        results: list[VideoInfo] = []
        for ep in episodes:
            try:
                results.append(self.get_video(ep.video_sn))
            except (WatchingPermissionDenied, ParentPasswordRequired):
                continue
        return results

    def list_episodes(self, video_sn: int) -> tuple[str, list[EpisodeSummary]]:
        """(番劇標題, 所有集數的輕量摘要)——只打一次 video.php，**不**逐集 get_video()。

        給「重新檢查排程更新」（web_redesign_round2.md 階段 4）先粗篩「哪幾集還沒下載」
        用：那個功能大部分時候要掃十幾二十部收藏、每部十幾集，如果每一集都 get_video()
        會慢到不能用。真正決定要下載某一集時再走 get_video() 的權限檢查。"""
        video, anime = self._fetch_raw(video_sn)
        episodes = _flatten_episodes(anime.get("episodes", {}))
        if not episodes:
            # 電影／特別篇沒有集數清單 → 用這個 video_sn 本身當唯一一集（round 6 第 13 項）
            episodes = [
                EpisodeSummary(
                    episode=anime.get("episodeIndex", 0) + 1,
                    video_sn=video_sn,
                    state=video.get("state", 0),
                    cover=video.get("cover", ""),
                )
            ]
        return clean_anime_title(anime["title"]), episodes

    def _fetch_raw(self, video_sn: int, *, session=None) -> tuple[dict, dict]:
        """回傳 (video 原始 dict, anime 原始 dict)，不做觀看權限檢查——只給內部用來列集數，
        真正要「使用」某一集時一律透過 get_video() 走過權限檢查。"""
        response = (session or self._session).get(_VIDEO_INFO_URL, params={"videoSn": video_sn})
        payload = response.json()
        try:
            data = payload["data"]
            return data["video"], data["anime"]
        except (KeyError, TypeError) as exc:
            raise CatalogError(f"video.php 回應格式不符預期: {payload}") from exc


def _build_video_info(video: dict, anime: dict) -> VideoInfo:
    return VideoInfo(
        video_sn=video["videoSn"],
        anime_sn=video["animeSn"],
        title=video["title"],
        duration=video["duration"],
        up_time=video["upTime"],
        prev_video_sn=video["prevVideoSn"],
        next_video_sn=video["nextVideoSn"],
        watching_permission_pass=bool(video.get("watchingPermission", {}).get("pass", False)),
        request_parent_password=bool(video.get("requestParentPassword", False)),
        anime_title=clean_anime_title(anime["title"]),
        total_episode=anime["totalEpisode"],
        episode_index=anime["episodeIndex"],
        episodes=_flatten_episodes(anime.get("episodes", {})),
        season_start=anime.get("seasonStart", ""),
        is_special_episode=is_special_episode_title(video.get("title", "")),
    )


def _flatten_episodes(episodes_by_season: dict) -> list[EpisodeSummary]:
    flattened: list[EpisodeSummary] = []
    for season_episodes in episodes_by_season.values():
        for ep in season_episodes:
            flattened.append(
                EpisodeSummary(
                    episode=ep["episode"],
                    video_sn=ep["videoSn"],
                    state=ep["state"],
                    cover=ep["cover"],
                )
            )
    return flattened
