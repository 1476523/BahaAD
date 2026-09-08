"""範本渲染：`@token@` 替換 ＋ Telegram HTML 子集 → Discord Markdown 轉換。
規格見 docs/requirements/notify.md「範本 token」「v1.1」兩節。

**v1.1（round3 階段 5-4）**：可自訂範本支援 Telegram HTML 標籤的子集
（`<b>`／`<i>`／`<u>`／`<s>`／`<code>`／`<pre>`／`<blockquote>`／`<a href>`／
`<tg-spoiler>` 等）。發送時：
- Telegram 走 `parse_mode=HTML`（見 `senders.send_telegram`），帶入的動態內容
  （番劇名、錯誤訊息…）可能含 `<`／`>`／`&`，`render_message(escape_html=True)` 先做
  HTML 跳脫再替換 token，否則會被誤判成標籤／讓 Telegram API 回錯。
- Discord 沒有 HTML，`to_discord_markdown()` 把標籤轉成 markdown，帶入內容**不跳脫**。

轉換規則、逐行包 marker 的理由都沿用舊專案 `aniGamerPlus/NotifyDB.py`（「機動調整作品
比對」「通知範本快捷按鈕」是 `docs/custom-features.md` 的自訂功能，可參考舊專案）。
"""

from __future__ import annotations

import html
import re


def render_message(template: str, *, escape_html: bool = False, **context: object) -> str:
    """`@token@` → `context` 對應值。`context` 沒帶到的 token 保留原樣不清空，方便使用者
    發現自己拼錯 token 名稱（沿用舊專案定案，見 notify.md「邊界案例」）。

    `escape_html=True`（Telegram 用）：帶入值先 `html.escape`，避免值裡的 `<`／`&` 被
    `parse_mode=HTML` 誤判——範本自己寫的標籤不受影響（那是 template 字串、不是帶入值）。
    """
    text = template
    for key, value in context.items():
        replacement = str(value)
        if escape_html:
            replacement = html.escape(replacement, quote=False)
        text = text.replace(f"@{key}@", replacement)
    return text


def _wrap_per_line(marker: str):
    """逐行各自包一組完整 marker，而不是整段多行只在頭尾各包一次——避免標記跨行時被
    最後才處理的 blockquote 規則插入的行首「> 」前綴打斷成沒被正確渲染的裸符號。"""
    return lambda m: "\n".join(marker + line + marker for line in m.group(1).split("\n"))


# Telegram HTML → Discord Markdown。**依序處理，順序很重要**：
# 1. 帶 class 的程式碼區塊排在單純 <pre>/<code> 之前，否則被泛用規則提前吃掉、帶不出語言名
# 2. blockquote 排在所有 inline 樣式（粗體/斜體/…）之後：blockquote 規則會把「> 」插進每行
#    行首，排在 inline 規則前的話 inline 的 DOTALL 會在 "> " 插入後才跨行比對、把標記插在
#    "> " 中間；inline 規則都改用 _wrap_per_line() 讓標記不跨行，搭配 blockquote 最後處理才對
_TG_TO_DISCORD_RULES: tuple[tuple[re.Pattern[str], object], ...] = (
    (re.compile(r'<pre>\s*<code class="language-([^"]*)">(.*?)</code>\s*</pre>', re.DOTALL),
     lambda m: "```" + m.group(1) + "\n" + m.group(2) + "\n```"),
    (re.compile(r"<pre>(.*?)</pre>", re.DOTALL), lambda m: "```\n" + m.group(1) + "\n```"),
    (re.compile(r"<code>(.*?)</code>", re.DOTALL), lambda m: "`" + m.group(1) + "`"),
    (re.compile(r"<(?:b|strong)>(.*?)</(?:b|strong)>", re.DOTALL), _wrap_per_line("**")),
    (re.compile(r"<(?:i|em)>(.*?)</(?:i|em)>", re.DOTALL), _wrap_per_line("*")),
    (re.compile(r"<(?:u|ins)>(.*?)</(?:u|ins)>", re.DOTALL), _wrap_per_line("__")),
    (re.compile(r"<(?:s|strike|del)>(.*?)</(?:s|strike|del)>", re.DOTALL), _wrap_per_line("~~")),
    (re.compile(r"<tg-spoiler>(.*?)</tg-spoiler>", re.DOTALL), _wrap_per_line("||")),
    (re.compile(r'<a href="([^"]*)">(.*?)</a>', re.DOTALL), lambda m: "[" + m.group(2) + "](" + m.group(1) + ")"),
    (re.compile(r"<blockquote(?:\s+expandable)?>(.*?)</blockquote>", re.DOTALL),
     lambda m: "\n".join("> " + line for line in m.group(1).strip("\n").split("\n"))),
)


