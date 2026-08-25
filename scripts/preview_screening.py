"""장전 스크리닝(08:50 로직)을 지금 시점 데이터로 미리 돌려보는 프리뷰 도구.

TradingEngine.pre_screen_job() 과 동일한 판정 로직(등락률순위/거래대금순위 조회 ->
전일 거래대금 500억+ & 돌파패턴 검증 -> 뉴스+AI 감성분석)을 사용하지만,
텔레그램 알림 발송과 DB 저장은 하지 않고 콘솔에만 결과를 출력한다.

사용법: .venv/bin/python scripts/preview_screening.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.ai import gemini_sentiment
from src.broker.kis_client import KISClient
from src.data import screener
from src.news import naver_news


def main():
    print("=" * 70)
    print(" 장전 스크리닝 프리뷰 (실시간 알림/DB 저장 없이 콘솔 출력만)")
    print("=" * 70)

    broker = KISClient()

    print("\n[1단계] 등락률순위/거래대금순위로 예비 후보 조회 중...")
    candidates = screener.get_prescreening_candidates(broker)
    print(f" -> 예비 후보 {len(candidates)}개: {[c['name'] for c in candidates]}")

    passed = []
    for cand in candidates:
        code, name = cand["code"], cand["name"]
        daily_info = screener.build_daily_info(broker, code)
        ok, reasons = screener.screen_stock_batch(daily_info)
        if not ok:
            print(f"\n[{name}] 탈락 - {', '.join(reasons)}")
            continue

        print(f"\n[{name}] ✅ 1차 스크리닝 통과")
        print(f"   거래대금: {daily_info['prev_trade_amount']/1e8:.1f}억원 | "
              f"종가: {daily_info['prev_close']:,.0f}원 | 고가: {daily_info['prev_high']:,.0f}원")

        news_items = naver_news.search_news(name)
        sentiment = gemini_sentiment.analyze_sentiment(name, news_items)
        print(f"   AI 감성: {sentiment['emoji']} {sentiment['sentiment']} - {sentiment['summary']}")
        if news_items:
            print(f"   관련기사: {news_items[0]['title']}")

        passed.append({"code": code, "name": name, "daily_info": daily_info, "sentiment": sentiment})

    print("\n" + "=" * 70)
    print(f" 최종 통과: {len(passed)}개 종목")
    for p in passed:
        print(f"  - {p['name']} ({p['code']}) | {p['sentiment']['emoji']} {p['sentiment']['sentiment']}")
    print("=" * 70)
    print("\n※ 이 결과는 지금 시점(장마감 후) 데이터 기준 미리보기입니다.")
    print("   내일 08:50 실제 스크리닝은 그 시점 최신 순위 데이터로 다시 계산되므로 다를 수 있습니다.")


if __name__ == "__main__":
    main()
