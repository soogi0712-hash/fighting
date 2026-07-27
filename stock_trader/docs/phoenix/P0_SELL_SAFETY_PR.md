# P0 매도 실행 안전성 (Project Phoenix) — PR 문서

> 브랜치: `fix/p0-sell-safety` (기준: 원격 배포 브랜치 `feat/phoenix-engine` @ `abd236f`)
> ⚠️ `feat/phoenix-engine` 에는 rebase / force-push / 직접 덮어쓰기를 하지 않는다. 별도 브랜치 + 새 PR.

---

## 1. Executive Summary

> **This PR changes execution reliability only.
> It does not change the trading strategy, signal generation,
> entry/exit logic, or risk model.**
>
> (이번 PR은 전략 변경이 아니라 **실행 안정성 개선**입니다. 스코어·신호 생성·
> 진입/청산 로직·리스크 모델은 일절 변경하지 않았습니다.)

**목적.** 실계좌(KIS_MODE=real, LIVE_ORDER_ENABLED=true)로 Vultr 배포된 자동매매 봇에서 관측된 매도 경로 라이브 버그(중복 매도, 수량 초과 반복, `ORD_UNPR=0` 매도 차단, 포지션 불일치, 재시도 폭주)를 근본 수정하여, **크래시·재시작·API 장애가 발생해도 주문/체결/포지션/실현손익 상태가 일관되게 유지**되도록 한다.

**해결한 핵심 문제.**
- 접수↔체결 창(accept→fill)에서의 **이중 매도**.
- 정규장 지정가 매도에 `ORD_UNPR=0` 이 들어가 **KIS가 매도를 차단**하던 버그.
- 재시도/워치독/고아손절이 `positions.pop()` + 직접 `apply_sell()` 로 **회계를 우회**하던 문제.
- FillObserver/Lifecycle "accept≠fill" 아키텍처가 상태전이 누락으로 **사실상 동작하지 않던** 문제.
- **타임아웃(rt_cd=9)** 을 거절과 구분하지 못해 접수됐을 수 있는 주문을 맹목 재시도.
- 미체결 주문이 **영구 ACTIVE 로 잔존**(stale)하거나, EXPIRE 후 **늦은 체결의 PnL 누락**.

**변경하지 않은 전략 로직 (불변 보장).**
- 매수/매도 스코어, 스크리너, 진입 조건.
- 손절 −5%, 익절 2.0% / 2.5%, 트레일링, 시간청산, 일일 손익 가드.
- 현금-only 사이징 원칙(신용/미수 금지, compound_pool 매수여력 미가산).

**기대 효과.** 이중 매도/유령 회계 제거, 정규장 지정가 매도 정상화, 재시작 시 4개 저장소 자동 수렴, KIS 장애 시 보수적(ACTIVE 유지) 동작, 늦은 체결의 회계 복구. 모든 비정상 경로는 **prominent 로그**로 관측 가능.

---

## 2. Root Cause Analysis

### 2.1 기존 구조에서 문제가 발생한 이유
배포 브랜치 `abd236f` 는 "주문 접수(rt_cd=0)는 체결이 아니다"라는 **execution-driven(FillObserver→lifecycle FILLED 시에만 apply)** 설계를 도입했으나, 다음 결함으로 설계 의도가 무력화되어 있었다.

1. **상태전이 누락**: 정규 BUY/SELL 접수 경로가 `create()` 직후 곧바로 `accept()` 를 호출. 그러나 상태머신은 `UNKNOWN→SIGNAL_CONFIRMED→ORDER_SUBMITTED→ORDER_ACCEPTED` 만 허용 → `accept()` 가 매번 예외 → `except` 폴백으로 **접수 시점에 apply_sell/apply_buy** 실행(= 원래의 accept-time 부킹 버그). 결과적으로 PendingRegistry 가 항상 비어 in-flight 가드가 무력화.
2. **회계 우회 경로**: retry/watchdog/orphan 강제청산이 `api.sell` 성공 시 `positions.pop()` 로 포지션을 즉시 제거 → 체결 확인 없이 회계 확정, 접수↔체결 창 중복 매도.
3. **가격 비대칭**: 매수는 정규장 지정가에 유효 가격을 넣는데 매도만 `ORD_UNPR=0` → KIS "지정가 단가≤0" 규칙에 걸려 차단.
4. **동기화 부재**: 포지션 sync 가 avg 불일치 시에만 교정, 유령 제거 없음, ACTIVE 보호 없음.
5. **실패 분류 부재**: rt_cd≠0 을 모두 동일 취급 → 타임아웃(rt_cd=9, 접수됐을 수 있음)도 맹목 재시도.
6. **단말 writer 부재**: pending_orders 에 FILLED 만 기록, CANCELLED/REJECTED/EXPIRED writer 없음 → stale ACTIVE 영구 잔존.

