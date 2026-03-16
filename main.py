# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import re
import json
import asyncio
from datetime import datetime
from typing import Optional

import feedparser
import anthropic
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Junction Magazine API")

# 静的ファイル配信（/static/* で static フォルダ内を配信）
_static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

RSS_FEEDS = {
    "HYPEBEAST JP": "https://hypebeast.com/jp/feed",
    "WWD JAPAN": "https://www.wwdjapan.com/rss",
}

_cache: dict = {"articles": [], "last_updated": None}


def _extract_thumbnail(entry) -> str | None:
    if hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
        return entry.media_thumbnail[0].get("url")
    if hasattr(entry, "enclosures") and entry.enclosures:
        for enc in entry.enclosures:
            if enc.get("type", "").startswith("image/"):
                return enc.get("href") or enc.get("url")
    for field in ("summary", "content"):
        raw = entry.get(field, "")
        if isinstance(raw, list):
            raw = raw[0].get("value", "") if raw else ""
        m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', raw)
        if m:
            return m.group(1)
    return None


def _fetch_rss() -> list[dict]:
    articles = []
    for source, url in RSS_FEEDS.items():
        feed = feedparser.parse(url)
        for entry in feed.entries[:10]:
            summary_raw = entry.get("summary", "")
            if isinstance(summary_raw, list):
                summary_raw = summary_raw[0].get("value", "") if summary_raw else ""
            summary_clean = re.sub(r"<[^>]+>", "", summary_raw)[:300]
            articles.append({
                "source": source,
                "title": entry.get("title", ""),
                "link": entry.get("link", ""),
                "published": entry.get("published", entry.get("updated", "")),
                "thumbnail": _extract_thumbnail(entry),
                "original_summary": summary_clean,
                "brands": [],
                "category": "その他",
                "ai_summary": summary_clean[:80],
                "trend_keywords": [],
            })
    return articles


def _analyze_with_claude(article: dict) -> dict:
    prompt = f"""以下のファッション記事を分析して、JSON形式で結果を返してください。JSONのみを返し、それ以外のテキストは一切含めないでください。

【記事タイトル】
{article['title']}

【記事の概要】
{article['original_summary']}

【出力するJSON形式】
{{
  "brands": ["記事に登場するブランド名を配列で。なければ空配列"],
  "category": "以下から1つ選択: ストリート / ラグジュアリー / スニーカー / ビューティー / デザイナー / コラボ / ビジネス / カルチャー / その他",
  "summary": "記事の内容を30〜60文字で要約",
  "trend_keywords": ["トレンドキーワードを1〜3個"]
}}"""

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    text = message.content[0].text.strip()
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


async def _analyze_one(article: dict, loop: asyncio.AbstractEventLoop, sem: asyncio.Semaphore) -> dict:
    async with sem:
        try:
            result = await loop.run_in_executor(None, _analyze_with_claude, article)
            article["brands"] = result.get("brands", [])
            article["category"] = result.get("category", "その他")
            article["ai_summary"] = result.get("summary", article["ai_summary"])
            article["trend_keywords"] = result.get("trend_keywords", [])
        except Exception as e:
            print(f"[analyze error] {article['title'][:30]}: {e}")
        return article


async def _do_refresh() -> list[dict]:
    loop = asyncio.get_event_loop()
    sem = asyncio.Semaphore(3)
    articles = await loop.run_in_executor(None, _fetch_rss)
    analyzed = await asyncio.gather(*[_analyze_one(a, loop, sem) for a in articles])
    _cache["articles"] = list(analyzed)
    _cache["last_updated"] = datetime.now().isoformat()
    return _cache["articles"]


@app.get("/api/articles")
async def get_articles():
    if not _cache["articles"]:
        await _do_refresh()
    return {
        "articles": _cache["articles"],
        "last_updated": _cache["last_updated"],
        "total": len(_cache["articles"]),
    }


@app.post("/api/refresh")
async def post_refresh():
    articles = await _do_refresh()
    return {
        "articles": articles,
        "last_updated": _cache["last_updated"],
        "total": len(articles),
    }
