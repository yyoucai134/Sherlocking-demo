"""browse_page 工具：抓取网页并抽取正文，补齐 web_search 只返回摘要片段的短板。

设计要点（对应正文抽取的三类常见翻车场景）：
- 结构化解析：用标准库 html.parser 的标签栈定位元素，不用正则截 HTML，
  嵌套标签/页面改版时不会截断（无第三方依赖，无需 pip 安装）。
- 容器优先 + 全局兜底：优先 article/main/正文 class 容器内的段落；
  容器内有效内容不足时回退到全页段落。
- 先清洗后判定：cookie/订阅/广告/版权等模板节点在收集文本前就通过
  skip 栈排除；段落级过滤模板文案之后，才做长度/段落数门槛。
- 段落去重：归一化（压空白、去 CJK 间隙空格）后按指纹去重，
  避免父子节点文本重复计入。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

DEFAULT_MAX_CHARS = 6000
FETCH_TIMEOUT = 20
MAX_HTML_BYTES = 5 * 1024 * 1024  # 单个页面最多读取 5MB

# 结构性噪声标签：整棵子树都不是正文
_NOISE_TAGS = frozenset({
    "script", "style", "noscript", "iframe", "svg", "nav", "header",
    "footer", "aside", "form", "button", "select", "template", "canvas",
})

# class/id 切词后命中即视为噪声容器（导航/弹窗/广告/评论/推荐等）
_NOISE_TOKENS = frozenset({
    "ad", "ads", "advert", "advertisement", "banner", "cookie", "consent",
    "popup", "modal", "dialog", "mask", "subscribe", "newsletter", "donate",
    "login", "signin", "signup", "register", "breadcrumb", "crumb", "share",
    "social", "comment", "comments", "related", "recommend", "recommended",
    "sidebar", "toolbar", "promo", "promotion", "navbar", "menu", "copyright",
    "pagination", "backtop", "back-to-top", "hidden", "tools", "edit",
    "recommendation", "sug", "search-box", "searchbox",
})

# class/id 切词后命中即视为正文容器
_CONTENT_TOKENS = frozenset({
    "article", "content", "main", "post", "entry", "prose", "story",
    "artibody", "lemma", "markdown", "editor", "zoom", "rich", "richtext",
    "detail", "body",
})

# 按段落收集文本的块级标签
_BLOCK_TAGS = frozenset({
    "p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "dd", "pre",
})
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
# 在这些元素闭合时，把游离文本（不在 <p> 内的文字）刷成一个段落
_FLUSH_AT_CLOSE = frozenset({"div", "section", "article", "main", "td", "th", "br"})

# 段落级模板文案：命中且段落较短时丢弃（长段落里偶然出现不全丢）
_BOILERPLATE_RE = re.compile(
    r"(?i)(cookie|consent|订阅|关注公众号|关注我们|版权所有|copyright|all rights reserved|"
    r"隐私政策|privacy policy|使用条款|terms of (use|service)|广告|advertisement|"
    r"skip to (content|main)|点击下载|下载app|立即登录|免费注册|sign (in|up)|"
    r"related (articles|posts)|recommended (for you|articles)|继续阅读全文|责任编辑|"
    r"本文来自|转载请注明|免责声明|举报/反馈|扫一扫|二维码|打开app|查看更多)"
)
_WS_RE = re.compile(r"\s+")
_CJK_SPACE_RE = re.compile(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")

# 正文容器门槛（容器内满足才采用容器范围，否则回退全页）
_MIN_CONTAINER_PARAS = 2
_MIN_CONTAINER_CHARS = 200
# 单段最低有效长度
_MIN_PARA_CJK = 10
_MIN_PARA_TOTAL = 30

_USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.1 Safari/605.1.15",
)


def _class_tokens(attrs: list[tuple[str, str | None]]) -> set[str]:
    """提取元素 class/id 中的切词集合，用于噪声/正文容器判定。"""
    tokens: set[str] = set()
    for name, value in attrs:
        if name not in ("class", "id") or not value:
            continue
        for token in _TOKEN_SPLIT_RE.split(value.lower()):
            if token:
                tokens.add(token)
    return tokens


def _is_noise_attrs(tag: str, attrs: list[tuple[str, str | None]]) -> bool:
    tokens = _class_tokens(attrs)
    if tokens & _NOISE_TOKENS:
        return True
    # role=navigation/banner 是全站通用模块，等同噪声
    for name, value in attrs:
        if name == "role" and value and value.lower() in {"navigation", "banner"}:
            return True
    return False


def _is_content_marker(tag: str, attrs: list[tuple[str, str | None]]) -> bool:
    if tag in {"article", "main"}:
        return True
    for name, value in attrs:
        if name == "role" and value and value.lower() == "main":
            return True
    return bool(_class_tokens(attrs) & _CONTENT_TOKENS)


def _normalize_text(text: str) -> str:
    """压缩空白，并去掉两个汉字之间因排版产生的空格。"""
    text = _WS_RE.sub(" ", text).strip()
    # 最多迭代几轮，处理“汉 字 汉 字”这种连续间隔
    for _ in range(3):
        cleaned = _CJK_SPACE_RE.sub("", text)
        if cleaned == text:
            break
        text = cleaned
    return text.strip()


def _para_fingerprint(text: str) -> str:
    """段落去重指纹：归一化后只保留字母数字与汉字。"""
    return re.sub(r"[\W_]+", "", text.lower())


def _is_valid_paragraph(text: str) -> bool:
    """段落级清洗后的有效性门槛。"""
    if not text:
        return False
    if len(_CJK_RE.findall(text)) >= _MIN_PARA_CJK:
        return True
    # 非中文为主的段落：总长够即可
    return len(text) >= _MIN_PARA_TOTAL


def _is_boilerplate(text: str) -> bool:
    """短段落命中模板文案即丢弃；长段落保留（避免误杀含提示词的真实正文）。"""
    return len(text) < 80 and bool(_BOILERPLATE_RE.search(text))


class _ArticleExtractor(HTMLParser):
    """标签栈式正文抽取器。

    维护一条元素栈，每项记录 (tag, skipped, in_container)：
    - skipped: 自身或祖先为噪声节点，子树全部不收集；
    - in_container: 自身或祖先命中正文容器标记。
    文本按块级标签（p/li/h* 等）聚合成段，游离文本在结构边界刷段。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._stack: list[tuple[str, bool, bool]] = []
        self._block_tags: list[str] = []       # 当前打开的块级标签栈
        self._block_parts: list[list[str]] = []
        self._block_in_container: list[bool] = []
        self._loose_parts: list[str] = []
        self._loose_in_container = False
        self.blocks: list[dict[str, Any]] = []  # {kind, text, in_container}
        self._in_title = False
        self.title_parts: list[str] = []
        self.meta_description = ""
        self.og_title = ""

    # ---- 栈辅助 ----
    def _parent_flags(self) -> tuple[bool, bool]:
        if self._stack:
            _, skipped, in_container = self._stack[-1]
            return skipped, in_container
        return False, False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        parent_skipped, parent_container = self._parent_flags()
        skipped = parent_skipped or tag in _NOISE_TAGS or _is_noise_attrs(tag, attrs)
        in_container = (not skipped) and (
            parent_container or _is_content_marker(tag, attrs)
        )
        self._stack.append((tag, skipped, in_container))

        if skipped:
            return
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            self._handle_meta(attrs)
        elif tag in _BLOCK_TAGS:
            self._flush_loose()
            self._block_tags.append(tag)
            self._block_parts.append([])
            self._block_in_container.append(in_container)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # <br/> 在当前块内当作空格，避免句子粘连
        if tag == "br" and self._block_parts:
            self._block_parts[-1].append(" ")
        elif tag == "meta":
            self._handle_meta(attrs)

    def handle_endtag(self, tag: str) -> None:
        # 从栈顶弹出最近的同名标签（容忍缺失/错位闭合）
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                break

        if self._in_title and tag == "title":
            self._in_title = False
        # 关闭块级标签：连带关闭压在它上面的未闭合块
        while self._block_tags and self._block_tags[-1] != tag:
            self._finalize_block()
        if self._block_tags and self._block_tags[-1] == tag:
            self._finalize_block()
        if tag in _FLUSH_AT_CLOSE:
            self._flush_loose()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            # 标题文字只进标题缓冲区，避免泄漏为正文段落（全页回退时）
            self.title_parts.append(data)
            return
        if not data or not data.strip():
            return
        skipped, in_container = self._parent_flags()
        if skipped:
            return
        if self._block_parts:
            self._block_parts[-1].append(data)
        else:
            if not self._loose_parts:
                self._loose_in_container = in_container
            self._loose_parts.append(data)

    def _handle_meta(self, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {name.lower(): (value or "") for name, value in attrs}
        name = (attr_map.get("name") or attr_map.get("property") or "").lower()
        if name == "description" and not self.meta_description:
            self.meta_description = attr_map.get("content", "")
        elif name == "og:title" and not self.og_title:
            self.og_title = attr_map.get("content", "")

    def _finalize_block(self) -> None:
        tag = self._block_tags.pop()
        parts = self._block_parts.pop()
        in_container = self._block_in_container.pop()
        text = _normalize_text("".join(parts))
        if text:
            self.blocks.append({
                "kind": "heading" if tag in _HEADING_TAGS else "paragraph",
                "text": text,
                "in_container": in_container,
            })

    def _flush_loose(self) -> None:
        if not self._loose_parts:
            return
        text = _normalize_text("".join(self._loose_parts))
        in_container = self._loose_in_container
        self._loose_parts = []
        self._loose_in_container = False
        if text:
            self.blocks.append({"kind": "paragraph", "text": text,
                                "in_container": in_container})

    def close(self) -> None:  # type: ignore[override]
        super().close()
        while self._block_tags:
            self._finalize_block()
        self._flush_loose()

    # ---- 输出 ----
    def title(self) -> str:
        return _normalize_text(self.og_title or "".join(self.title_parts))

    def extract(self) -> tuple[str, bool]:
        """返回 (正文 Markdown 风格文本, 是否命中正文容器)。"""
        paragraphs = [b for b in self.blocks if b["kind"] == "paragraph"]

        def clean_paras(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
            seen: set[str] = set()
            result: list[dict[str, Any]] = []
            for block in blocks:
                text = block["text"]
                if _is_boilerplate(text):
                    continue
                # 标题块不受段落长度门槛约束（短标题如“背景”同样有价值）
                if block["kind"] != "heading" and not _is_valid_paragraph(text):
                    continue
                fingerprint = _para_fingerprint(text)
                if not fingerprint or fingerprint in seen:
                    continue
                seen.add(fingerprint)
                result.append(block)
            return result

        container_paras = [b for b in paragraphs if b["in_container"]]
        use_container = (
            len(container_paras) >= _MIN_CONTAINER_PARAS
            and sum(len(b["text"]) for b in container_paras) >= _MIN_CONTAINER_CHARS
        )
        cleaned = clean_paras(self.blocks if not use_container else [
            b for b in self.blocks if b["in_container"]
        ])

        lines: list[str] = []
        for block in cleaned:
            if block["kind"] == "heading":
                lines.append(f"## {block['text']}")
            else:
                lines.append(block["text"])
        return "\n\n".join(lines), use_container


def _validate_public_url(url: str) -> None:
    """仅允许 http(s) 且目标不是本机/内网地址，防止借工具探测内网。"""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("仅支持 http/https 且必须是完整 URL")
    host = parts.hostname or ""
    if host in {"localhost", "ip6-localhost"} or host.endswith(".local"):
        raise ValueError("禁止访问本机/内网地址")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return  # 域名（非 IP 直连），放行
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        raise ValueError("禁止访问本机/内网地址")


def _decode_html(raw: bytes, header_charset: str | None) -> str:
    """按 响应头 → meta charset → utf-8 的顺序解码。"""
    encodings = [header_charset, "utf-8", "gb18030"]
    head = raw[:2048].decode("ascii", errors="ignore")
    match = re.search(r'charset=["\']?([\w-]+)', head, re.I)
    if match:
        encodings.insert(1, match.group(1))
    for encoding in encodings:
        if not encoding:
            continue
        try:
            return raw.decode(encoding, errors="strict")
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _fetch_html(url: str) -> tuple[str, str, str]:
    """抓取页面，返回 (最终URL, content_type, HTML 文本)。"""
    request = Request(url, headers={
        "User-Agent": _USER_AGENTS[0],
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    with urlopen(request, timeout=FETCH_TIMEOUT) as response:  # noqa: S310 - URL 已校验
        content_type = response.headers.get_content_type()
        final_url = response.geturl()
        raw = response.read(MAX_HTML_BYTES)
        charset = response.headers.get_content_charset()
    if content_type not in {"text/html", "application/xhtml+xml", "text/plain", ""}:
        raise ValueError(f"不支持的内容类型：{content_type}（非网页，可能是 PDF/图片/文件）")
    return final_url, content_type, _decode_html(raw, charset)


def _truncate_at_boundary(text: str, max_chars: int) -> tuple[str, bool]:
    """在段落边界截断，避免把一句话切一半。"""
    if len(text) <= max_chars:
        return text, False
    cut = text[:max_chars]
    last_break = cut.rfind("\n\n")
    if last_break > max_chars // 2:
        cut = cut[:last_break]
    return cut.rstrip() + "\n\n……（内容过长，已截断；如需后续部分可分段抓取）", True


@tool("browse_page", parse_docstring=True)
def browse_page_tool(url: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Fetch a web page and extract its main article content (cleaned text, not raw HTML).

    Use after web_search when a result looks relevant but you need the full page content,
    or when search snippets are insufficient to answer with solid evidence.

    Args:
        url: Full http(s) URL of the page to read, usually taken from a web_search result.
        max_chars: Maximum characters of extracted content to return. Default is 6000.
    """
    try:
        _validate_public_url(url)
        final_url, _content_type, html_text = _fetch_html(url)
    except Exception as exc:  # 抓取/校验失败：结构化报错，让上层可换来源
        logger.warning("browse_page 抓取失败 %s: %s", url, exc)
        return json.dumps({"url": url, "error": f"页面抓取失败：{exc}"}, ensure_ascii=False)

    extractor = _ArticleExtractor()
    try:
        extractor.feed(html_text)
        extractor.close()
    except Exception as exc:  # 页面 HTML 畸形时至少不拖垮主循环
        logger.warning("browse_page 解析失败 %s: %s", url, exc)
        return json.dumps(
            {"url": final_url, "error": f"页面解析失败：{type(exc).__name__}"},
            ensure_ascii=False,
        )

    content, used_container = extractor.extract()
    title = extractor.title()
    payload: dict[str, Any] = {
        "url": final_url,
        "title": title,
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    if not content:
        # 正文门槛未通过（频道页/聚合页/JS 渲染页）：给 meta 描述兜底并明确告警
        payload["warning"] = "未能抽取到正文（可能是列表页、需登录或由 JavaScript 渲染）"
        if extractor.meta_description:
            payload["content"] = _normalize_text(extractor.meta_description)
            payload["char_count"] = len(payload["content"])
        else:
            payload["content"] = ""
        return json.dumps(payload, ensure_ascii=False)

    content, truncated = _truncate_at_boundary(content, max_chars)
    payload["content"] = content
    payload["char_count"] = len(content)
    if truncated:
        payload["truncated"] = True
    if not used_container:
        payload["warning"] = "未识别到明确正文容器，内容为全页抽取，可能含少量无关段落"
    return json.dumps(payload, indent=2, ensure_ascii=False)