### 2.2 단계별로 해결한 원인
| 단계 | 근본 원인 | 해결 |
|---|---|---|
| P0-0 | 접수↔체결 창 중복 매도 | PendingRegistry 기반 in-flight SELL 가드 |
| P0-1 | 매도 지정가 `ORD_UNPR=0` 비대칭 | `select_sell_price()` 로 유효가 보장 |
| P0-2 | 회계 우회(`positions.pop`/직접 `apply_sell`) | `submit_retry_liquidation()` 로 lifecycle 경로 강제 |
| P0-2b | 상태전이 누락(accept 예외→폴백) | `confirm_signal→submit→accept` 정상화 |
| P0-3 | 동기화의 유령 미제거·ACTIVE 미보호 | `reconcile_from_broker()`(회계 무개입, ACTIVE 보호, idempotent) |
| P0-4 | 타임아웃/거절 미구분 + stale 잔존 | `classify_order_failure` + `reconcile_stale_pendings` |
| P0-4a | EXPIRE 후 늦은 체결 PnL 누락 + 크로스-스토어 불일치 | `recover_expired_fills` + fill-aware expiry + `reconcile_execution_state`(F1/F2) |

---

## 3. Architecture

### 3.0 핵심 설계 원칙 — Single Source of Truth
```
==================================================================

                    Execution (FillObserver)
                              │
                              ▼
                    Single Source of Truth

          Only confirmed executions may mutate:

                        - Position
                        - Realized PnL
                        - Compound
                        - Re-entry state

==================================================================
```
> **오직 '확인된 체결(confirmed execution)'만이 포지션·실현손익·compound·재진입
> 상태를 변경할 수 있다.** 접수(accept)·재시도·워치독·고아청산·동기화·reconcile 은
> 절대 이 상태들을 직접 바꾸지 않는다. 모든 회계 변경은 FillObserver 가 감지한 체결이
> `_handle_buy_filled` / `_handle_sell_filled` 를 통과할 때 **정확히 1회**만 일어난다.

### 3.1 정상 매도/매수 실행 흐름
```
                    ┌────────────────────────────────────────────────┐
                    │                   KIS REST API                  │
                    │  order(TTTC0802U/0801U) · open orders(TTTC0084R)│
                    │  fills(TTTC0081R) · balance(TTTC8434R)          │
                    └───────────────┬───────────────┬────────────────┘
        (1) 주문 접수 rt_cd=0        │               │  (2) 주기적 체결조회
                    ▼               │               │
        ┌───────────────────────┐   │               │
        │  OrderLifecycleManager │   │               │
        │  create → confirm_     │   │               │
        │  signal → submit →     │   │               │
        │  accept(odno)          │   │               │
        │  [ORDER_ACCEPTED]      │   │               │
        └───────────┬───────────┘   │               │
                    │ _register_pending_order        │
                    ▼               │               │
        ┌───────────────────────┐   │               │
        │   PendingRegistry      │◀──┘               │
        │  pending_orders(WAL)   │  in-flight 가드    │
        │  ACCEPTED/PARTIAL      │  has_active_sell   │
        └───────────┬───────────┘                   │
                    │  get_trackable()               │
                    ▼                                │
        ┌───────────────────────┐                   │
        │     FillObserver       │◀──────────────────┘
        │  poll_once():          │  cum_filled watermark
        │  KIS 체결조회 → 정규화  │  (관측 전용, 포지션 미투영)
        └───────────┬───────────┘
                    │  dispatch_fill(delta, avg, is_full)
                    ▼
        ┌───────────────────────┐
        │ OrderLifecycle.full_   │  ★ FILLED 멱등: on_filled 정확히 1회
        │ fill(on_filled=updater)│
        └───────────┬───────────┘
                    ▼
        ┌───────────────────────────────────────────┐
        │ ExecutionDrivenPositionUpdater             │
        │  side=BUY → _handle_buy_filled             │
        │  side=SELL→ _handle_sell_filled            │  ← 회계 단일 지점
        └───────────┬───────────────────────────────┘
                    │  apply_buy / apply_sell (정확히 1회)
                    │  + DailyPnLGuard.record + ReentryGuard
                    ▼
        ┌───────────────────────┐
        │     PyramidPosition    │  pyramid_positions.json
        │  포지션·평단·레벨·PnL   │
        └───────────────────────┘

원칙: 회계(apply_*/PnL/재진입/compound)는 오직 _handle_*_filled 에서만.
      retry/watchdog/orphan/sync/reconcile 은 절대 회계에 직접 개입하지 않는다.
```

