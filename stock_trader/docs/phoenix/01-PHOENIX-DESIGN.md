# Project Phoenix — Crash-Safe 체결 엔진 설계 (Phase 1)

> 목표: 프로그램이 **어느 시점에 종료되더라도** 재시작 후 주문·체결·포지션·실현손익·DailyPnL·
> Risk 상태를 **정확히** 복구하는 엔진.
> 전제 원칙: 브로커 체결을 **영구적 진실의 원천**으로, 모든 체결에 **durable idempotency key**,
> 중복 체결은 **영구히 한 번만** 반영, 저장과 처리상태의 **crash window 제거**, 재시작 시
> **event store 기반 자동 복원**. 전략 점수/매매 조건은 **불변**(원칙 9), 실계좌 주문·배포 **금지**(원칙 10).
>
> 설계 태도: "상태를 절대 저장하지 않는다"를 맹목 적용하지 않는다. **이벤트 로그가 권위 있는 원천**이되,
> 성능을 위한 **projection/snapshot**을 둔다 — 단 projection은 **언제든 이벤트로 재구축 가능**해야 한다.

---

## 1. Architecture

```
                         ┌───────────────────────────────────────────────┐
                         │              Strategy Layer (불변)             │
                         │  strategy_manager / pyramid / us_strategy      │
                         │  점수·매매조건 그대로. 반환: "주문 의도(Intent)"│
                         └───────────────┬───────────────────────────────┘
                                         │ ExecutionIntent(code, side, qty, price, kind)
                                         ▼
   ┌──────────────────────────────────────────────────────────────────────────────┐
   │                        Phoenix Execution Engine (신규)                          │
   │                                                                                │
   │   ┌────────────┐   ┌─────────────────┐   ┌───────────────┐   ┌──────────────┐ │
   │   │ OrderGate  │──▶│ OrderCoordinator│──▶│ BrokerAdapter │──▶│ Reconciler   │ │
   │   │ (recovery/ │   │ intent→submit→  │   │ (KIS wrapper +│   │ open_orders/ │ │
   │   │  risk 게이트)│   │  ack→fills      │   │  ODNO 파싱)   │   │ balance 대조 │ │
   │   └────────────┘   └────────┬────────┘   └───────────────┘   └──────┬───────┘ │
   │                             │  모든 상태 전이 = 1 트랜잭션                │      │
   │                             ▼                                          ▼      │
   │            ┌──────────────────────────────────────────────────────────────┐  │
   │            │           Event Store  (SQLite, WAL, 단일 파일)               │  │
   │            │  events (append-only)  +  projections (positions/pnl/risk/…)  │  │
   │            │  +  order_index  +  processed_watermark  +  snapshot_meta     │  │
   │            │  ── 이벤트 append 와 projection 갱신이 동일 트랜잭션 ──         │  │
   │            └──────────────────────────────────────────────────────────────┘  │
   └──────────────────────────────────────────────────────────────────────────────┘
                                         │
                                         ▼
                        RecoveryManager (부팅 시): snapshot 로드 → 이벤트 재생
                        → 브로커 reconcile → RecoveryCompleted 이벤트 → 게이트 해제
```

**핵심 아키텍처 결정**
1. **단일 트랜잭션 저장소**: 여러 JSON 파일 금지. SQLite 1개 파일(`data/phoenix.db`, WAL 모드).
   이벤트 append 와 모든 projection 갱신을 **하나의 ACID 트랜잭션**으로 커밋 → 저장과 처리상태
   사이의 crash window가 **원천적으로 소멸**(GAP2 문제 5 해결).
2. **이벤트 소싱 + projection**: `events`는 append-only 권위 원천. `positions/pnl/risk/daily_pnl`은
   **동일 DB 내 projection 테이블**(성능용). 언제든 `events`만으로 재구축 가능.
3. **브로커 체결이 진실**: 내부 상태는 항상 `get_balance`/`get_open_orders`/`get_order_history`로
   reconcile되어 브로커 수량이 최종 권위(단, stale/합성 fallback 판별 필요).
