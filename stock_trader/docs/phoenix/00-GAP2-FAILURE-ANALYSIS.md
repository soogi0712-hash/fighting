# Project Phoenix — GAP2 실패 원인 분석 (Phase 1)

> 목적: 차세대 crash-safe 체결 엔진을 설계하기 전에, 기존 GAP2(현행 `stock_trader`)
> 구조가 **왜 crash 시 상태를 정확히 복구하지 못하는지**를 코드 근거와 함께 확정한다.
> 이 문서는 설계의 "요구사항 근거"이며, 여기서 도출된 결함이 Phoenix 설계의 각 절과 1:1로 대응된다.

분석 대상 커밋: `34bcea4` / 브랜치 `claude/phoenix-engine-design-qusdfl`
분석 방식: 3개 독립 서브에이전트 교차 검증 + 직접 코드 확인. 모든 주장에 파일:라인 근거.

---

## 0. 현행 상태 저장 지형도 (persistence topology)

체결 1건이 논리적으로 완결되려면 아래 **5개 JSON 파일 + 2개 순수 메모리 구조**가 갱신되어야
하지만, 이들 사이에는 **어떤 트랜잭션·원자성·저널도 없다.**

| 저장 대상 | 파일/위치 | 쓰기 방식 | 원자성 | crash 복구 |
|---|---|---|---|---|
| 봇 실행여부 | `data/bot_state.json` | `open(w)+json.dump` (`app.py:85`) | ❌ 직접 덮어쓰기 | 손상 시 "정지"로 강등 (`app.py:97`) |
| 포지션 | `data/pyramid_positions.json` | `open(w)+json.dump` (`pyramid_strategy.py:141`) | ❌ | 손상 시 `{}`로 **전량 소실** (`pyramid_strategy.py:131`) |
| 복리풀 | `data/compound_pool.json` | 위 함수의 2번째 dump (`:143`) | ❌ 파일간 원자성 없음 | 손상 시 `0.0` (`:136`) |
| 거래로그 | `data/trade_log.json` | read-modify-write (`strategy_manager.py`) | ❌ | 손상 시 `[]` |
| 재진입차단 | `data/reentry_guard.json` | read-modify-write (`reentry_guard.py:84`) | ❌ | 손상 시 `{}` → 즉시 재매수 허용 |
| 매도 재시도 큐 | `_sell_retry_q` (list) | **메모리 전용** (`app.py:1696`) | — | **crash 시 전량 소실** |
| DailyPnL/Risk | `DailyPnLGuard` 인스턴스 | **메모리 전용** (`daily_pnl_guard.py:92`) | — | **crash 시 0으로 리셋** |

핵심: **"진실의 원천"이 파편화**되어 있고, 그 어느 것도 append-only 이벤트 로그가 아니며,
가장 이벤트 로그에 가까운 `trade_log.json`은 **체결 이후 4번째로** 기록되고(사후 기록) 비원자적이라
write-ahead log 역할을 할 수 없다.

---

## 1. 매수(BUY) 경로 — 제출 전 durable 기록 없음

- 주문 제출: `strategy_manager.py:542` `result = self.api.buy(...)`.
  **이 호출 이전에 디스크에 남는 "주문 의도(intent)" 기록이 전혀 없다.** 소켓으로 요청이 나간 뒤
  `result` 평가 전에 crash 나면, 주문이 나갔다는 durable 증거가 0.
- 상태 반영은 `rt_cd == "0"` 일 때만 (`:543, :636`) `apply_buy()` → 메모리 변경 후
  `_save()` (`pyramid_strategy.py:735`).
- 기록되는 수량/가격은 **제출값**이지 확정 체결값이 아님 (`:637`, 부분체결 무시 — §5 참조).

## 2. 매도(SELL) 경로 + 재시도 큐 — 이중반영·delta 유실의 진원지

정상 매도 순서 (`strategy_manager.py:728-787`):
1. `api.sell(...)` → **브로커 체결(외부·비가역)**
2. `pyramid.apply_sell(...)` → 포지션 삭제(`pyramid_strategy.py:761`) + 복리풀 가산(`:767`) + `_save()`(`:779`)
3. `pnl_guard.record(net_profit)` → **메모리만** 변경 (`daily_pnl_guard.py:138`, 영속화 없음)
4. `_log_trade(...)` → `trade_log.json`
5. `reentry.record_sell(...)` → `reentry_guard.json`

