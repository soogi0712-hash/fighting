# 라이브 국내 매매 — 종목당 비중 상한(10/20/25%) 연결 설계

> 목표: **이미 설계된** `calc_dynamic_weight`의 10/20/25% 종목당 비중 상한을 **라이브 국내 매수 경로**에
> 실제로 연결한다. **전략 점수·매수조건·매도조건·스크리너·손절·익절·트레일링은 변경하지 않는다.**
> 본 문서는 **변경 영향도와 테스트 계획만** 제출한다(코드 수정 없음). 위험기반 사이징은 보류.
>
> 근거 커밋: 브랜치 `feat/phoenix-engine`, 분석 대상은 `strategies/pyramid_strategy.py`,
> `strategies/strategy_manager.py`, `screener/trade_decision.py`, `screener/risk_guard.py`, `config.py`.

---

## 1. 요약 (문제 한 줄)
라이브 국내 매수 사이징의 **종목당 분모가 "전체 계좌"**(`max_per_stock == max_total`)여서, Full Entry가
**단일 종목에 계좌의 100%**까지 투입될 수 있다. 설계된 10/20/25% 상한(`calc_dynamic_weight`)은
이 경로에서 **호출되지 않는다.** 해결책은 종목당 분모를 `total_assets × max_weight_pct/100`로 바꾸는
**배선 복원**이며, 어떤 점수/조건/청산 로직도 건드리지 않는다.

---

## 2. Full Entry가 100%까지 들어가는 정확한 호출 흐름

```
app.py _trading_loop
  └─ strategy_manager.run(stock, cached_cash)                     # strategy_manager.py:108
       ├─ cash = cached_cash | balance.get("cash")                # :277-281  (총자산 아님, 현금만)
       └─ pyramid.evaluate(code, name, cur_price, ind_score, cash,# :284-288
                            buy_score_norm, sell_score)
            │  # ↑ total_assets · AI total_score · rs 를 넘기지 않음
            ├─ [포지션 없음]  _try_entry(...)                      # pyramid_strategy.py:224 → 512
            │     investable = min(max_per_stock, max_total, cash+pool)   # :546-550
            │     ├ Early : invest_amt = investable × 0.30               # :551
            │     └ Full  : invest_full = investable   ← ★100% 지점 #1    # :584
            │              (BUY SCORE ≥ 0.55 이면 investable 전액 투입)   # :582-588
            ├─ [Early 후 Full]  _try_full_entry(...)               # :462 → 625
            │     investable = min(max_per_stock, max_total)              # :637
            │     full_invest_amt = investable × 0.70   ← ★100% 지점 #2   # :638
            │              (Early 30% + FullAdd 70% = 100%)
            └─ [피라미딩 2~4]  _try_add(...)                        # :468 → 668
                  invest_limit = min(max_per_stock, max_total) × ratio    # :679 (15/10/10%)
```

**왜 100%가 되는가 — 두 조건의 결합:**
1. `config.py:27-28` : `MAX_INVESTMENT_PER_STOCK == MAX_TOTAL_INVESTMENT`(둘 다 5,000,000, 주석
   "총자산과 동일 = 한도 없음"). → 종목당 분모 `min(max_per_stock, max_total)` = **전체 계좌**.
2. `_try_entry` Full 분기(`pyramid_strategy.py:584`)와 `_try_full_entry`(`:638`)가 그 분모에
   ratio 1.0(=100%) / 0.30+0.70 을 곱한다. → **단일 종목 = 계좌의 100%** (실효 한계는 `cash+pool`뿐).
3. 이 경로 어디에서도 `risk_guard.check_buy`·`calc_dynamic_weight`가 호출되지 않는다
   (`strategy_manager.py`는 `RiskGuard`를 import조차 하지 않음). 즉 10/20/25% 캡이 **완전 부재.**

> 참고: **재배분 경로**(`strategy_manager.py:975`)는 `evaluate(..., min(cash, alloc))`로 호출되고
> `alloc`이 `trade_decision.prioritize_reallocation`(내부에서 `calc_dynamic_weight` 사용,
> `trade_decision.py:231-232`)로 이미 캡이 걸린다. **비중 상한이 빠진 곳은 신호기반 1차 매수 경로뿐이다.**

---

## 3. 수정 위치 (구현 시 대상 — 지금은 설계만)

