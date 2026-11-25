"""外部信息类工具：网页搜索与页面抓取。"""

from __future__ import annotations

import re


def web_search(query: str, max_results: int = 5) -> list[dict]:
    """网页搜索（cn.bing.com），返回 [{title, url, snippet}]。"""
    import html as html_mod
    try:
        import httpx
        resp = httpx.get(
            "https://cn.bing.com/search",
            params={"q": query, "count": max_results},
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                 "AppleWebKit/537.36 (KHTML, like Gecko) "
                                 "Chrome/124.0 Safari/537.36",
                     "Accept-Language": "zh-CN,zh;q=0.9"},
            follow_redirects=True,
        )
        resp.raise_for_status()
        page = resp.text
        results: list[dict] = []
        for block in re.findall(r'<li class="b_algo"[\s\S]*?</li>', page)[:max_results]:
            m = re.search(r'<h2><a[^>]*href="([^"]+)"[^>]*>([\s\S]*?)</a></h2>', block)
            if not m:
                continue
            url, title = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()
            sm = re.search(r"<p[\s\S]*?>([\s\S]*?)</p>", block)
            snippet = re.sub(r"<[^>]+>", "", sm.group(1)).strip() if sm else ""
            results.append({"title": html_mod.unescape(title),
                            "url": url,
                            "snippet": html_mod.unescape(snippet)[:300]})
        return results or {"error": "搜索无结果，建议换关键词"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"搜索失败: {e}"}


def fetch_url(url: str) -> str:
    """抓取网页并提取正文文本（截断）。"""
    try:
        import httpx
        resp = httpx.get(url, timeout=20, follow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0 (compatible; agent-pipeline/0.1)"})
        resp.raise_for_status()
        text = resp.text
        text = re.sub(r"<script[\s\S]*?</script>", "", text)
        text = re.sub(r"<style[\s\S]*?</style>", "", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:8000]
    except Exception as e:  # noqa: BLE001
        return f"抓取失败: {e}"