### 3.2 Restart 복구 흐름
```
프로세스 재시작
   │
   ▼
(1) StrategyManager.__init__
     └ load_all_active() → _pending_buy_meta / _pending_sell_meta 복원
   │
   ▼
(2) run_fill_poll()                 다운타임 중 완료된 체결을 먼저 booking
   │                                (watermark 기반, 멱등)
   ▼
(3) reconcile_execution_state()     pending↔lifecycle 크로스-스토어 수렴
     ├ F1: lifecycle FILLED & pending TRACKABLE → pending 을 FILLED 로 동기화(재booking X)
     └ F2: pending FILLED & lifecycle 미FILLED  → 미완료 booking 을 멱등 완료(차액 delta)
   │
   ▼
(4) reconcile_stale_pendings()      5분↑ 미체결 & open orders 성공조회로 미발견
     ├ EXPIRE 전 체결조회 → 실체결이면 booking
     ├ 체결 없음 → EXPIRED(회계 무개입)
     └ open orders 조회 실패 → ACTIVE 유지(자동해제 금지)
   │
   ▼
(5) recover_expired_fills()         EXPIRED(당일+직전영업일) 중 늦은 체결 재확인 → 복구
   │
   ▼
(6) _sync_positions_from_balance()  최종적으로 KIS 잔고 기준 존재/수량/평단 정합화(회계 무개입)
   │
   ▼
4개 저장소(lifecycle·pending·position·PnL) 일관 상태 수렴
```

---

## 4. P0 단계별 변경사항

| 단계 | 변경 파일 | 변경 이유 | 테스트 | 부작용 | 해결한 버그 |
|---|---|---|---|---|---|
| **P0-0** | `journal/fill_observer.py`, `strategies/strategy_manager.py`, `app.py` | 접수↔체결 창 중복 매도 차단 | `test_inflight_guard`(8) | in-flight면 정상 매도도 1주기 지연(안전측) | 미체결 매도 존재 시 run/retry/watchdog 중복 매도 |
| **P0-1** | `strategies/order_pricing.py`(신규), `strategies/strategy_manager.py` | 매도 지정가 `ORD_UNPR=0` → KIS 차단 | `test_order_pricing`(7) | 지정가에 현재가 폴백 사용(가격정보 0이면 0 유지=KIS가 차단, 안전) | 정규장 지정가 매도 전량 실패 |
| **P0-2** | `strategies/strategy_manager.py`, `app.py` | 회계 우회(`positions.pop`/직접 `apply_sell`) 제거 | `test_retry_liquidation`(5) | 강제청산도 체결 확인 후 부킹(즉시성↓, 정합성↑) | 재시도·워치독·고아손절 이중매도/유령회계 |
| **P0-2b** | `strategies/strategy_manager.py` | `create→accept` 상태전이 예외→폴백 부킹 | `test_lifecycle_transition_flow`(5) | booking 시점이 접수→체결로 이동(설계 의도) | FillObserver 아키텍처 무력화 / P0-0 가드 미작동 |
| **P0-3** | `strategies/pyramid_strategy.py`, `strategies/strategy_manager.py`, `app.py` | 유령 미제거·ACTIVE 미보호·회계 개입 | `test_position_reconcile`(9) | qty/avg 교정은 요약값만(레벨구조·full_entry_done 보존) | 유령 포지션·수량 불일치·삼성전자 포지션 불일치 |
| **P0-4** | `strategies/order_failure.py`(신규), `api/kis_api.py`, `journal/fill_observer.py`, `strategies/strategy_manager.py`, `app.py` | 타임아웃/거절 미구분 + stale 영구 잔존 | `test_order_failure`(8), `test_pending_terminal`(8), `test_sell_timeout`(3), `test_stale_reconcile`(6), `test_p04_integration`(3) | 타임아웃 미확인 시 재시도 보류(다음 루프 재평가) | rt_cd=9 맹목 재시도 이중매도 / SELL_RETRY 폭주 / stale ACTIVE |
| **P0-4a** | `phoenix/lifecycle.py`, `journal/fill_observer.py`, `api/kis_api.py`, `strategies/strategy_manager.py`, `app.py` | EXPIRE 후 늦은 체결 PnL 누락 + 크로스-스토어 불일치 | `test_expired_fill_recovery`(6), `test_p04a_hardening`(5) | 체결가 미확보 시 가짜 booking 금지(로그로 노출) | 늦은 FILLED PnL 누락 / F1·F2 크래시 불일치 |