| # | 파일:라인 | 현재 | 변경 방향(설계) |
|---|---|---|---|
| A | `strategy_manager.py:284-288` | `evaluate(..., cash, ...)` — 총자산/점수 미전달 | `total_assets`(라이브 총평가액)와 `max_weight_pct`(아래 §4)를 **추가 인자로 전달** |
| B | `pyramid_strategy.py:196` | `evaluate(...)` 시그니처 | `total_assets=None, max_weight_pct=None` **선택 인자 추가**(미전달 시 하위호환: 캡 미적용 또는 보수적 10%) |
| C | `pyramid_strategy.py:546-588` `_try_entry` | `investable = min(max_per_stock, max_total, cash+pool)`; Full=100% | 종목당 분모를 `cap = total_assets × max_weight_pct/100`로 대체 + `invest_amt = min(invest_amt, cap − 이미투자액)` 하드클램프 |
| D | `pyramid_strategy.py:637-639` `_try_full_entry` | `investable = min(max_per_stock, max_total)` | 동일하게 `cap` 기준으로 70% 계산 + 클램프 |
| E | `pyramid_strategy.py:676-679` `_try_add` | `invest_limit = min(max_per_stock, max_total) × ratio` | 동일하게 `cap × ratio` + 누적 캡 클램프 |
| — | `pyramid_strategy.py:572` 포트폴리오 한도 | `total_invested + cost > max_total` | **유지**(계좌 총 100% 한도) |
| — | `config.py:27-28` | `max_per_stock == max_total` | **직접 수정 안 함** — 캡은 코드에서 동적으로 산출(아래 §4) |

핵심: **단 하나의 개념적 변경** = "종목당 분모를 전체계좌 → `total_assets × 동적비중/100`으로 교체".
4개 사이트에 동일하게 적용. ratio(30/70/100/15/10/10)와 청산·조건 로직은 그대로 둔다.

---

## 4. 설계 — 캡 산출(기존 tier 재사용, 점수 불변)

```
max_weight_pct = calc_dynamic_weight(total_score, rs_value)   # 기존 함수, 기존 tier
                 # AI≥90 & RS≥5 → 25 / AI≥80 → 20 / else → 10  (trade_decision.py:61-76)
cap_krw        = total_assets_live × max_weight_pct / 100
```
- **점수 재계산 없음**: `total_score`/`rs_value`는 **이미 저장된 값**을 그대로 읽는다 —
  포지션 추가매수는 `PyramidPosition.entry_score`/`rs_value`(`strategy_manager.py:915-916, 927-928`에서
  이미 사용 중), 신규 진입은 스크리너/`stock` dict가 제공하는 동일 값.
- **미확인 점수 → 보수적 10% 폴백**: `total_score`/`rs`를 구할 수 없으면(예: 잔고동기화로 복원된
  고아 포지션) `WEIGHT_BASE`(10%)로 캡 → **절대 과대 사이징 없음**, 신호 판단은 불변.
- **`total_assets_live`는 현금이 아니라 총평가액**(`balance["total_eval"]`, `app.py:1400`에서 이미 사용)으로
  써야 한다. 그래야 (a) 이미 배포된 자본까지 포함해 비중이 정확하고, (b) 계좌가 복리로 커지면 캡도 함께 커진다.

이 설계는 `calc_dynamic_weight`/`risk_guard`/`trade_decision`의 **임계치·로직을 하나도 바꾸지 않고**,
그 결과값을 라이브 사이징 클램프로 **소비만** 한다.

---

## 5. 변경 영향도

**바뀌는 것 (사이징만):**
| 항목 | Before | After (예: 계좌 500만, 20% tier) |
|---|---|---|
| Full Entry 단일종목 | 계좌의 100% (500만) | ≤ tier% (20% = 100만) |
| Early Entry | 계좌의 30% (150만) | cap의 30% (20%×30% = 6% = 30만) |
| Early+FullAdd | 계좌의 100% | cap의 100% (= 20% = 100만) |
| 피라미딩 L2/L3/L4 | 계좌의 15/10/10% | cap의 15/10/10% (누적 ≤ cap) |
| 자본 전개 | 1~2종목에 집중 | 다수 종목(예 5종목×20%)로 분산, 총전개율 유지 |

**바뀌지 않는 것 (원칙 3 준수 — 명시적 보장):**
- 어떤 신호가 발화하는지(BUY/SELL/HOLD 판정), 매수/매도 **조건**, 스크리너, `buy_score_norm`/`sell_score`,
  손절(-5%), 익절(+2.0/+2.5%), 트레일링(-1%/+1.5% 활성), 쿨다운, 재진입 차단, 일일손실 가드 — **전부 동일.**
- 변경은 **주문 수량(qty)** 한 곳뿐. `action`/`reason`(수량·금액 제외)은 동일해야 한다.

**복리 목표 영향(해치지 않음):**
- 캡×다종목이면 총 자본 전개율은 유지(예 5×20%=100%)되고, 줄어드는 것은 **단일명 변동성뿐**. 변동성
  드래그 감소 → **기하(복리) 수익 개선**. 단일 거래의 명목 이익은 작아지나 위험조정·생존성은 향상.