4. **전략과 실행의 분리**: Strategy Layer는 **의도(Intent)만** 반환하도록 얇게 조정. 점수·조건·임계치
   코드는 손대지 않는다(원칙 9). 실제 `api.buy/sell` 호출과 상태 반영은 전부 Phoenix로 이관.
5. **단일 writer**: 엔진은 단일 스레드 이벤트 루프(APScheduler 루프)가 DB에 쓴다. 대시보드 등은
   read-only. 동시 쓰기 lost-update(GAP2) 제거.

---

## 2. Event Schema

append-only 사실 기록. 모든 상태 변화는 이벤트로 표현되고, **그 이벤트가 곧 처리완료 증거**다.

### 2.1 `events` 테이블
```sql
CREATE TABLE events (
  seq            INTEGER PRIMARY KEY AUTOINCREMENT,  -- 전역 단조 순서(=처리 순서)
  event_uuid     TEXT    NOT NULL UNIQUE,            -- 생성측 UUID
  ts             TEXT    NOT NULL,                   -- ISO8601 (KST)
  type           TEXT    NOT NULL,                   -- 아래 이벤트 타입
  aggregate_type TEXT    NOT NULL,                   -- 'order' | 'position' | 'account'
  aggregate_id   TEXT    NOT NULL,                   -- client_order_id | code | 'ACCOUNT'
  client_order_id TEXT,                              -- 주문 이벤트 결속키
  odno           TEXT,                               -- 브로커 주문번호(확보 후)
  code           TEXT,
  side           TEXT,                               -- 'BUY' | 'SELL'
  qty            INTEGER,
  price          REAL,
  cum_filled_qty INTEGER,                            -- 체결 관측의 누적 체결수량
  realized_pnl   REAL,                               -- 청산 delta(계산된 경우)
  idempotency_key TEXT UNIQUE,                       -- 중복 반영 차단(핵심)
  payload        TEXT,                               -- 기타 JSON
  schema_ver     INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX ix_events_order ON events(client_order_id, seq);
CREATE INDEX ix_events_code  ON events(code, seq);
```

### 2.2 이벤트 타입 (사실만 기록, 명령 아님)
| 타입 | 의미 | 유발 |
|---|---|---|
| `OrderIntentRecorded` | 신규/추가/청산 의도. **브로커 제출 전** 기록 | 전략 의도 수신 |
| `OrderSubmitAttempted` | 브로커 제출 호출 시작 | 제출 직전 |
| `OrderAckReceived` | 제출 응답 수신, `ODNO` 결속 | 제출 응답 |
| `OrderSubmitAmbiguous` | timeout/네트워크 예외 → 상태 미상 | 제출 예외 |
| `ExecutionObserved` | 브로커 관측: **누적 체결수량** `cum_filled_qty` | 체결 폴링/reconcile |
| `OrderCanceled` | 취소 확정(잔량) | 취소 응답/reconcile |
| `OrderRejected` | 거절 | 제출 응답 |
| `OrderClosed` | 주문 종결(전량체결/취소/만료) | 전이 확정 |
| `PositionReconciled` | 브로커 잔고 기준 포지션 보정 | 부팅/주기 reconcile |
| `RealizedPnlBooked` | 실현손익 확정(청산 delta) | 매도 체결 반영 |
| `DailySessionReset` | 세션 경계 일일 리셋 | 날짜/세션 전환 |
| `RiskStateChanged` | 손실한도/이익잠금 상태 전이 | 손익 반영 후 평가 |
| `RecoveryStarted` / `RecoveryCompleted` | 복구 게이트 열림/닫힘 | 부팅 |

원칙: 이벤트는 **관측된 사실**(observed) 또는 **의도**(intended)만 담는다. projection은 이벤트에서 **파생**될 뿐,
이벤트 없이 바뀌지 않는다.

---

## 3. Order State Machine

주문 aggregate(`client_order_id` 기준)의 상태. 전이는 **오직 이벤트로만** 발생한다.