---

## 5. 테스트 결과 (73개, 전부 통과)

실행: `python -m unittest discover -s tests/p0_sell -t .` → **Ran 73, OK**. `py_compile` 전 파일 통과. 실데이터(`data/*.json`, `*.db`) 무변경(임시 DB/파일 격리).

| 카테고리 | 파일(개수) | 무엇을 검증하는가 |
|---|---|---|
| in-flight 가드 | `test_inflight_guard`(8) | ACCEPTED/PARTIAL 매도 존재 시 `has_active_sell`=True, FILLED/CANCELLED=False, BUY 미집계, 코드/시장 격리 |
| 주문 가격 | `test_order_pricing`(7) | 정규장 지정가 매도 유효가 보장(0 방지), 시장가/장후=0, 장전=가격 필수 |
| 재시도 라우팅 | `test_retry_liquidation`(5) | retry 성공→Lifecycle 등록만, 체결→apply_sell 1회, 중복 dispatch 무중복, watchdog+retry 충돌 시 실주문 1건 |
| 상태전이 | `test_lifecycle_transition_flow`(5) | UNKNOWN→…→FILLED 전 구간 순서, 회귀(과거 버그 예외 고정), 부분→전량, 직행 체결 |
| 포지션 정합 | `test_position_reconcile`(9) | 유령 제거, 누락 복원, 수량 교정, ACTIVE SELL 조기삭제 방지, ACTIVE BUY 중복생성 방지, 부분체결 유지, 조회실패 보존, idempotent, level_entries/full_entry_done 보존 |
| 실패 분류 | `test_order_failure`(8) | 5xx/Exception/TIMEOUT→TIMEOUT, BLOCKED/4xx/HTTP200거절→REJECTED |
| 단말 마킹 | `test_pending_terminal`(8) | CANCELLED/REJECTED/EXPIRED 마킹, FILLED 덮어쓰기 금지, idempotent, stale 판정 |
| SELL 타임아웃 | `test_sell_timeout`(3) | 거래소 접수확인→추적등록(재시도X), 미발견/ API실패→UNVERIFIED |
| stale reconcile | `test_stale_reconcile`(6) | 미발견→EXPIRED, API실패→ACTIVE 유지, live 유지, 중복 idempotent, 체결건 EXPIRE 안함, odno없음 보수유지 |
| P0-4 통합 | `test_p04_integration`(3) | restart: 체결됨→FILLED+포지션제거+apply 1회 / 소멸→EXPIRED+포지션보존+apply 0회 / 재실행 동일 |
| 늦은 체결 복구 | `test_expired_fill_recovery`(6) | 사라진 주문 실체결→booking, EXPIRED후 복구 1회, idempotent, 미체결 유지, restart 통합 |
| 크래시 하드닝 | `test_p04a_hardening`(5) | 늦은 BUY 복구, 부분→누적 차액만 booking, F1(재booking 0), F2(정확히 1회), 체결가미확보→가짜금지 |

---

## 6. Residual Risks