- 단일명 -5% 손절 손실이 계좌 -5% → 최대 -tier×5%(20%면 -1%)로 축소 → `DAILY_LOSS_LIMIT(-3%)`·
  `MAX_DAILY_LOSSES(2)` 가드가 비로소 유효.

**엣지/결정 필요 사항:**
1. **고가주 최소 1주 보장**(`pyramid_strategy.py:554-557`): 1주 가격이 cap을 초과하는 소액계좌에서
   "1주 허용" vs "캡 준수(스킵)" 중 정책 결정 필요(권장: 캡 우선, 초과 시 스킵 — 집중 방지).
2. **점수 소스 폴백**: `total_score`/`rs` 부재 시 10% 폴백(위 §4). 고아 포지션·복원 포지션에 적용.
3. **`total_assets` 소스**: `total_eval` 조회 실패 시 stale/합성 fallback 사용 금지 → 캡 산출 보류하고
   보수적 처리(가장 작은 캡) 필요.
4. **하위호환**: `evaluate`에 인자 미전달 시(기존 호출부/테스트) 동작 정의 — 권장: `max_weight_pct=None`
   이면 10% 캡 강제(과대 사이징 원천 차단) 또는 명시적으로 캡 비활성(둘 중 택1, 테스트에서 고정).
5. **간이 임시 완화**(비권장): `config`에서 `MAX_INVESTMENT_PER_STOCK < MAX_TOTAL_INVESTMENT`로 설정하면
   **평면(flat) 캡**은 즉시 생기나 10/20/25% tier·복리 스케일링을 잃는다. 정식 배선 전 응급용으로만.

---

## 6. 테스트 계획 (구현 전 작성 대상)

기존 스타일(`tests/test_compound_growth_philosophy.py`)과 신규 `unittest`를 병행. FakeBroker 기반(실계좌 금지).

**(1) 사이징 단위 테스트 — `_try_entry`/`_try_full_entry`/`_try_add`**
- Full Entry 캡: `total_assets=5,000,000`, score→25/20/10% 각각에 대해 단일종목 투입 ≤ cap 검증 (기존: 100%).
- Early Entry = cap×30% (계좌×30% 아님) 검증.
- `_try_full_entry`: Early+FullAdd 누적 ≤ cap 검증.
- `_try_add` L2/L3/L4 누적이 cap을 넘지 않음 검증.
- 폴백: `total_score`/`rs` 미제공 → 10% 캡 적용 검증.
- 복리 스케일링: `total_assets` 증가 시 cap 비례 증가 검증.
- 고가주 최소 1주 엣지: 결정된 정책(스킵/허용)대로 동작 검증.

**(2) 불변성(회귀) 테스트 — "전략 변경 없음" 증명**
- 동일 입력에 대해 `evaluate`의 `action`·`reason`(수량/금액 필드 제외)이 **변경 전후 동일**함을 스냅샷 비교.
- 청산 경로(손절/익절/트레일링/시간청산/쿨다운/재진입)의 판정이 **완전 동일**함을 검증(사이징 무관).
- `calc_dynamic_weight`/`risk_guard`/`trade_decision`의 **기존 단위 테스트 그대로 통과**
  (임계치 미변경 확인).

**(3) 통합(FakeBroker) 테스트**
- 고점수 신호 1건 → 진입+FullAdd+피라미딩 후에도 **단일명 총비중 ≤ tier%** 검증.
- 신호 5건 동시 → 총 전개율 ~100%, **어느 종목도 cap 초과 없음** 검증(분산 확인).
- 가드 정합: cap + -5% 손절에서 2연속 손절 ≤ ~2% → `-3%` 일일한도 내(앞선 위험분석과 일치) 검증.

**(4) 속성 기반**
- 랜덤 score/price/total_assets 다수 시행 → 불변식 **단일명 비중 ≤ 해당 tier 캡** 항상 성립.

**테스트 명령(안):** `cd stock_trader && python -m unittest discover -s tests/phoenix -t .`
(사이징 테스트는 `tests/sizing/` 신설 예정 — 구현 승인 후.)

---

## 7. 요약 결론
- **원인**: `max_per_stock == max_total`(전체계좌) 분모 + Full Entry ratio 1.0 → 단일명 100%.
  설계된 10/20/25% 캡은 라이브 1차 매수 경로에서 **미호출**.
- **해결(설계)**: 4개 사이징 사이트의 분모를 `total_assets × calc_dynamic_weight/100`로 교체 + 하드클램프.
  점수·조건·청산·스크리너 **불변**, 변경은 **qty 한 곳**.
- **다음**: 위 §5 결정 5건(고가주 1주 정책·폴백·total_assets 소스·하위호환·임시완화) 확정 후 구현 착수 요청.