```
     OrderIntentRecorded
            │
            ▼
        INTENT ──OrderSubmitAttempted──▶ SUBMITTING
                                            │
              ┌────────────OrderAck─────────┼──────────OrderSubmitAmbiguous──────┐
              ▼                             ▼                                     ▼
          SUBMITTED (ODNO 결속)        (동일 SUBMITTING 유지)               AMBIGUOUS
              │                                                                  │
     ExecutionObserved(cum>0, <ord)                                    (Reconciler가
              ▼                                                         open_orders/balance/
        PARTIALLY_FILLED ──ExecutionObserved(cum==ord)──▶ FILLED         history로 판정)
              │                                             │                    │
              │                                             ▼                    ▼
              ├─────────OrderCanceled(잔량)──────────▶  CANCELED          SUBMITTED/FILLED/
              │                                                            CANCELED/REJECTED
              ▼                                                            중 하나로 수렴
          (부분체결분은 포지션에 반영, 잔량은 취소)
        모든 종결 상태 → OrderClosed → CLOSED (재처리 금지)
```

- **AMBIGUOUS**는 crash-safety의 핵심 상태다. timeout으로 "성공했는지 모름"인 주문을 **재제출하지 않고**
  reconcile 대상으로 격리한다(GAP2 문제: timeout 이중주문 방지).
- 부분체결은 정상 경로. `PARTIALLY_FILLED`에서 잔량 취소 시 이미 체결된 delta는 포지션에 남고 잔량만 취소.
- 취소와 체결 동시(crash G): `ExecutionObserved`와 `OrderCanceled`가 같은 `ODNO`에 도착해도 **cum_filled_qty
  watermark**가 실제 체결분을 확정하고, 취소는 "그 시점 잔량"만 반영 → 이중/누락 없음.

---

## 4. Event Store Transaction Model

모든 상태 전이는 **단일 SQLite 트랜잭션** 안에서:

```python
def apply(engine_action):
    with db.transaction("IMMEDIATE"):          # BEGIN IMMEDIATE (writer lock)
        if exists(idempotency_key):            # 1) 멱등 검사
            return ALREADY_APPLIED             #    중복이면 no-op 커밋 없이 반환
        insert_event(...)                      # 2) 이벤트 append (UNIQUE(idempotency_key))
        update_projection(...)                 # 3) positions/pnl/risk/daily_pnl 갱신
        update_order_index(...)                # 4) 주문 상태·watermark 갱신
        set_processed_watermark(event.seq)     # 5) 처리완료 표식 = 같은 트랜잭션
    # COMMIT: 위 2~5가 원자적으로 확정 (fsync 보장, WAL)
```

**PRAGMA**: `journal_mode=WAL`, `synchronous=FULL`(체결 반영), `busy_timeout=5000`, `foreign_keys=ON`.

**crash window 제거의 원리**: GAP2는 "이벤트 저장(2)"·"projection 적용(3)"·"처리완료 기록(5)"이
서로 다른 파일·시점이라 A~E 창이 생겼다. Phoenix는 2·3·5가 **같은 트랜잭션**이므로 커밋 여부는 이분법이다 —
전부 반영되었거나(커밋됨) 전혀 안 됨(롤백). 중간 상태가 디스크에 존재할 수 없다.

**외부 부수효과 순서(브로커 제출)**: 브로커 호출은 트랜잭션 밖의 비가역 부수효과다. 따라서:
1. `OrderIntentRecorded`(+`OrderSubmitAttempted`)를 **먼저 커밋**한다(제출 전 durable intent).
2. 그 다음 브로커 `api.buy/sell` 호출.
3. 응답을 `OrderAckReceived` 또는 `OrderSubmitAmbiguous`로 **다음 트랜잭션에 커밋**.
→ 제출 직후 crash(케이스 A)여도 intent가 남아 재구동 시 reconcile로 진실을 회수한다.

---

## 5. Durable Idempotency Model

두 방향 모두를 durable 키로 보호한다.

### 5.1 아웃바운드(제출) 멱등 — 이중주문 방지
- 제출 **이전에** `client_order_id = uuid4()`를 생성해 `OrderIntentRecorded`로 커밋.
- 재구동 시, 종결(CLOSED)되지 않은 intent는 **재제출하지 않고** Reconciler가 브로커
  `get_open_orders`(잔량)·`get_balance`(정산)·`get_order_history`로 실제 결과를 판정.
