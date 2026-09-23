"""Web search tool — DuckDuckGo 与 Bing 中国版并发竞速，自动适配网络环境。

- DuckDuckGo（ddgs 库，无需 key）：海外或有代理的环境可用。
- Bing 中国版（cn.bing.com，标准库 urllib，无需 key）：国内服务器可用。
- 两者并发执行，谁先返回有效结果就用谁；都失败才报错。
"""

from __future__ import annotations

import html
import json
import logging
import re
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

DEFAULT_MAX_RESULTS = 5
DEFAULT_REGION = "wt-wt"
DEFAULT_SAFESEARCH = "moderate"

# Bing 中国版（国内服务器可直连，无需备案/API key）
BING_ENDPOINT = "https://cn.bing.com/search"
BING_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 模块级线程池：复用线程，避免每次搜索新建/泄漏线程
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="websearch")


def _strip_tags(fragment: str) -> str:
    """去掉 HTML 标签并反转义实体。"""
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def _normalize(query: str, engine: str, rows: list[dict[str, str]]) -> str:
    return json.dumps(
        {
            "query": query,
            "engine": engine,
            "total_results": len(rows),
            "results": rows,
        },
        indent=2,
        ensure_ascii=False,
    )


def _search_ddg(query: str, max_results: int) -> list[dict[str, str]]:
    """DuckDuckGo 搜索（海外/代理环境）。失败或无结果返回空列表/抛异常。"""
    from ddgs import DDGS

    ddgs = DDGS(timeout=15)
    raw = ddgs.text(
        query,
        region=DEFAULT_REGION,
        safesearch=DEFAULT_SAFESEARCH,
        max_results=max_results,
    )
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("href", r.get("link", "")),
            "content": r.get("body", r.get("snippet", "")),
        }
        for r in (raw or [])
    ]


def _search_bing(query: str, max_results: int) -> list[dict[str, str]]:
    """Bing 中国版 HTML 搜索（国内服务器直连），用标准库解析 b_algo 结果块。"""
    params = urlencode({"q": query, "count": str(max_results), "setlang": "zh-cn", "mkt": "zh-CN"})
    request = Request(f"{BING_ENDPOINT}?{params}", headers=BING_HEADERS)
    with urlopen(request, timeout=15) as response:  # noqa: S310 - 固定的官方域名
        charset = response.headers.get_content_charset() or "utf-8"
        page = response.read().decode(charset, errors="replace")

    rows: list[dict[str, str]] = []
    for block in re.findall(r'<li class="b_algo".*?</li>', page, re.S):
        link = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not link:
            continue
        url, title = link.group(1), _strip_tags(link.group(2))
        if not url or not title:
            continue
        para = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        snippet = _strip_tags(para.group(1)) if para else ""
        rows.append({"title": title, "url": url, "content": snippet})
        if len(rows) >= max_results:
            break
    return rows


@tool("web_search", parse_docstring=True)
def web_search_tool(query: str, max_results: int = DEFAULT_MAX_RESULTS) -> str:
    """Search the web for information. Use this tool to find current information, news, articles, and facts from the internet.

    Args:
        query: Search keywords describing what you want to find. Be specific for better results.
        max_results: Maximum number of results to return. Default is 5.
    """
    engines: dict[str, Any] = {
        "duckduckgo": _EXECUTOR.submit(_search_ddg, query, max_results),
        "bing": _EXECUTOR.submit(_search_bing, query, max_results),
    }

    pending = set(engines.values())
    errors: list[str] = []
    while pending:
        done, pending = wait(pending, return_when=FIRST_COMPLETED)
        for future in done:
            engine_name = next(name for name, fut in engines.items() if fut is future)
            try:
                rows = future.result()
            except Exception as exc:  # 单个引擎失败不影响另一个
                logger.warning("web_search 引擎 %s 失败: %s", engine_name, exc)
                errors.append(f"{engine_name}: {type(exc).__name__}")
                continue
            if rows:
                # 取消还没开始的备用任务（已在跑的会自己结束，不阻塞返回）
                for other in pending:
                    other.cancel()
                return _normalize(query, engine_name, rows)
            errors.append(f"{engine_name}: 无结果")

    return json.dumps(
        {
            "error": f"No results found. 所有搜索引擎均失败（{'; '.join(errors)}），请检查服务器网络或更换关键词",
            "query": query,
        },
        ensure_ascii=False,
    )
