"""抓 manifest.json＋驗證 ed25519 簽章。規格見 docs/requirements/updater.md、
manifest 格式見 docs/decisions/0001-update-manifest-format.md。

簽章驗證的序列化方式跟 `scripts/sign_manifest.py` 簽署端完全對稱——都是對「除了
`signature` 欄位以外的整個 JSON 內容」用 `json.dumps(data, sort_keys=True,
ensure_ascii=False, separators=(",", ":"))` 序列化成規範化位元組後簽署/驗證。任何一邊
改了序列化方式，簽章就永遠對不上，兩邊要一起改。

內建公鑰是 `scripts/generate_manifest_signing_key.py` 這次實作已經跑過一次產生的，私鑰
只在使用者自己的機器上（`keys/manifest_signing_key.pem`，`.gitignore` 已排除，絕不進
版本控制）。
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

MANIFEST_URL = "https://raw.githubusercontent.com/1476523/BahaAD/updates/manifest.json"

_PUBLIC_KEY_B64 = "gVv7gZkFGbicmTKFaplsSN1Jw6NjNzEbhKJgTctOlEg="


class ManifestSignatureError(Exception):
    pass


class ManifestUnavailableError(Exception):
    """manifest.json 這次抓不到、或回應根本不是 JSON——最常見的原因是差異更新的
    `updates` 分支還沒發布（GitHub raw 回 404「404: Not Found」純文字）。這是預期內
    的狀態、不是故障，呼叫端只要記 info 並跳過這一輪，不必回報診斷。"""


class HttpGetter(Protocol):
    def get(self, url: str) -> "_ResponseLike": ...


class _ResponseLike(Protocol):
    status_code: int

    def json(self) -> dict: ...


@dataclass(frozen=True)
class ManifestFileEntry:
    path: str
    sha256: str
    url: str


@dataclass(frozen=True)
class UpdateManifest:
    version: str
    minimum_required_version: str
    mandatory: bool
    notes: str
    files: list[ManifestFileEntry]


def _canonical_bytes(data: dict[str, Any]) -> bytes:
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _verify_signature(data: dict[str, Any]) -> None:
    signature_b64 = data.get("signature")
    if not signature_b64:
        raise ManifestSignatureError("manifest 缺少 signature 欄位")

    payload = {key: value for key, value in data.items() if key != "signature"}
    try:
        public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(_PUBLIC_KEY_B64))
        public_key.verify(base64.b64decode(signature_b64), _canonical_bytes(payload))
    except InvalidSignature as exc:
        raise ManifestSignatureError("manifest 簽章驗證失敗") from exc
    except (ValueError, TypeError) as exc:
        # base64 解碼失敗、簽章長度不對這類格式錯誤，一律視同驗證失敗——不繼續往下走，
        # 不區分「格式壞掉」跟「簽章真的對不上」，呼叫端只需要知道能不能信任這份 manifest
        raise ManifestSignatureError("manifest 簽章格式不正確") from exc


def fetch_manifest(http: HttpGetter) -> UpdateManifest:
    """GET manifest.json，驗證簽章，驗不過丟 ManifestSignatureError、直接拒絕整個
    更新流程，不繼續往下走。"""
    response = http.get(MANIFEST_URL)

    status = getattr(response, "status_code", None)
    if status is not None and status != 200:
        raise ManifestUnavailableError(f"manifest.json 目前取不到（HTTP {status}）")

    try:
        data = response.json()
    except ValueError as exc:  # json.JSONDecodeError 也是 ValueError；raw 404 頁面會走到這
        raise ManifestUnavailableError(f"manifest.json 回應不是有效 JSON：{exc}") from exc

    _verify_signature(data)

    files = [
        ManifestFileEntry(path=entry["path"], sha256=entry["sha256"], url=entry["url"])
        for entry in data["files"]
    ]
    return UpdateManifest(
        version=data["version"],
        minimum_required_version=data["minimum_required_version"],
        mandatory=data["mandatory"],
        notes=data.get("notes", ""),
        files=files,
    )