- (KIS는 클라이언트 토큰 미지원이므로) 아웃바운드 멱등은 "제출 전 intent 커밋 + 재구동 시 재제출 금지 +
  reconcile"로 달성. timeout 성공 주문의 유령 방지.

### 5.2 인바운드(체결) 멱등 — 이중반영 방지 (핵심)
브로커 체결은 **per-order 누적 스냅샷**(`tot_ccld_qty`)으로만 관측된다(이벤트 아님, 중복·역순 가능).
따라서 **watermark 기반 delta**로 반영한다:

```
order_index(odno) 에 applied_qty(=이미 포지션 반영한 누적 체결수량) 를 유지.
관측 cum_q 도착 시  (단일 트랜잭션 내):
    delta = max(0, cum_q - applied_qty)      # 뒤로 가는 스냅샷은 delta=0 (역순 안전)
    if delta == 0: return ALREADY_APPLIED     # 중복/역순 → no-op
    idempotency_key = f"exec:{odno}:{cum_q}"  # UNIQUE 로 정확 중복 재차단
    insert ExecutionObserved(cum_filled_qty=cum_q)
    apply position delta (+delta for BUY / -delta for SELL)
    if SELL: book RealizedPnl(delta 기준)      # 청산 회계도 같은 트랜잭션
    applied_qty = cum_q                        # watermark 전진(단조 증가만)
```

- **중복 스냅샷**: 같은 `cum_q` 재도착 → `idempotency_key` 충돌 또는 `delta==0` → 반영 안 됨.
- **역순 스냅샷**: 더 작은 `cum_q` → `delta==0` → 무시(watermark는 감소하지 않음).
- **부분→전량 진행**: `cum_q`가 20→50→100처럼 증가하면 각 delta(20,30,50)만 반영 → 정확.
- **재시도 청산의 회계 누락(GAP2 D)**: 청산도 동일 `ExecutionObserved`/`RealizedPnl` 경로로만 반영되므로
  "positions.pop만 하고 손익 누락" 같은 우회 경로가 원천 차단.

---

## 6. Crash Recovery Matrix (A~I)

각 지점에 대해 **영구 저장된 데이터 / 재시작 시 동작 / 중복 방지 / 유실 방지 / 거래 차단**.
(Phoenix 기준. "거래 차단"은 신규 위험증가 주문 차단 여부 — §8 게이트.)

### A. 주문 접수 응답 직후 crash
| 항목 | 내용 |
|---|---|
| 영구저장 | `OrderIntentRecorded`+`OrderSubmitAttempted` 커밋됨. `OrderAck`는 미커밋일 수 있음 |
| 재시작 동작 | intent가 미종결 → Reconciler가 `open_orders`/`balance`로 실제 체결·잔량 판정, `OrderAck`/`ExecutionObserved` 소급 기록 |
| 중복 방지 | 재제출 안 함(§5.1). 체결 반영은 watermark delta(§5.2) |
| 유실 방지 | intent durable → 실제 결과를 브로커에서 회수 |
| 거래 차단 | 해당 종목에 미종결 주문 존재 → 그 종목 **신규 위험증가 주문 차단**(전역 복구게이트도 적용) |

### B. 체결 조회 직후 event 저장 전 crash
| 항목 | 내용 |
|---|---|
| 영구저장 | 관측했으나 `ExecutionObserved` 미커밋 → **디스크엔 없음** |
| 재시작 동작 | 다음 체결 폴링/reconcile가 동일 누적 `cum_q`를 재관측해 반영 |
| 중복 방지 | watermark: 재관측이 실제 반영. 아직 반영 전이므로 정상 1회 반영 |
| 유실 방지 | 브로커 누적 스냅샷은 사라지지 않음 → 재조회로 회수 |
| 거래 차단 | 주문 미종결 → 종목 차단 유지 |