### 6.1 현재 남아 있는 리스크 (R1~R4)
- **R1 — apply 중 크래시**: `_handle_sell_filled` 실행 중(lifecycle FILLED 영속화 *이후*, apply_sell 완료 *이전*) 크래시 시, 멱등 가드가 재실행을 막아 realized PnL 금액이 미기록될 수 있다. 포지션 수량은 P0-3 잔고동기화가 정정하나 **PnL 금액은 자동 복구되지 않음**(에러 로그로 노출).
- **R2 — odno 유실**: 접수 응답 유실로 odno 가 없는 주문은 체결조회 대조가 불가 → 자동 복구 대상에서 제외(보수적으로 ACTIVE 유지 후 수동 확인).
- **R3 — 조회 범위 초과**: KIS 체결조회는 당일+직전 영업일만 커버. 그 이전(장기 다운타임) 미기록 체결은 복구 대상 밖.
- **R4 — 부분체결 PnL 시점**: 부분체결 후 잔량 취소 시, 체결분 수량은 booking 되나 아키텍처상 실현손익은 전량 완료 시점에 총량으로 계산됨(부분별 분해 없음).

> 정확한 표현: "영구 누락 0"이 **아니라**, "수량 정합성은 잔고 기준으로 보장되고, PnL 금액 자동복구는 3중 방어(fill-aware expiry / reconcile_execution_state / recover_expired_fills) 범위 내에서 보장되며, 그 경계 밖 잔여 위험(R1~R4)은 모두 prominent 로그로 노출된다."

### 6.2 향후 개선 (P1 이후)
- **P1-A**: `_handle_*_filled` 를 저널 기반 재개 가능(resumable) 구조로 → R1 해소(exactly-once 부킹).
- **P1-B**: odno 유실 주문에 client_order_id 로 대체 대조 → R2 완화.
- **P1-C**: 체결조회 date range 를 설정 가능화 + 장기 다운타임 복구 배치 → R3 완화.
- **P1-D**: 부분체결 실현손익 분해 회계 → R4 해소.
- **P1-E**: `get_balance`/open-orders 의 빈 응답과 API 실패를 명확히 구분(현재 빈 리스트=진짜 0 가정) → 플레이키 응답으로 인한 잘못된 유령 제거 방지.

---

## 7. Deployment Plan

**단계적 승격 흐름 (필수 관찰 기간 포함):**
```
Paper Trading
     ↓
Real Account (minimum quantity)
     ↓
24~48시간 관찰   ← 이상 없으면만 다음 단계
     ↓
Production
```

1. **`fix/p0-sell-safety` push** — 원격 배포 브랜치를 건드리지 않고 새 브랜치만 push(`git push -u origin fix/p0-sell-safety`).
2. **PR 생성** — base=`feat/phoenix-engine`, head=`fix/p0-sell-safety`. 본 문서를 PR 본문으로 사용.
3. **Code Review** — 회계 단일 지점 원칙, ACTIVE 보호, idempotency, 전략 불변성 중점 리뷰.
4. **Paper Trading 검증** — `KIS_MODE=paper`, `LIVE_ORDER_ENABLED=false` 로 기동. 주의: 모의투자는 미체결/체결조회 API 제약이 있어 reconcile 은 보수적(ACTIVE 유지)으로 동작 → 로그로 정상 확인.
5. **실계좌 1주 관찰(주문 비활성)** — `KIS_MODE=real`, `LIVE_ORDER_ENABLED=false` 로 조회·reconcile 만 수행, 로그로 상태 수렴 관찰.
6. **소액 실거래 (Real Account, minimum quantity)** — `LIVE_ORDER_ENABLED=true`, 최소 수량/소액 한도로 실제 매매 검증(이중매도/지정가매도/재시작 복구).
7. **24~48시간 관찰** — 소액 실거래 상태로 최소 24~48시간 운영하며 배포 후 확인 로그(10)·Rollback 트리거(9)를 모니터링. **이상 없음이 확인될 때만** 다음 단계로 승격.
8. **운영 배포 (Production)** — 관찰 통과 후 정규 한도로 배포(docker compose 재기동).
9. **Rollback** — 배포 브랜치가 별도이므로 이전 이미지/커밋으로 즉시 롤백. 상태 저장소(`trading_journal.db`, `pyramid_positions.json`)는 스키마 하위호환이므로 데이터 마이그레이션 불필요. 롤백 시에도 reconcile 로직만 비활성화되고 데이터는 보존.
   - **즉시 Rollback 트리거 (아래 중 하나라도 발생 시 지체 없이 롤백):**
     - `duplicate SELL detected` — 동일 종목 중복 매도 관측
     - `lifecycle divergence` — lifecycle ↔ pending/포지션 상태 불일치 지속
     - `unexpected Pending growth` — 미체결(pending ACTIVE) 비정상 증가
     - `reconcile loop` — reconcile 가 수렴하지 않고 반복(EXPIRE↔복구 진동 등)
     - `unexpected PnL mismatch` — 실현손익이 잔고/체결내역과 불일치
