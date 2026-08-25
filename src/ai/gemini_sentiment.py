"""Google Gemini Free Tier 뉴스 감성 분석.
무료 등급 15 RPM 한도 보호를 위해 클래스 레벨 락으로 호출 간 최소 GEMINI_THROTTLE_SECONDS 간격을 강제한다.
"""
from __future__ import annotations

import json
import re
import threading
import time as time_lib

import requests

from src.config import CONFIG
from src.utils.logger import get_logger

log = get_logger("gemini_sentiment")

API_URL_TMPL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

_lock = threading.Lock()
_last_call_at = 0.0

_PROMPT_TMPL = """다음은 한국 주식 종목 '{stock_name}'에 대한 최신 뉴스 제목/요약 목록입니다.
이 뉴스를 바탕으로 아래 JSON 형식으로만 응답하세요. 다른 설명은 절대 추가하지 마세요.

{{"sentiment": "POSITIVE" 또는 "NEUTRAL" 또는 "NEGATIVE", "summary": "핵심 상승/하락 재료를 2줄 이내 한국어로 요약"}}

뉴스 목록:
{news_block}
"""


def _throttle():
    global _last_call_at
    with _lock:
        elapsed = time_lib.time() - _last_call_at
        wait = CONFIG.gemini_throttle_seconds - elapsed
        if wait > 0:
            time_lib.sleep(wait)
        _last_call_at = time_lib.time()


def _extract_json(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"JSON 형식을 찾을 수 없음: {text[:200]}")
    return json.loads(match.group(0))


def analyze_sentiment(stock_name: str, news_items: list[dict]) -> dict:
    """반환: {"sentiment": "POSITIVE"|"NEUTRAL"|"NEGATIVE", "summary": str}
    실패 시 안전한 기본값(NEUTRAL)을 반환한다 (매매 로직이 뉴스 실패로 멈추지 않도록).
    """
    fallback = {"sentiment": "NEUTRAL", "summary": "뉴스 분석 실패 - 수동 확인 필요", "emoji": "🟡"}

    if not CONFIG.gemini_api_key:
        log.warning("Gemini API 키 미설정 - 감성 분석 스킵")
        return fallback
    if not news_items:
        return {"sentiment": "NEUTRAL", "summary": "관련 뉴스 없음", "emoji": "🟡"}

    news_block = "\n".join(f"- {n['title']}: {n['description']}" for n in news_items[:5])
    prompt = _PROMPT_TMPL.format(stock_name=stock_name, news_block=news_block)

    _throttle()  # 15 RPM 무료 한도 보호 (기본 4.5초 간격)

    url = API_URL_TMPL.format(model=CONFIG.gemini_model)
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    try:
        res = requests.post(url, params={"key": CONFIG.gemini_api_key}, json=body, timeout=15)
        if res.status_code == 429:
            log.warning("Gemini 429 Too Many Requests - 쿼터 초과, 5초 대기 후 1회 재시도")
            time_lib.sleep(5)
            res = requests.post(url, params={"key": CONFIG.gemini_api_key}, json=body, timeout=15)
        res.raise_for_status()
        data = res.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = _extract_json(text)
        sentiment = parsed.get("sentiment", "NEUTRAL").upper()
        emoji = {"POSITIVE": "🟢", "NEUTRAL": "🟡", "NEGATIVE": "🔴"}.get(sentiment, "🟡")
        return {"sentiment": sentiment, "summary": parsed.get("summary", ""), "emoji": emoji}
    except Exception:
        log.exception("Gemini 감성 분석 실패: %s", stock_name)
        return fallback