### C. event 저장 직후 projection 적용 전 crash
| 항목 | 내용 |
|---|---|
| 영구저장 | **불가능 상태** — event append와 projection 갱신이 **동일 트랜잭션**(§4). 커밋 안 됐으면 event도 없음 |
| 재시작 동작 | 트랜잭션 롤백 상태 → B와 동일하게 재관측 반영 |
| 중복 방지 | — (애초에 부분반영 없음) |
| 유실 방지 | 재조회로 회수 |
| 거래 차단 | 유지 |

### D. projection 적용 직후 처리완료 기록 전 crash
| 항목 | 내용 |
|---|---|
| 영구저장 | **불가능** — projection 갱신과 `processed_watermark` 갱신이 동일 트랜잭션 |
| 재시작 동작 | 둘 다 커밋 or 둘 다 롤백. 이분법 |
| 중복 방지 | watermark==event.seq 원자 확정 |
| 유실 방지 | 롤백 시 재관측 반영 |
| 거래 차단 | 유지 |

### E. 처리완료 직후 응답 전 crash
| 항목 | 내용 |
|---|---|
| 영구저장 | 이벤트+projection+watermark 커밋 완료. 다만 전략/알림 응답은 미전달 |
| 재시작 동작 | 재구동 시 이미 처리됨(watermark 확인) → **재처리 금지**. 미전송 알림은 outbox로 재전송(멱등) |
| 중복 방지 | `idempotency_key`/watermark로 동일 관측 재반영 차단 |
| 유실 방지 | 상태는 이미 durable |
| 거래 차단 | 종목 종결 시 해제 |

### F. 부분체결 도중 crash
| 항목 | 내용 |
|---|---|
| 영구저장 | 마지막으로 커밋된 `cum_q`까지 포지션 반영, `applied_qty` watermark 저장 |
| 재시작 동작 | reconcile가 최신 누적 `cum_q` 재조회 → 남은 delta만 반영, 잔량은 `open_orders`로 추적 |
| 중복 방지 | delta=max(0,cum-applied) → 이미 반영분 재반영 없음 |
| 유실 방지 | 브로커 누적값이 진실 → 놓친 체결분 회수 |
| 거래 차단 | 주문 미종결 → 종목 차단 유지 |

### G. 주문 취소와 체결이 동시 발생
| 항목 | 내용 |
|---|---|
| 영구저장 | `ExecutionObserved(cum_q)`와 `OrderCanceled(잔량)`은 각각 별도 트랜잭션 커밋 |
| 재시작 동작 | reconcile가 `balance`(정산수량)와 `open_orders`(잔량0/부재)로 최종 상태 확정 |
| 중복 방지 | 체결분은 watermark로 1회 반영. 취소는 "관측 시점 잔량"만 반영(멱등 키 포함) |
| 유실 방지 | 실제 체결분(=cum_q)은 취소와 무관하게 포지션에 확정 |
| 거래 차단 | 종목 종결까지 차단 |

### H. event store 손상 또는 lock
| 항목 | 내용 |
|---|---|
| 영구저장 | WAL + `synchronous=FULL`로 마지막 커밋까지 보존. 부분 트랜잭션은 SQLite가 자동 롤백 |
| 재시작 동작 | 부팅 시 `PRAGMA integrity_check`. 정상: snapshot+replay. 손상: WAL 복구 시도 → 실패 시 **Safe-Halt** 모드 |
| 중복 방지 | 무결성 확인 전엔 어떤 주문도 안 냄 |
| 유실 방지 | 커밋된 이벤트는 손실 없음. lock 경합은 `busy_timeout` 재시도 |
| 거래 차단 | **전면 차단**(신규·청산 모두 보류). 운영자 알림. GAP2의 "손상 시 조용히 {} 리셋" 금지 |

### I. broker 조회 결과가 중복 또는 역순 도착
| 항목 | 내용 |
|---|---|
| 영구저장 | 반영된 관측만 `applied_qty`까지 저장 |
| 재시작 동작 | 순서 무관 — 항상 `max` watermark로 수렴 |
| 중복 방지 | `delta=max(0,cum-applied)`, `idempotency_key=exec:{odno}:{cum}` UNIQUE |
| 유실 방지 | 더 큰 `cum_q`가 언젠가 오면 그 delta 반영(누적 단조성) |
| 거래 차단 | 해당 주문 종결 시 해제 |

