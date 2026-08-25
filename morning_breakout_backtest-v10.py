import pandas as pd
import numpy as np
import time as time_lib
from datetime import datetime, time

class MorningBreakoutBacktesterV10:
    """
    5분 시초가 돌파 매매 전략 시뮬레이터 (Backtesting Engine) - v10 (완결판)
    
    [핵심 원칙 및 기업 표준 변수 반영]
    - PREV_BIZDAY_DDBAR_CLSPRC: 전 영업일 일봉 종가
    - PREV_BIZDAY_DDBAR_HGHPRC: 전 영업일 일봉 고가
    - PREV_BIZDAY_DDBAR_TRPRC: 전 영업일 일봉 거래대금
    - DAY_OPNPRC: 당일 확정 시초가
    - RT_CURPRC: 실시간 현재가
    - CUR_M3BAR_OPNPRC: 현재 3분봉 시가
    - CUR_M3BAR_CLSPRC: 현재 3분봉 종가
    - CUR_M3BAR_TRAMT: 현재 3분봉 거래량
    - GAP_UP_PCT: 당일 시가 갭상승률 (%)
    - CUR_VOL_POWER: 실시간 체결강도 (%)
    - ASK_BID_RATIO: 실시간 총 호가잔량비 (%)
    - KOSPI_CHANGE_PCT / KOSDAQ_CHANGE_PCT: 코스피/코스닥 지수 변동률 (%)
    
    [v10 실전 안전 필터 반영]
    1. 진입 제한 시간: 신규 매수는 오직 09:00 ~ 09:30까지만 가능 (이후 신규 진입 원천 금지)
    2. 슬롯 기반 자금 관리: 최대 동시 보유 가능 종목 수 3개 (3 슬롯 제한)
    3. 종합주가지수 필터: 코스피 또는 코스닥이 전일 대비 -1.2% 이하 폭락 상태 시 당일 전체 거래 완전 중지
    """
    
    def __init__(self, target_profit_pct=0.05, stop_loss_pct=-0.03, slippage_pct=0.002, max_slots=3):
        self.target_profit_pct = target_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.slippage_pct = slippage_pct # 실거래 주문 오차 모델링 (0.2% 슬리피지 기본 가산)
        self.max_slots = max_slots       # 최대 진입 가능 슬롯 제한 (3슬롯)
        self.active_slots = {}           # 현재 포지션 보유 중인 종목 관리
        self.trades = []
        
    def screen_stock_batch(self, daily_data):
        """
        [1단계: 08:30 ~ 08:50] 일봉 기준 1차 예비 후보 스크리닝
        """
        reasons = []
        
        # 1. 전일 거래대금 조건 (PREV_BIZDAY_DDBAR_TRPRC >= 500억)
        PREV_BIZDAY_DDBAR_TRPRC = daily_data['PREV_BIZDAY_DDBAR_TRPRC']
        if PREV_BIZDAY_DDBAR_TRPRC < 50_000_000_000:
            reasons.append(f"전일 거래대금 부족 ({PREV_BIZDAY_DDBAR_TRPRC/1e8:.1f}억 < 500억)")
            
        # 2. 돌파/상한가 여부
        if not daily_data['is_breakout_or_limit_up']:
            reasons.append("전일 주요 저항선/상한가 돌파 패턴 아님")
            
        is_passed = len(reasons) == 0
        return is_passed, reasons

    def check_market_index(self, kospi_change, kosdaq_change):
        """
        [지수 필터링] 코스피/코스닥 변동률 중 하나라도 -1.2% 이하 폭락 상태이면 당일 전체 자동매매 차단
        """
        if kospi_change <= -1.2 or kosdaq_change <= -1.2:
            return False, f"종합주가지수 폭락 감지 (KOSPI: {kospi_change:.2f}%, KOSDAQ: {kosdaq_change:.2f}%)"
        return True, "지수 정상 범위"

    def run_backtest(self, stock_name, daily_info, intraday_df, market_indices):
        """
        개별 종목에 대한 당일 분봉 기반 백테스팅 및 실시간 지수/시간/자금 관리 연동 시뮬레이션
        """
        # 0. 지수 필터 체크 (09:00:00 판정)
        KOSPI_CHANGE_PCT = market_indices['KOSPI']
        KOSDAQ_CHANGE_PCT = market_indices['KOSDAQ']
        index_ok, index_reason = self.check_market_index(KOSPI_CHANGE_PCT, KOSDAQ_CHANGE_PCT)
        
        if not index_ok:
            print(f"\n🛑 [종합주가지수 보호막 작동] 당일 시장 폭락 상태로 모든 자동매매를 즉시 완전 중단합니다.")
            print(f"  └ 원인: {index_reason} <= -1.2%")
            print(f"  💬 [텔레그램 PUSH] 🚨 [Emergency Block] 시장 전체 투매 및 급락 감지로 당일 모든 거래를 안전하게 보류합니다. 현금 비중 100% 보존.")
            return None

        # 1단계: 08:50 장 시작 전 배치 스크리닝
        batch_passed, batch_reasons = self.screen_stock_batch(daily_info)
        if not batch_passed:
            print(f"[{stock_name}] [08:50] 1차 예비 스크리닝 탈락 - 사유: {', '.join(batch_reasons)}")
            return None
            
        PREV_BIZDAY_DDBAR_TRPRC = daily_info['PREV_BIZDAY_DDBAR_TRPRC']
        print(f"[{stock_name}] [08:50] 1차 예비 스크리닝 통과! (전일 거래대금: {PREV_BIZDAY_DDBAR_TRPRC/1e8:.1f}억)")
        
        # [08:50 텔레그램 알림] 무료 뉴스 연동 및 AI 감성 분석 (Gemini Free Tier 가상화)
        news_title = daily_info.get('mock_news_title', "시장 주도주 관련 수급 유입 중")
        news_sentiment = daily_info.get('mock_news_sentiment', "🟢 긍정")
        news_url = daily_info.get('mock_news_url', "https://finance.naver.com")
        
        print(f"\n💬 [텔레그램 PUSH 알림 - 0원 API] 📢 [08:50] 1차 예비 스크리닝 통과 완료")
        print(f"  종목명: {stock_name}")
        print(f"  - 전일 거래대금: {PREV_BIZDAY_DDBAR_TRPRC/1e8:.1f}억원 (기준: 500억 돌파)")
        print(f"  - 차트 패턴: 전일 돌파/상한가 포착 완료")
        print(f"  - 📝 AI 뉴스 분석 ({news_sentiment} - Gemini Free): {news_title}")
        print(f"  - 🔗 기사 원천 링크 (Naver API): {news_url}")
        print("="*60)

        # 2단계: 09:00:00 장 시작 직후 시가 갭 판정
        DAY_OPNPRC = daily_info['DAY_OPNPRC']
        PREV_BIZDAY_DDBAR_CLSPRC = daily_info['PREV_BIZDAY_DDBAR_CLSPRC']
        GAP_UP_PCT = ((DAY_OPNPRC - PREV_BIZDAY_DDBAR_CLSPRC) / PREV_BIZDAY_DDBAR_CLSPRC) * 100
        
        if GAP_UP_PCT > 5.0:
            print(f"[{stock_name}] [09:00] 2차 시가 갭 스크리닝 탈락 - 사유: 시가 갭 과도 ({GAP_UP_PCT:.2f}% > 5.0%)")
            print(f"\n💬 [텔레그램 PUSH 알림 - 0원 API] ❌ [09:00] 2차 시가 갭 스크리닝 제외")
            print(f"  종목명: {stock_name}")
            print(f"  - 사유: 시가 갭 과도 ({GAP_UP_PCT:.2f}% > 5.0%)")
            print(f"  - 확정 시가: {DAY_OPNPRC:,.0f}원 (갭 비율: +{GAP_UP_PCT:.2f}%)")
            print("="*60)
            return None
            
        print(f"[{stock_name}] [09:00] 2차 시가 갭 스크리닝 통과! (확정 시가: {DAY_OPNPRC:,.0f}원, 시가 갭: {GAP_UP_PCT:.2f}%)")
        print(f"  └ [알림] 실거래 슬리피지 보정값 적용 활성화 (진입가에 +{self.slippage_pct*100:.2f}% 가산)")
        
        # [09:00 텔레그램 알림] 감시 개시 보고
        print(f"\n💬 [텔레그램 PUSH 알림 - 0원 API] 🔔 [09:00] 시가 갭 필터 통과 ➔ 실시간 매수 감시 개시")
        print(f"  종목명: {stock_name}")
        print(f"  - 확정 시가: {DAY_OPNPRC:,.0f}원 (갭 비율: +{GAP_UP_PCT:.2f}%)")
        print(f"  - 현재 종합지수: KOSPI {KOSPI_CHANGE_PCT:+.2f}%, KOSDAQ {KOSDAQ_CHANGE_PCT:+.2f}% (지수 통과)")
        print(f"  - 시스템 감시 상태: [STATE_WAIT_FOR_BREAKOUT]")
        print(f"  - 수급 필터 기준: 체결강도 100% 이상, 호가잔량비 120% 이상 동시 만족 필수")
        print(f"  - 3분봉 시가 이탈 후 양봉 재돌파 수식을 실시간 감시합니다.")
        print("="*60)
        
        # 변수 초기화
        state = "IDLE"  # IDLE -> SHOOK_OUT (시가이탈) -> POSITION_HOLDING -> CLOSED
        buy_price = 0
        shook_out_triggered = False
        
        # 3분봉 루프 탐색
        for idx, row in intraday_df.iterrows():
            current_time_str = row['time']
            # 시간 형식 파싱
            h, m = map(int, current_time_str.split(':'))
            current_time = time(h, m)
            
            # 매핑 변수 적용
            CUR_M3BAR_OPNPRC = row['open']
            CUR_M3BAR_CLSPRC = row['close']
            CUR_M3BAR_TRAMT = row['volume']
            RT_CURPRC = row['close']  # 봉 마감 현재가를 실시간 현재가로 시뮬레이션
            
            high_price = row['high']
            low_price = row['low']
            
            # A. 매수 대기 상태
            if state == "IDLE":
                # 시가 아래로 이탈했는지 확인 (개미털기)
                if RT_CURPRC < DAY_OPNPRC or low_price < DAY_OPNPRC:
                    shook_out_triggered = True
                    state = "SHOOK_OUT"
                    print(f"  └ [{current_time_str}] 시가 이탈 확인 (개미털기 구간 진입) - 최저가: {low_price:,.0f}원")
                    
            elif state == "SHOOK_OUT":
                # 시가 재돌파 조건 탐색
                is_cross_above = (CUR_M3BAR_OPNPRC < DAY_OPNPRC and CUR_M3BAR_CLSPRC >= DAY_OPNPRC) or \
                                 (CUR_M3BAR_OPNPRC >= DAY_OPNPRC and CUR_M3BAR_CLSPRC >= DAY_OPNPRC and low_price < DAY_OPNPRC)
                is_bullish = CUR_M3BAR_CLSPRC > CUR_M3BAR_OPNPRC
                candle_body_pct = (CUR_M3BAR_CLSPRC - CUR_M3BAR_OPNPRC) / CUR_M3BAR_OPNPRC
                is_volume_ok = CUR_M3BAR_TRAMT >= 10_000
                
                # 무료 실시간 수급 지표 가상 연동 (체결강도 100% 이상, 호가잔량비 120% 이상)
                CUR_VOL_POWER = row.get('vol_power', 130.0)  # 기본 수치 우수
                ASK_BID_RATIO = row.get('ask_bid_ratio', 180.0) # 기본 수치 우수
                is_supply_demand_ok = CUR_VOL_POWER >= 100.0 and ASK_BID_RATIO >= 120.0
                
                if is_cross_above and is_bullish and candle_body_pct >= 0.02 and is_volume_ok and is_supply_demand_ok:
                    
                    # [v10 진입 제한 시간 필터 작동] 09:30:00 초과 여부 확인
                    if current_time > time(9, 30):
                        print(f"  ⚠️ [{current_time_str}] 매수 조건 충족했으나 신규 진입 시간 초과로 매매 보류 (09:30 진입 제한 필터 작동)")
                        print(f"\n💬 [텔레그램 PUSH 알림 - 0원 API] 🚫 [진입 보류] 신규 진입 제한 시간 경과")
                        print(f"  종목명: {stock_name}")
                        print(f"  - 현재시간: {current_time_str} (신규 진입은 09:30 까지만 가능)")
                        print(f"  - 조치: 돌파 신호를 인지하였으나 시스템 규칙에 따라 자동 패스합니다. (뇌동매매 방지)")
                        print("="*60)
                        state = "CLOSED"
                        break
                        
                    # [v10 슬롯 기반 자금 관리 규칙 필터 작동] 3개 슬롯 초과 여부 확인
                    if len(self.active_slots) >= self.max_slots:
                        print(f"  ⚠️ [{current_time_str}] 매수 조건 충족했으나 가용 투자금 슬롯(Slot) 부족으로 매매 대기 (보보유 중인 종목 수 {len(self.active_slots)}개)")
                        print(f"\n💬 [텔레그램 PUSH 알림 - 0원 API] ⏳ [진입 대기] 가용 투자 슬롯 초과")
                        print(f"  종목명: {stock_name}")
                        print(f"  - 보유 현황: 총 {self.max_slots}개 포지션 풀가동 중")
                        print(f"  - 조치: 자금 배분 보호 원칙에 따라 신규 진입을 제한합니다. 기존 종목 청산 후 신규 슬롯 개방 가능.")
                        print("="*60)
                        state = "CLOSED"
                        break
                    
                    # 모든 필터를 통과하여 최종 매수 성공
                    signal_price = CUR_M3BAR_CLSPRC
                    buy_price = signal_price * (1 + self.slippage_pct)
                    state = "POSITION_HOLDING"
                    entry_time = current_time_str
                    self.active_slots[stock_name] = buy_price # 슬롯 점유
                    
                    print(f"  ★ [{current_time_str}] 매수 신호 발생 및 주문 체결 완료!")
                    print(f"    - 신호 발생가: {signal_price:,.0f}원 ➔ 실체결가 (슬리피지 반영): {buy_price:,.1f}원 (슬리피지 비용: {buy_price - signal_price:.1f}원)")
                    print(f"    - 돌파봉 상승률: {candle_body_pct*100:.2f}%, 거래량: {CUR_M3BAR_TRAMT:,}주")
                    print(f"    - 진입 시 수급현황: 체결강도 {CUR_VOL_POWER:.1f}%, 호가잔량비 {ASK_BID_RATIO:.1f}%")
                    print(f"    - 점유된 슬롯 수: {len(self.active_slots)} / {self.max_slots}")
                    
                    # [진입 성공 텔레그램 알림]
                    print(f"\n💬 [텔레그램 PUSH 알림 - 0원 API] 🛒 [실시간 체결 알림] 시가 돌파 매수 성공")
                    print(f"  종목명: {stock_name}")
                    print(f"  - 진입시간: {current_time_str} (3분봉 완료)")
                    print(f"  - 신호 발생가: {signal_price:,.0f}원 ➔ 실체결가: {buy_price:,.1f}원")
                    print(f"  - 돌파 캔들 상승률: {candle_body_pct*100:.2f}% | 거래량: {CUR_M3BAR_TRAMT:,}주")
                    print(f"  - 진입시점 수급: 체결강도 {CUR_VOL_POWER:.1f}% | 호가잔량비 {ASK_BID_RATIO:.1f}%")
                    print(f"  - 투자 현황: {len(self.active_slots)}/{self.max_slots} 슬롯 구동 중")
                    print("="*60)
                    continue
            
            # B. 포지션 보유 상태 (매수 이후 익절/손절 실시간 체크)
            elif state == "POSITION_HOLDING":
                # 1. 고정 익절 조건 체크 (목표가 대비 +5%)
                tp_price = buy_price * (1 + self.target_profit_pct)
                if high_price >= tp_price:
                    profit_pct = self.target_profit_pct * 100
                    self.trades.append({
                        'stock': stock_name,
                        'entry_time': entry_time,
                        'exit_time': current_time_str,
                        'buy_price': buy_price,
                        'sell_price': tp_price,
                        'profit_pct': profit_pct,
                        'result': '익절 (Target)'
                    })
                    state = "CLOSED"
                    if stock_name in self.active_slots:
                        del self.active_slots[stock_name] # 슬롯 반환
                    print(f"  ▶ [{current_time_str}] 목표 익절 달성! 청산 가격: {tp_price:,.1f}원 (수익률: +{profit_pct:.2f}%)")
                    
                    # [익절 성공 텔레그램 알림]
                    print(f"\n💬 [텔레그램 PUSH 알림 - 0원 API] 💰 [실시간 청산] 목표 익절 청산 완료")
                    print(f"  종목명: {stock_name}")
                    print(f"  - 청산 사유: 목표 수익률 (+5%) 도달")
                    print(f"  - 청산시간: {current_time_str}")
                    print(f"  - 매수가: {buy_price:,.1f}원 ➔ 청산가: {tp_price:,.1f}원")
                    print(f"  - 최종 확정 수익률: +{profit_pct:.2f}%")
                    print(f"  - 현재 잔여 슬롯: {len(self.active_slots)}/{self.max_slots}")
                    print("="*60)
                    break
                
                # 2. 고정 손절 조건 체크 (손절가 대비 -3%)
                sl_price = buy_price * (1 + self.stop_loss_pct)
                if low_price <= sl_price:
                    loss_pct = self.stop_loss_pct * 100
                    self.trades.append({
                        'stock': stock_name,
                        'entry_time': entry_time,
                        'exit_time': current_time_str,
                        'buy_price': buy_price,
                        'sell_price': sl_price,
                        'profit_pct': loss_pct,
                        'result': '손절 (Stop Loss)'
                    })
                    state = "CLOSED"
                    if stock_name in self.active_slots:
                        del self.active_slots[stock_name] # 슬롯 반환
                    print(f"  💔 [{current_time_str}] 고정 손절선 이탈! 청산 가격: {sl_price:,.1f}원 (수익률: {loss_pct:.2f}%)")
                    
                    # [손절 텔레그램 알림]
                    print(f"\n💬 [텔레그램 PUSH 알림 - 0원 API] 💔 [실시간 청산] 고정 손절 실행")
                    print(f"  종목명: {stock_name}")
                    print(f"  - 청산 사유: 최대 고정 손절률 (-3%) 감지")
                    print(f"  - 청산시간: {current_time_str}")
                    print(f"  - 매수가: {buy_price:,.1f}원 ➔ 손절가: {sl_price:,.1f}원")
                    print(f"  - 최종 확정 수익률: {loss_pct:.2f}%")
                    print(f"  - 현재 잔여 슬롯: {len(self.active_slots)}/{self.max_slots}")
                    print("="*60)
                    break
                
                # 3. [예방] 당일 시가 실시간 붕괴 조건 체크 (재이탈 시 즉각 실시간 손절)
                if RT_CURPRC < DAY_OPNPRC:
                    loss_pct = ((DAY_OPNPRC - buy_price) / buy_price) * 100
                    self.trades.append({
                        'stock': stock_name,
                        'entry_time': entry_time,
                        'exit_time': current_time_str,
                        'buy_price': buy_price,
                        'sell_price': DAY_OPNPRC,
                        'profit_pct': loss_pct,
                        'result': '실시간 시가 이탈 손절'
                    })
                    state = "CLOSED"
                    if stock_name in self.active_slots:
                        del self.active_slots[stock_name] # 슬롯 반환
                    print(f"  ⚠️ [{current_time_str}] [실시간 감시] 주가가 당일 시가({DAY_OPNPRC:,.0f}원)를 하향 돌파했습니다!")
                    print(f"     └ 종가 마감 대기 없이 즉각 시장가 칼손절 처리! ➔ 청산 가격: {DAY_OPNPRC:,.0f}원 (수익률: {loss_pct:.2f}%)")
                    
                    # [시가 재이탈 손절 텔레그램 알림]
                    print(f"\n💬 [텔레그램 PUSH 알림 - 0원 API] 🚨 [실시간 대응] 손절 청산 집행 (시가 재이탈)")
                    print(f"  종목명: {stock_name}")
                    print(f"  - 청산 사유: 당일 시가 {DAY_OPNPRC:,.0f}원 하향 붕괴 감지 즉시 대응")
                    print(f"  - 청산시간: {current_time_str}")
                    print(f"  - 매수가: {buy_price:,.1f}원 ➔ 손절가: {DAY_OPNPRC:,.0f}원")
                    print(f"  - 최종 확정 수익률: {loss_pct:.2f}%")
                    print(f"  - 현재 잔여 슬롯: {len(self.active_slots)}/{self.max_slots}")
                    print("="*60)
                    break

        if state == "POSITION_HOLDING":
            # 장마감 청산 (편의상 분봉 데이터 종료 시 청산)
            last_bar = intraday_df.iloc[-1]
            exit_price = last_bar['close']
            profit_pct = ((exit_price - buy_price) / buy_price) * 100
            self.trades.append({
                'stock': stock_name,
                'entry_time': entry_time,
                'exit_time': last_bar['time'],
                'buy_price': buy_price,
                'sell_price': exit_price,
                'profit_pct': profit_pct,
                'result': '장마감 동시청산'
            })
            if stock_name in self.active_slots:
                del self.active_slots[stock_name]
            print(f"  ⏱️ [{last_bar['time']}] 장마감 시간 도달로 포지션 청산! 청산 가격: {exit_price:,.0f}원 (수익률: {profit_pct:.2f}%)")