10. **배포 후 확인 로그**:
   - `[SELL ACCEPTED] lifecycle 등록 완료` / `[SELL FILLED] apply_sell 완료`
   - `[submit_retry_liquidation] 접수 성공` / `⏸️ SELL_TIMEOUT`
   - `[reconcile_stale] ... expired=/kept_unverified=/api_failed=`
   - `[recover_expired] 늦은 체결 복구 완료` (발생 시 주의)
   - `[reconcile_state] F1_synced/F2_booked/F2_unbookable`
   - `🧹 유령 포지션 제거` / `🔧 수량·평단 교정`
   - `❌ ... 체결가 미확보`(R1/R4 경계 — 수동 확인 필요)

---

## 8. Commit Plan (리뷰 단위, 7 커밋)

> 각 커밋은 해당 단계의 코드 + 테스트를 함께 포함. `strategy_manager.py`/`app.py` 는 여러 커밋에 걸쳐 해당 단계 hunk 만 포함.

| # | 제목 | 변경 내용 | 포함 파일 |
|---|---|---|---|
| 1 | `fix(sell): P0-0 in-flight 매도 가드로 접수↔체결 중복매도 차단` | `has_active_order/has_active_sell` + 가드 진입 | `journal/fill_observer.py`, `strategies/strategy_manager.py`, `app.py`, `tests/p0_sell/test_inflight_guard.py` |
| 2 | `fix(sell): P0-1 정규장 지정가 매도 ORD_UNPR=0 차단 수정` | `select_sell_price` 도입 + SELL 가격 연결 | `strategies/order_pricing.py`, `strategies/strategy_manager.py`, `tests/p0_sell/test_order_pricing.py` |
| 3 | `refactor(sell): P0-2 재시도/워치독/고아청산을 lifecycle 경로로 라우팅` | `submit_retry_liquidation` + `positions.pop`/직접 `apply_sell` 제거 | `strategies/strategy_manager.py`, `app.py`, `tests/p0_sell/test_retry_liquidation.py` |
| 4 | `fix(lifecycle): P0-2b 정규 접수 경로 상태전이 정상화` | `confirm_signal→submit→accept` 4개 경로 교정 | `strategies/strategy_manager.py`, `tests/p0_sell/test_lifecycle_transition_flow.py` |
| 5 | `feat(position): P0-3 잔고 기준 구조적 포지션 정합화(회계 무개입)` | `reconcile_from_broker`/`active_order_codes` + sync 재작성 + watchdog W4 위임 | `strategies/pyramid_strategy.py`, `strategies/strategy_manager.py`, `app.py`, `tests/p0_sell/test_position_reconcile.py` |
| 6 | `feat(exec): P0-4 타임아웃/거절 구분 + stale pending 자동 reconcile` | `classify_order_failure`, `get_open_orders_checked`, `mark_terminal`/`get_stale_trackable`, `reconcile_stale_pendings`, SELL 타임아웃 검증 | `strategies/order_failure.py`, `api/kis_api.py`, `journal/fill_observer.py`, `strategies/strategy_manager.py`, `app.py`, `tests/p0_sell/test_order_failure.py`, `test_pending_terminal.py`, `test_sell_timeout.py`, `test_stale_reconcile.py`, `test_p04_integration.py` |
| 7 | `feat(exec): P0-4a 늦은 체결 무손실 복구 + 크로스-스토어 정합화` | `recover_fill_from_terminal`, `get_by_status`/`restore_from_terminal`, ccld date range, `recover_expired_fills`, fill-aware expiry, `reconcile_execution_state`(F1/F2) | `phoenix/lifecycle.py`, `journal/fill_observer.py`, `api/kis_api.py`, `strategies/strategy_manager.py`, `app.py`, `tests/p0_sell/test_expired_fill_recovery.py`, `test_p04a_hardening.py` |

각 커밋 메시지 말미:
```
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01GCCRktmsbZ2kzuPGX4ByyM
```