---

## 7. Replay & Reconciliation Model

### 7.1 Replay (내부 재구축)
- projection은 **언제든 폐기 후 이벤트 전량 재생으로 재구축 가능**(권위=events).
- 성능: `snapshot_meta(last_seq, blob)`에 주기적 projection 스냅샷 저장. 부팅 시
  `snapshot` 로드 → `seq > last_seq` 이벤트만 재생. 스냅샷은 **최적화일 뿐** 진실이 아니다.
- 정합성 자가검증: 부팅 시 projection을 이벤트로 재계산해 스냅샷과 대조(불일치 시 재생으로 교체 + 경고).

### 7.2 Reconciliation (브로커 대조 — 브로커가 최종 진실)
부팅 시 및 주기적으로:
1. `get_balance`로 종목별 정산 수량/평단 취득(단, stale/합성 fallback 판별 — 실패면 reconcile 보류).
2. 미종결 주문에 대해 `get_open_orders`(잔량)·`get_order_history`(누적 체결) 조회.
3. projection 수량 ≠ 브로커 수량이면 `PositionReconciled` 이벤트로 **보정**(브로커=권위). 단
   평단/level 구조는 이벤트 이력이 있으면 유지, 없으면 브로커 평단으로 seed.
4. AMBIGUOUS 주문을 실제 상태(SUBMITTED/FILLED/CANCELED/REJECTED)로 수렴.

reconcile 결과도 **이벤트로 기록**되므로 재생 가능성이 유지된다.

---

## 8. Recovery Order-Gate Policy

**신규 위험증가 vs 위험감소 분류**(원칙 6):
| 분류 | 주문 종류 | 복구 중 정책 |
|---|---|---|
| 위험 **증가** | 신규 BUY, 추가매수(pyramid add) | **차단**(원칙 7) |
| 위험 **감소** | 청산 SELL, 손절, 긴급청산 | **조건부 허용**(§8.2) |

### 8.1 게이트 위치 — 브로커 제출 직전 (GAP2 문제 4 해결)
게이트는 **`OrderCoordinator.submit()` 내부, `api.buy/sell` 호출 직전**에 강제된다. 부팅 시 1회가 아니라
**모든 제출 경로**가 반드시 게이트를 통과. 게이트 통과 조건:
- 전역 상태가 `RecoveryCompleted` 이후일 것(복구 완료).
- 대상 종목에 **미종결/AMBIGUOUS 주문이 없을 것**(in-flight 충돌 방지).
- DailyPnL·Risk projection이 **이벤트로 복원 완료**되어 손실한도 가드가 무장 상태일 것
  (GAP2의 "0 리셋 → 가드 해제" 방지).

### 8.2 보유 포지션의 안전한 SELL·긴급청산 정책 (원칙 8)
복구 중에도 위험감소는 허용하되 **오탐 매도 금지**를 위해:
- **잔고 확증 후에만 매도**: `PositionReconciled`로 브로커 잔고에 실제 존재가 확인된 수량에 한해서만
  청산 주문 허용(내부 유령 포지션 매도 금지).
- **긴급청산(손절/장마감)**: 복구 완료 전이라도, 잔고 확증 + 미종결 청산 주문 없음이면 허용. 단
  제출 전 `OrderIntentRecorded`(kind=`LIQUIDATION`) 커밋 → 재구동 시 재제출 금지·reconcile로 중복 방지.
- **부분 청산 안전**: 청산도 watermark delta로 반영되므로 재시도/중복 관측에도 이중 매도 없음.
- **Safe-Halt(케이스 H)**: event store 손상 시엔 청산조차 보류(무결성 미확인 상태에서 주문 금지) —
  운영자 개입 우선.

---

## 9. Position & PnL Projection Model

모두 이벤트에서 파생되는 projection 테이블(동일 트랜잭션 갱신).