**요구된 검토 문제와의 대응:**

- **(a) callback 성공 후 retry 삭제 전 crash 시 이중반영**
  `_flush_sell_retry_q` (`app.py:1741-1765`) 순서는 `api.sell` → `positions.pop` →
  `_save()` → (성공 시 큐에서 미추가) → `_sell_retry_q = remain`.
  큐가 **메모리 전용**이라 crash 시 큐 자체가 사라져 "이중반영" 대신 "청산 유실"로 나타난다.
  그러나 **단일 프로세스 내**에서는 `api.sell`이 브로커에서 실제 성공했으나 응답이 유실/예외로
  잡히면 `attempt += 1` 후 재제출(`app.py:1759-1763`) → **동일 물량 이중 매도**. 성공 판정이
  `rt_cd`만 보기 때문. 또한 `pnl_guard.record`는 무조건 `+=` (dedup 없음)이라 재호출 시 이중 집계.

- **(b) applied_qty 저장 후 retry 저장 전 crash 시 delta 유실**
  정상 매도에서 `apply_sell`이 **포지션을 먼저 삭제**(`:761`)하고 `_save`까지 끝낸 뒤 `pnl_guard.record`가
  실행된다. 2와 3 사이에 crash 나면 포지션은 이미 사라져 재구동 시 **손익 delta를 재도출할 수 없다**
  (원본 소실). 게다가 `pnl_guard`는 애초에 영속화되지 않아 어느 지점에서 죽든 그날 delta 전체가 사라진다.

- **재시도/워치독 경로의 회계 누락 (crash 무관 상시 버그)**
  `_flush_sell_retry_q`와 워치독 강제매도 성공 경로는 `apply_sell`이 아니라 `positions.pop()`만 호출
  (`app.py:1747-1748`, `1932-1933`). → 실현손익 미계산, 복리풀 미가산, `pnl_guard.record` 미호출.
  **즉 재시도 큐를 거친 청산은 손익 회계에서 영구 누락된다.**

## 3. Durable callback idempotency 부재

- 주문 제출에 **클라이언트 생성 주문 ID/중복 토큰이 없다**. `api.buy/sell`은 `code,qty,price,ord_dvsn`만 받음
  (`kis_api.py:806-812`).
- 브로커가 주는 유일한 durable 키는 KIS **`ODNO`**(+`KRX_FWDG_ORD_ORGNO`)인데, 성공 응답의
  `output`에 있으나 **wrapper가 파싱조차 하지 않는다**(`kis_api.py:744` raw dict 그대로 반환).
- 체결 조회 `get_order_history`(`kis_api.py:1922`)는 **주문 단위 누적 스냅샷**(`tot_ccld_qty`)이고
  출력에서 `ODNO`를 **버린다**(`:1953-1962`) → "제출한 주문"과 "체결분"을 조인할 수 없다.
- 결과: 체결 이벤트를 한 번만 반영하도록 보장하는 **어떤 idempotency key도 없다.**

## 4. Recovery block이 broker 주문 제출 전에 연결되지 않음

- 복구는 `_sync_positions_from_balance`(`app.py:260`)뿐이고 **초기화 시 1회만** 실행. 매수 경로
  (`api.buy` 직전)에 "복구 중이면 신규 위험증가 주문 차단"하는 게이트가 **없다**.
- DailyPnL 가드가 crash 후 0으로 리셋되므로(§5·F) 손실한도 가드가 **무장 해제**된 채 신규 매수가 진행된다.
- in-flight(제출됨·미반영) 주문은 복구 대상이 아니며, 재구동 후 체결돼도 **다음 init까지 미추적**.

## 5. pending/retry/position/pnl 상태 분리 → 원자성 없음

- 위 §0 표대로 5파일 + 2메모리 구조가 **순차·비원자적**으로 갱신된다. 체결 1건에 대한 crash window가
  최소 8개(아래 표) 존재하며 각기 다른 불일치를 남긴다.