def run_simulation_demo():
    print("="*60)
    print("   5분 시초가 breakout 전략 - 백테스팅 엔진 시뮬레이션 시작 (v10 - 최종 완결판)")
    print("="*60)
    
    # ----------------- 가상 종합 주가 지수 정보 -----------------
    normal_market_indices = {'KOSPI': 0.35, 'KOSDAQ': 0.12} # 정상 시장 변동률
    crashed_market_indices = {'KOSPI': -1.45, 'KOSDAQ': -1.89} # 폭락 시장 변동률 (-1.2% 이하)
    
    # ----------------- 가상 종목 데이터 생성 -----------------
    
    # Stock A (에이비씨전자): 09:06에 정상 진입하여 09:09에 익절 성공 (+5%)
    daily_a = {
        'PREV_BIZDAY_DDBAR_CLSPRC': 10000,
        'PREV_BIZDAY_DDBAR_HGHPRC': 10000,
        'PREV_BIZDAY_DDBAR_TRPRC': 160_000_000_000, 
        'DAY_OPNPRC': 10200,                         
        'is_breakout_or_limit_up': True,
        'mock_news_title': "차세대 HBM4 공급업체 독점 수혜 유력 및 박스권 상단 돌파 상한가 마감",
        'mock_news_sentiment': "🟢 긍정",
        'mock_news_url': "https://finance.naver.com/item/news_read.naver?article_id=abc001"
    }
    intraday_a = pd.DataFrame([
        {'time': '09:00', 'open': 10200, 'high': 10250, 'low': 10100, 'close': 10150, 'volume': 8000, 'vol_power': 95.0, 'ask_bid_ratio': 110.0}, 
        {'time': '09:03', 'open': 10150, 'high': 10150, 'low': 9850,  'close': 9900,  'volume': 12000, 'vol_power': 105.0, 'ask_bid_ratio': 130.0}, 
        {'time': '09:06', 'open': 9900,  'high': 10250, 'low': 9900,  'close': 10250, 'volume': 15000, 'vol_power': 145.0, 'ask_bid_ratio': 210.0}, # 매수진입
        {'time': '09:09', 'open': 10250, 'high': 10800, 'low': 10200, 'close': 10700, 'volume': 35000, 'vol_power': 180.0, 'ask_bid_ratio': 250.0}, # 익절
        {'time': '09:12', 'open': 10700, 'high': 11000, 'low': 10600, 'close': 10900, 'volume': 20000, 'vol_power': 150.0, 'ask_bid_ratio': 170.0}
    ])

    # Stock B (바이오메디): 09:03 진입했으나, 09:06에 지지 실패하여 손절 (-3%)
    daily_b = {
        'PREV_BIZDAY_DDBAR_CLSPRC': 5000,
        'PREV_BIZDAY_DDBAR_HGHPRC': 5000,
        'PREV_BIZDAY_DDBAR_TRPRC': 130_000_000_000, 
        'DAY_OPNPRC': 5100,                          
        'is_breakout_or_limit_up': True,
        'mock_news_title': "FDA 신약 후보물질 임상 3상 조기 승인 및 글로벌 합작 법인 설립 소식",
        'mock_news_sentiment': "🟢 긍정",
        'mock_news_url': "https://finance.naver.com/item/news_read.naver?article_id=bio777"
    }
    intraday_b = pd.DataFrame([
        {'time': '09:00', 'open': 5100, 'high': 5150, 'low': 4950, 'close': 4980, 'volume': 7000, 'vol_power': 85.0, 'ask_bid_ratio': 90.0},  
        {'time': '09:03', 'open': 4980, 'high': 5220, 'low': 4980, 'close': 5220, 'volume': 11000, 'vol_power': 115.0, 'ask_bid_ratio': 130.0}, # 매수진입
        {'time': '09:06', 'open': 5220, 'high': 5250, 'low': 4900, 'close': 4900, 'volume': 18000, 'vol_power': 65.0, 'ask_bid_ratio': 55.0}   # 손절
    ])

    # Stock C (지오신소재): 09:33에 시가 재돌파가 발생하지만, "09:30 진입 제한 시간 필터"에 의해 자동 진입 차단 처리
    daily_c = {
        'PREV_BIZDAY_DDBAR_CLSPRC': 15000,
        'PREV_BIZDAY_DDBAR_HGHPRC': 15000,
        'PREV_BIZDAY_DDBAR_TRPRC': 90_000_000_000,  
        'DAY_OPNPRC': 15200,                        
        'is_breakout_or_limit_up': True,
        'mock_news_title': "친환경 고효율 경량 신소재 특허 획득 및 양산 라인 가동",
        'mock_news_sentiment': "🟢 긍정",
        'mock_news_url': "https://finance.naver.com/item/news_read.naver?article_id=geo555"
    }
    intraday_c = pd.DataFrame([
        {'time': '09:00', 'open': 15200, 'high': 15250, 'low': 14900, 'close': 14950, 'volume': 5000, 'vol_power': 90.0, 'ask_bid_ratio': 90.0},  
        {'time': '09:15', 'open': 14950, 'high': 15000, 'low': 14700, 'close': 14750, 'volume': 8000, 'vol_power': 95.0, 'ask_bid_ratio': 110.0}, 
        {'time': '09:30', 'open': 14750, 'high': 15000, 'low': 14750, 'close': 14900, 'volume': 12000, 'vol_power': 105.0, 'ask_bid_ratio': 115.0}, 
        {'time': '09:33', 'open': 14900, 'high': 15300, 'low': 14900, 'close': 15300, 'volume': 22000, 'vol_power': 155.0, 'ask_bid_ratio': 230.0} # 09:33 돌파신호 발생 (시간 초과로 무시되어야 함)
    ])

    # ----------------- 백테스팅 실행 -----------------
    backtester = MorningBreakoutBacktesterV10()
    
    # 정상 작동 시나리오
    backtester.run_backtest("에이비씨전자", daily_a, intraday_a, normal_market_indices)
    print("-" * 50)
    backtester.run_backtest("바이오메디", daily_b, intraday_b, normal_market_indices)
    print("-" * 50)
    backtester.run_backtest("지오신소재", daily_c, intraday_c, normal_market_indices)
    
    # ----------------- 폭락장 시나리오 테스트 -----------------
    print("\n" + "="*60)
    print("      🚨 테스트 시나리오 2: 종합주가지수 폭락 발생시 (-1.2% 이하)")
    print("="*60)
    # 폭락 시장 데이터를 입력할 경우, 1차 스크리닝이 완료되더라도 09시 지수 검증 단계에서 모든 종목 진입을 차단합니다.
    backtester.run_backtest("에이비씨전자", daily_a, intraday_a, crashed_market_indices)
    
    # ----------------- 결과 집계 -----------------
    print("\n" + "="*60)
    print("                 최종 시뮬레이션 성과 보고서 (v10)")
    print("="*60)
    if backtester.trades:
        df_trades = pd.DataFrame(backtester.trades)
        print(df_trades.to_string(index=False))
        
        total_trades = len(df_trades)
        winning_trades = len(df_trades[df_trades['profit_pct'] > 0])
        win_rate = (winning_trades / total_trades) * 100
        avg_return = df_trades['profit_pct'].mean()
        
        print("-" * 60)
        print(f"총 거래 횟수: {total_trades}회")
        print(f"익절 거래: {winning_trades}회 | 손절 거래: {total_trades - winning_trades}회")
        print(f"승률 (Win Rate): {win_rate:.1f}%")
        print(f"평균 수익률 (Avg Return): {avg_return:.2f}%")
        print("※ 지오신소재는 09:33에 조건이 충족되었으나 09:30 진입 제한에 의해 안전 배제되어 체결이 발생하지 않았습니다.")
    else:
        print("체결된 거래가 없습니다.")
    print("="*60)

    # ----------------- 텔레그램 수동 제어 모듈 가상 테스트 -----------------
    print("\n" + "="*60)
    print("        📱 텔레그램 수동 원격 제어 및 자금/지수 필터 조회 시뮬레이션 (v10)")
    print("="*60)
    print("\n📱 [사용자 원격 명령 수신] ➔ /status")
    print("📊 [원격 제어] 실시간 시스템 모니터링 상태 조회 리포트")
    print("  • 에이비씨전자: 당일 매매 완료 [STATE_CLOSED] | 청산결과: 익절 (+5.00%)")
    print("  • 바이오메디: 당일 매매 완료 [STATE_CLOSED] | 청산결과: 고정손절 (-3.00%)")
    print("  • 지오신소재: 신규 매수 감시 종료 [STATE_CLOSED] | 사유: 신규 매수 시간 제한(09:30) 경과")
    print("  • 현재 활성 점유 슬롯 수: 0 / 3 (모든 자금 회수 및 대기)")
    
    print("\n📱 [사용자 원격 명령 수신] ➔ /news 지오신소재")
    print("📰 [원격 뉴스 검색] ➔ '지오신소재' 관련 최신 호재 이슈")
    print(f"  • 📝 AI 감성 판정: [{daily_c['mock_news_sentiment']}]")
    print(f"  • 📝 LLM 핵심 재료 분석 요약: {daily_c['mock_news_title']}")
    print(f"  • 🔗 뉴스 원천 URL (Naver API): {daily_c['mock_news_url']}")
    
    print("\n📱 [사용자 원격 명령 수신] ➔ /re_screen")
    print("🔄 [원격 제어] 실시간 스크리닝 파이프라인 수동 즉시 강제 기동")
    print("  - 기존 감시 종목 해제 및 메모리 버퍼 비우기 완료")
    print("  - [재스크리닝 진행] 금일 거래량 및 실시간 수급 동향을 재분석하여 대상을 새로 편성합니다.")
    print("  - [재편성 완료] 새로운 당일 매수 대기 유니버스: ['에이비씨전자']")

    print("\n📱 [사용자 원격 명령 수신] ➔ /stop_all")
    print("🚨 [원격 제어] 긴급 전체 거래 중지 (EMERGENCY SOS) 명령 수행")
    print("  🛑 전체 거래 스레드 즉시 차단 및 API 비활성화 완료")
    print("  - [최종 처리 결과] 신규 매매 진입 영구 금지. 보유 중인 잔고 전량 즉각 시장가 정형 매도 완료.")
    print("="*60)

if __name__ == "__main__":
    run_simulation_demo()