```sql
CREATE TABLE positions (            -- code 단위 현재 보유
  code TEXT PRIMARY KEY, qty INTEGER, avg_price REAL,
  cost_basis REAL, levels TEXT,     -- pyramid level 구조(JSON), 전략 불변 유지
  updated_seq INTEGER               -- 마지막 반영 이벤트 seq
);
CREATE TABLE order_index (          -- 주문 단위 상태·멱등 watermark
  client_order_id TEXT PRIMARY KEY, odno TEXT, code TEXT, side TEXT,
  ord_qty INTEGER, applied_qty INTEGER DEFAULT 0,  -- 이미 반영한 누적 체결
  state TEXT, updated_seq INTEGER
);
CREATE TABLE daily_pnl (            -- 세션 단위 실현손익/거래수
  session_key TEXT PRIMARY KEY, realized_pnl REAL, peak_pnl REAL,
  trades INTEGER, risk_state TEXT, updated_seq INTEGER
);
CREATE TABLE processed_watermark (id INTEGER PRIMARY KEY CHECK(id=1), last_seq INTEGER);
CREATE TABLE snapshot_meta (id INTEGER PRIMARY KEY CHECK(id=1), last_seq INTEGER, blob TEXT);
```

- **positions**: `ExecutionObserved` delta로만 변경(BUY +, SELL −). avg_price/cost_basis는 매수 delta의
  가중평균, 부분매도는 원가기준 유지(전략의 기존 회계 규칙 보존, 원칙 9).
- **realized/daily PnL**: 매도 delta마다 `RealizedPnlBooked` 이벤트로 확정 → `daily_pnl`에 누적.
  GAP2의 "무조건 `+=`, 영속화 없음"과 달리 **각 청산이 이벤트로 기록되어 재구동 시 정확 복원**.
  세션 경계는 `DailySessionReset` 이벤트로 명시(재시작과 새 세션을 구분 — GAP2는 구분 불가였음).
- **Risk state**: `daily_pnl.risk_state`는 실현손익 이벤트 후 결정론적으로 재평가되어 `RiskStateChanged`로 기록.
  손실한도/이익잠금 상태가 이벤트로 복원되므로 crash 후에도 가드가 유지된다.
- **재구축성**: 위 모든 테이블은 `events` 재생으로 100% 재생성 가능. 스냅샷은 부팅 가속용.

---

## 10. Testing & Chaos-Test Plan

GAP2의 "스크립트형 스코어링 테스트뿐" 문제를 정면 해결한다. 전략 점수 로직은 불변이되 **실행/복구
경로**를 실제로 검증한다. (실계좌 금지 — 전부 FakeBroker 기반.)

### 10.1 결정론적 crash-injection 하네스
- `FakeKisBroker`: `api.buy/sell/get_open_orders/get_balance/get_order_history`를 구현하고
  **누적 체결 스냅샷**(부분·전량·중복·역순)과 timeout/ambiguous, 취소-체결 경합을 시나리오로 재생.
- `CrashPoint(label)`: 엔진 코드의 A~I 지점에 주입 가능한 중단점. 테스트가 특정 지점에서 프로세스
  종료를 모사하고, 새 엔진 인스턴스가 **같은 DB**로 재부팅하도록 한다.
- **불변식(모든 시나리오 사후 검증)**:
  1. 재구동+reconcile 후 `positions` == FakeBroker 잔고(수량).
  2. `Σ RealizedPnlBooked` == 실제 청산 손익(이중집계·누락 0).
  3. 동일 `ExecutionObserved`를 N회 재생 → projection 불변(멱등).
  4. AMBIGUOUS 주문이 재제출로 이어지지 않음(중복 주문 0).
  5. 복구 완료 전 신규 위험증가 주문 0건.

### 10.2 시나리오 매트릭스
- 크래시 매트릭스 A~I를 **각각 파라미터라이즈드 테스트**로. 각 케이스에 대해 위 5개 불변식 검증.
- 부분체결 진행(20→50→100 중 각 지점 crash), 취소-체결 경합(G), 중복·역순 관측(I),
  event store 손상(H: WAL 절단/`integrity_check` 실패 → Safe-Halt 진입 확인).

