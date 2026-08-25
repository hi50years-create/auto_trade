"""Naver 검색 Open API - 뉴스 검색. 무료(일 25,000회)."""
from __future__ import annotations

import re

import requests

from src.config import CONFIG
from src.utils.logger import get_logger

log = get_logger("naver_news")

API_URL = "https://openapi.naver.com/v1/search/news.json"
_TAG_RE = re.compile(r"<.*?>")


def _strip_html(text: str) -> str:
    return _TAG_RE.sub("", text).replace("&quot;", '"').replace("&amp;", "&").replace("&#39;", "'")


def search_news(query: str, display: int = 5) -> list[dict]:
    """종목명으로 최신 뉴스를 검색한다. 반환: [{title, description, link, pub_date}, ...]"""
    if not CONFIG.naver_client_id or not CONFIG.naver_client_secret:
        log.warning("Naver API 키 미설정 - 뉴스 검색 스킵")
        return []
    headers = {
        "X-Naver-Client-Id": CONFIG.naver_client_id,
        "X-Naver-Client-Secret": CONFIG.naver_client_secret,
    }
    params = {"query": query, "display": display, "sort": "date"}
    try:
        res = requests.get(API_URL, headers=headers, params=params, timeout=5)
        res.raise_for_status()
        items = res.json().get("items", [])
    except requests.RequestException:
        log.exception("Naver 뉴스 검색 실패: %s", query)
        return []

    return [
        {
            "title": _strip_html(item["title"]),
            "description": _strip_html(item["description"]),
            "link": item["originallink"] or item["link"],
            "pub_date": item.get("pubDate", ""),
        }
        for item in items
    ]
