"""極小的 Markdown → HTML——**只**給「隱私權說明」這個內部靜態文件用（`docs/PRIVACY_
POLICY.md`），不是通用 Markdown 引擎。支援到剛好夠這份文件排版一致：標題、段落、
引言、清單、表格、`---`、`**粗體**`、`` `程式碼` ``、`*斜體*`。

輸入是專案自己維護的檔案（非使用者輸入），但仍一律逸出 HTML 特殊字元後才組標籤。

使用者 2026-09-17：文件太長、希望各大段落做成收納（摺疊）顯示——`## ` 這個層級的
標題（「一、錯誤自動回報」「二、即時匿名使用統計」……）轉成 `<details><summary>`，
預設收合，點開才看內容；標題之間原本的 `---` 分隔線在這個版面下變得多餘（每個
`<details>` 自己就是一個有邊框的區塊），直接省略。標題以前的內容（文件標題、開頭
簡介）維持一律顯示，不進任何 `<details>`。`###` 這層維持一般標題，不再往下收納，
避免巢狀摺疊太瑣碎。
"""

from __future__ import annotations

import html
import re

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`]+?)`")
_ITALIC = re.compile(r"(?<!\*)\*([^*]+?)\*(?!\*)")


def _inline(text: str) -> str:
    out = html.escape(text)
    out = _CODE.sub(lambda m: f"<code>{m.group(1)}</code>", out)
    out = _BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", out)
    out = _ITALIC.sub(lambda m: f"<em>{m.group(1)}</em>", out)
    return out


def _table(rows: list[str]) -> str:
    def cells(line: str) -> list[str]:
        return [c.strip() for c in line.strip().strip("|").split("|")]

    head = cells(rows[0])
    body = [cells(r) for r in rows[2:]]  # rows[1] 是 |---|---| 分隔線
    thead = "".join(f"<th>{_inline(c)}</th>" for c in head)
    tbody = "".join(
        "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in body
    )
    return f"<table><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table>"


def render(md: str) -> str:
    lines = (md or "").replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            level = min(len(m.group(1)), 6)
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            i += 1
            continue

        if stripped == "---" or stripped == "***":
            out.append("<hr>")
            i += 1
            continue

        if stripped.startswith(">"):
            block = []
            while i < n and lines[i].strip().startswith(">"):
                block.append(lines[i].strip().lstrip(">").strip())
                i += 1
            out.append(f"<blockquote>{_inline(' '.join(block))}</blockquote>")
            continue

        if stripped.startswith("|") and i + 1 < n and re.match(r"^\|?[\s:|-]+\|?$", lines[i + 1].strip()):
            block = []
            while i < n and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            out.append(_table(block))
            continue

        if re.match(r"^[-*+]\s+", stripped):
            items = []
            while i < n and re.match(r"^[-*+]\s+", lines[i].strip()):
                items.append(_inline(re.sub(r"^[-*+]\s+", "", lines[i].strip())))
                i += 1
            out.append("<ul>" + "".join(f"<li>{it}</li>" for it in items) + "</ul>")
            continue

        # 一般段落：吃到下一個空行
        para = []
        while i < n and lines[i].strip() and not re.match(
            r"^(#{1,6}\s|>|\||[-*+]\s|---$|\*\*\*$)", lines[i].strip()
        ):
            para.append(lines[i].strip())
            i += 1
        out.append(f"<p>{_inline(' '.join(para))}</p>")

    return _fold_h2_sections(out)


_H2_BLOCK = re.compile(r"^<h2>(.*)</h2>$")


def _fold_h2_sections(blocks: list[str]) -> str:
    """把 `<h2>` 開始的每一段收進 `<details class="policy-section">`（預設收合），
    `<h2>` 之前的內容（文件標題、開頭簡介）原樣保留在外面。段落間原本的 `<hr>`
    在收合版面下多餘（每個 `<details>` 自己就有邊框），一併省略。"""
    out: list[str] = []
    in_section = False
    for block in blocks:
        if block == "<hr>":
            continue
        m = _H2_BLOCK.match(block)
        if m:
            if in_section:
                out.append("</details>")
            out.append(f'<details class="policy-section"><summary>{m.group(1)}</summary>')
            in_section = True
            continue
        out.append(block)
    if in_section:
        out.append("</details>")
    return "\n".join(out)