### 10.3 실제 전략 경로 통합 테스트
- `strategy_manager.run`/`pyramid`/`us_strategy`가 반환한 **실제 의도**를 Phoenix로 흘려
  FakeBroker로 체결 → 포지션/손익/게이트가 정확한지 확인. mock 콜백이 아니라 **전략→엔진→브로커→복구**
  전 구간 E2E.
- 회귀 가드: 전략 **점수/임계치 스냅샷 테스트**로 원칙 9(조건 불변) 위반을 CI에서 차단.

### 10.4 속성/장시간
- 랜덤화 chaos: 랜덤 지점 kill을 수천 회 반복(seed 고정)해도 불변식 유지(property-based).
- 동시성: 단일 writer 보장 검증(다중 writer 시 `BEGIN IMMEDIATE` 경합 처리).

---

## 11. GAP2 → Phoenix 마이그레이션 계획

원칙 10(실계좌·배포 금지) 하에서 **점진적·역행 가능** 이관.

**Phase 0 — Shadow(관측 전용)**
- `data/phoenix.db` 스키마 생성. Phoenix 엔진을 **주문 없이** 기동해, 기존 경로의 체결을 관측만 하여
  이벤트로 적재. 부트스트랩: 기존 `pyramid_positions.json`+`trade_log.json`+브로커 `get_balance`로
  1회성 `PositionReconciled`/`RealizedPnlBooked` seed 이벤트 생성.
- 산출물: Phoenix projection vs 기존 JSON 상태 **일치 리포트**(차이 분석).

**Phase 1 — 제출 경로 이관(전략 불변)**
- `strategy_manager`/`us_strategy`의 `api.buy/sell` 직접호출을 **의도 반환**으로 얇게 교체
  (점수·조건 코드 미변경). 실제 제출은 `OrderCoordinator`가 담당(intent 선기록 + 게이트 + 멱등).
- 기존 in-memory `_sell_retry_q`/워치독 강제매도를 **AMBIGUOUS+Reconciler** 경로로 대체.

**Phase 2 — 복구·게이트 컷오버**
- 부팅 복구를 `_sync_positions_from_balance` 단독에서 **RecoveryManager(snapshot→replay→reconcile)**로 대체.
- 복구 게이트를 제출 경로에 결속(§8). DailyPnL/Risk를 이벤트 기반 projection으로 전환(수동 `/api/pnl/inject` 제거).

**Phase 3 — 레거시 은퇴**
- 산재 JSON 쓰기 중단(읽기는 백필 검증용으로만 잠시 유지 후 제거). 대시보드는 Phoenix DB read-only로 전환.
- 롤백 전략: 각 Phase는 feature flag로 즉시 GAP2 경로 복귀 가능. Phase 2까지는 Phoenix가 산출한
  주문을 실제 제출 전 **드라이런 로그로만** 남겨 검증(실계좌 주문 금지 유지).

**불변 유지 체크리스트**: 전략 점수/임계/매매조건 파일(`trade_decision`, `ai_scorer`, `pyramid`의 스코어링,
`indicator_validator`, `strategy_manager`의 신호 판정) **미변경**. Phoenix는 "의도 이후"만 담당한다.

---

## 부록 — 요구 문제 ↔ 설계 대응표
| GAP2 문제 | Phoenix 해결 |
|---|---|
| callback 성공 후 retry 삭제 전 crash 이중반영 | §5.2 watermark delta + 단일 트랜잭션(§4): 재시도 큐를 durable 이벤트/AMBIGUOUS로 대체 |
| applied_qty 저장 후 retry 저장 전 crash delta 유실 | §4 event+projection+watermark 원자 커밋 → 부분반영 불가 |
| durable callback idempotency 부재 | §5 `idempotency_key` UNIQUE + `applied_qty` watermark |
| recovery block이 제출 전에 미연결 | §8.1 게이트를 `submit()` 내부·제출 직전에 강제 |
| pending/retry/position/pnl 원자성 없음 | §1 단일 SQLite, §4 단일 트랜잭션 |
| mock 중심 테스트로 실제 경로 미검증 | §10 crash-injection + 전략 E2E + 불변식 |