- 어떤 파일도 `tmp+os.replace`/`fsync`/lock을 쓰지 않아 **crash 없이도** KR·US 매니저 동시 접근 시
  lost-update 가능.

| 지점 | crash 위치 | 결과 |
|---|---|---|
| A | BUY 성공 → `apply_buy._save()` 전 | 브로커엔 보유, JSON엔 없음 → 재구동 시 가짜 level-1로 복원 |
| B | `_save` 두 dump 사이 (`pyramid_strategy.py:141-145`) | positions/compound_pool 불일치 |
| C | `_sell_retry_q`에 항목 있는 상태로 crash | 대기 청산 전량 소실(메모리 전용) |
| D | 재시도/워치독 매도 성공(`app.py:1742-1748`,`1930-1933`) | 손익 미기록·복리풀 미가산 (crash 없이도) |
| E | 정상 SELL: `apply_sell._save()` 후 `pnl_guard.record` 전 | 포지션 소멸+풀가산됐으나 일일손익 delta 영구 소실 |
| F | 임의 재시작 | `DailyPnLGuard`·`_today_pnl_*` 0 리셋 → 손실한도 가드 해제 (수동 `/api/pnl/inject` 전까지) |
| G | `rt_cd==0` 제출의 부분체결 | 전량체결로 오기록, 다음 init balance sync 때만 교정 |
| H | `bot_state.json`/`pyramid_positions.json` 손상 | `_load` 예외 삼킴 → "정지" 처리 / 포지션 `{}` 소거 |

## 6. Mock callback 중심 테스트로 실제 전략 경로 미검증

- 테스트 파일은 `tests/test_compound_growth_philosophy.py` **단 1개**이며, pytest `def test_`도 아닌
  스크립트형 `check()/section()` 검증기다.
- 검증 대상은 `transaction_cost`(수수료 계산), `trade_decision`(점수), `risk_guard`(한도),
  `indicator_validator`(지표) — **순수 전략/스코어링 로직뿐**.
- `api.buy/sell` mock, 체결 콜백, crash/restart, 복구, idempotency 테스트가 **전무**. 즉 실제 체결·
  영속화·복구 경로(`strategy_manager.run → 브로커 → 저장 → 재구동`)는 **어떤 자동 테스트로도 검증되지 않는다.**

---

## 7. 브로커(KIS) 계층이 제공하는 durable 사실 — Phoenix 설계 입력

| 관심사 | 브로커 durable 식별자 | 현행 wrapper 노출 |
|---|---|---|
| 제출 주문 | `ODNO` (+`KRX_FWDG_ORD_ORGNO`), 성공 `output` 내 | 반환되나 **미파싱**(`kis_api.py:744`) |
| 미체결 추적 | `order_no`(`odno`) + `unexec_qty`(`rmn_qty`) | 정규화 노출(`:857,:861`) — **활용 가능** |
| 취소 대상 | `ORGN_ODNO = order_no` | 입력만, 취소된 수량 결과 미파싱 |
| 체결 | 주문단위 `tot_ccld_qty`/`avg_prvs`, **per-fill id/seq/time 없음, ODNO 버림** | 약함 — 조인 불가 스냅샷 |
| 포지션 | 종목별 `hldg_qty`/`pchs_avg_pric` | 노출, 단 3회 실패 시 stale/합성 fallback |

**설계상 확정 사실:**
1. 브로커 제출은 **비멱등** — 클라이언트 토큰 없음, timeout 재전송 시 중복 주문 생성 위험.
2. 유일 durable 키는 KIS가 부여하는 **`ODNO`**. → Phoenix는 제출 **이전에** 자체 `client_order_id`를
   durable 기록하고, 제출 **이후** `ODNO`를 그 intent에 결속해야 한다.
3. 체결은 **per-order 누적 스냅샷**으로만 관측 가능(이벤트 아님) → Phoenix는 체결을
   "누적 체결수량 관측(observation)"으로 모델링하고 **watermark(applied_qty) 기반 delta**로 반영해야
   중복·역순 도착(crash I)에도 안전하다.
4. 복구의 durable 원천은 `get_open_orders`(주문별 잔량)와 `get_balance`(정산 수량/평단)이며,
   timeout 모호 주문은 엔진이 스스로 이 둘을 조회해 reconcile 해야 한다.