def to_discord_markdown(text: str) -> str:
    """把範本裡的 Telegram HTML 標籤轉成 Discord markdown。`@token@` 佔位符不受影響
    （規則只匹配 `<...>` 樣式標籤）。`<blockquote expandable>` Discord 沒有對應功能，
    退化成一般引用。"""
    for pattern, repl in _TG_TO_DISCORD_RULES:
        text = pattern.sub(repl, text)
    return text


# ---- 範本編輯頁「模擬預覽」用（只給預覽畫面，跟真正送出的內容無關）----

_PREVIEW_SAFE_TAGS = ("b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "blockquote")
_PREVIEW_A_RE = re.compile(r"&lt;a href=&quot;([^&]+?)&quot;&gt;(.*?)&lt;/a&gt;", re.DOTALL)


def to_telegram_preview_html(template: str, context: dict[str, object]) -> str:
    """範本 → 可直接塞進預覽視窗的 HTML，模擬 Telegram（`parse_mode=HTML`）氣泡。
    先把整段 escape（使用者亂打的 `<script>` 等一律失效），只還原白名單格式標籤，
    最後才把 `@token@` 換成跳脫過的範例值。"""
    out = html.escape(template)
    for tag in _PREVIEW_SAFE_TAGS:
        out = out.replace(f"&lt;{tag}&gt;", f"<{tag}>").replace(f"&lt;/{tag}&gt;", f"</{tag}>")
    out = out.replace("&lt;blockquote expandable&gt;", "<blockquote>")
    out = out.replace("&lt;tg-spoiler&gt;", '<span class="preview-spoiler">').replace("&lt;/tg-spoiler&gt;", "</span>")
    out = _PREVIEW_A_RE.sub(r'<a href="\1" target="_blank" rel="noopener">\2</a>', out)
    for key, value in context.items():
        out = out.replace(f"@{key}@", html.escape(str(value), quote=False))
    return out


_MD_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"```(?:\w*)\n?(.*?)```", re.DOTALL), r"<pre>\1</pre>"),
    (re.compile(r"`([^`]+)`"), r"<code>\1</code>"),
    (re.compile(r"\*\*(.+?)\*\*", re.DOTALL), r"<b>\1</b>"),
    (re.compile(r"__(.+?)__", re.DOTALL), r"<u>\1</u>"),
    (re.compile(r"\*(.+?)\*", re.DOTALL), r"<i>\1</i>"),
    (re.compile(r"~~(.+?)~~", re.DOTALL), r"<s>\1</s>"),
    (re.compile(r"\|\|(.+?)\|\|", re.DOTALL), r'<span class="preview-spoiler">\1</span>'),
    (re.compile(r"\[(.+?)\]\((.+?)\)"), r'<a href="\2" target="_blank" rel="noopener">\1</a>'),
)


def to_discord_preview_html(template: str, context: dict[str, object]) -> str:
    """範本 → Discord 卡片的預覽 HTML：先轉 markdown、替換 token，再把 markdown 轉成
    可顯示的 HTML（一樣先整段 escape 再還原）。"""
    text = render_message(to_discord_markdown(template), **context)
    text = html.escape(text)
    for pattern, repl in _MD_RULES:
        text = pattern.sub(repl, text)
    text = re.sub(
        r"(?:^&gt; .*(?:\n&gt; .*)*)",
        lambda m: '<blockquote>' + re.sub(r"^&gt; ", "", m.group(0), flags=re.MULTILINE) + "</blockquote>",
        text,
        flags=re.MULTILINE,
    )
    return text
