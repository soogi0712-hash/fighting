# Project Phoenix — Phase 1.5: KIS API 실측 분석 & 운영 설계

> Phase 1 설계(이벤트 스토어·watermark 멱등·복구 게이트)를 **실제 KIS API 응답 필드**와
> **운영(백업/복구/TPS/장마감)** 관점에서 구체화한다.
> 근거: 이 저장소 `api/kis_api.py`가 실제 호출하는 엔드포인트·TR_ID·파싱 필드 + KIS OpenAPI 규격.
> 표기 규칙: **[코드확인]** = 이 저장소가 실제로 사용/파싱하는 필드, **[KIS규격]** = KIS 문서상 존재하나
> 현행 코드가 파싱하지 않는 필드(예시 값은 형식 설명용).

---

## 0. API 지형도 (실측)

### 국내 (domestic-stock) — [코드확인]
| 기능 | 엔드포인트 | TR_ID | 코드 위치 |
|---|---|---|---|
| 매수/매도 | `/v1/trading/order-cash` | `TTTC0802U`/`TTTC0801U` | `kis_api.py:550` |
| 정정/취소 | `/v1/trading/order-rvsecncl` | `TTTC0803U` | `:886` |
| 미체결(정정취소가능) | `/v1/trading/inquire-psbl-rvsecncl` | `TTTC8036R` | `:825` |
| 체결/주문내역 | `/v1/trading/inquire-daily-ccld` | `TTTC8001R` | `:1925` |
| 잔고 | `/v1/trading/inquire-balance` | `TTTC8434R` | `:977` |
| 주문가능금액 | `/v1/trading/inquire-psbl-order` | `TTTC8908R` | `:946` |

### 미국/해외 (overseas-stock) — [코드확인]
| 기능 | 엔드포인트 | TR_ID | 코드 위치 |
|---|---|---|---|
| 매수 | `/v1/trading/order` | `TTTT1002U` | `:1478` |
| 매도 | `/v1/trading/order` | `TTTT1006U` | `:1706` |
| 주문가능금액 | `/v1/trading/inquire-psamount` | `TTTS3007R` | `:1343` |
| 잔고 | `/v1/trading/inquire-balance` | `TTTS3012R` | `:1749` |
| **미체결 조회** | `/v1/trading/inquire-nccs` | `TTTS3018R` | **없음 — Phoenix 신규 필요** |
| **체결내역 조회** | `/v1/trading/inquire-ccnl` | `TTTS3035R` | **없음 — Phoenix 신규 필요** |
| **정정/취소** | `/v1/trading/order-rvsecncl` | `TTTT1004U` | **없음 — Phoenix 신규 필요** |

> ⚠️ **US 재구성 공백**: 현행 코드에는 미국 **미체결/체결내역/취소** 조회가 전무하다. Phoenix의 US
> reconciliation·watermark 반영을 위해 `TTTS3018R`(미체결)·`TTTS3035R`(체결)·`TTTT1004U`(취소)
> 어댑터를 신규 구현해야 한다(§9·마이그레이션 Phase 1에 포함).

주문/US주문 성공 응답은 현재 **raw KIS dict 그대로 반환**되고 `output.ODNO`가 파싱되지 않는다
(`kis_api.py:744`, `:1507`). Phoenix `BrokerAdapter`는 이 `output`을 파싱해 이벤트에 결속한다.

---

## 1. KIS 국내 주문 — 상태별 응답 예시

주문 제출(`order-cash`) 성공 응답: **접수 확인일 뿐 체결 정보 없음**.
```jsonc
// TTTC0802U(매수) 성공 — [코드확인: rt_cd/msg_cd/msg1, output은 KIS규격]
{
  "rt_cd": "0", "msg_cd": "APBK0013", "msg1": "주문 전송 완료 되었습니다.",
  "output": {
    "KRX_FWDG_ORD_ORGNO": "00950",   // 주문채번지점번호(=ord_gno_brno)
    "ODNO":               "0000117057", // 거래소 주문번호 (당일·지점 스코프)
    "ORD_TMD":            "121052"     // 주문접수시각 HHMMSS
  }
}
```

이후 상태는 **`inquire-daily-ccld`(TTTC8001R) output1** 을 폴링해 관측한다. 같은 주문이 매 폴링마다
**누적값**으로 반복 등장한다(이벤트가 아니라 스냅샷). 상태는 `tot_ccld_qty` vs `ord_qty`로 판정:

```jsonc
// output1[i] — [코드확인: ord_dt/ord_tmd/pdno/prdt_name/sll_buy_dvsn_cd/
//              tot_ccld_qty/avg_prvs/tot_ccld_amt/ord_stts_name  (kis_api.py:1953-1962)]
// (아래 odno/orgn_odno/ord_qty/rmn_qty/cncl_yn 는 [KIS규격])

// ── ① 미체결(접수) ──
{ "ord_dt":"20260724","ord_gno_brno":"00950","odno":"0000117057","orgn_odno":"0000000000",
  "ord_tmd":"121052","pdno":"005930","prdt_name":"삼성전자","sll_buy_dvsn_cd":"02",
  "ord_qty":"10","ord_unpr":"80000","tot_ccld_qty":"0","rmn_qty":"10","avg_prvs":"0",
  "tot_ccld_amt":"0","cncl_yn":"N","ord_stts_name":"접수" }

// ── ② 부분체결 ──
{ ...,"odno":"0000117057","ord_qty":"10","tot_ccld_qty":"4","rmn_qty":"6",
  "avg_prvs":"80000","tot_ccld_amt":"320000","ord_stts_name":"부분체결" }

// ── ③ 체결완료 ──
{ ...,"odno":"0000117057","ord_qty":"10","tot_ccld_qty":"10","rmn_qty":"0",
  "avg_prvs":"80020","tot_ccld_amt":"800200","ord_stts_name":"체결" }

// ── ④ 정정 후 (원주문 orgn_odno 로 연결된 새 odno) ──
{ ...,"odno":"0000117099","orgn_odno":"0000117057","ord_unpr":"80500",
  "tot_ccld_qty":"0","rmn_qty":"6","ord_stts_name":"정정" }

// ── ⑤ 취소 ──
{ ...,"odno":"0000117101","orgn_odno":"0000117057","tot_ccld_qty":"0",
  "rmn_qty":"0","cncl_yn":"Y","ord_stts_name":"취소" }
```

미체결 잔량 확인용 **`inquire-psbl-rvsecncl`(TTTC8036R)** — 정정/취소 가능 주문만 나열:
```jsonc
// [코드확인: odno←item.odno, unexec_qty←rmn_qty, ord_qty←ord_qty (kis_api.py:856-867)]
{ "ord_gno_brno":"00950","odno":"0000117057","orgn_odno":"0000000000",
  "pdno":"005930","ord_qty":"10","rmn_qty":"6","psbl_qty":"6",   // psbl_qty=정정취소가능수량
  "ord_unpr":"80000","ord_dvsn_cd":"00","ord_tmd":"121052","sll_buy_dvsn_cd":"02" }
// 전량체결/취소 주문은 이 목록에서 사라짐 → "부재"가 곧 종결 신호
```

**정정/취소(`order-rvsecncl`, TTTC0803U)** 응답 — 새 ODNO 부여:
```jsonc
// 요청: RVSE_CNCL_DVSN_CD "01"=정정 / "02"=취소, ORGN_ODNO=원주문번호
{ "rt_cd":"0","msg_cd":"APBK0013","msg1":"정정취소 주문 완료",
  "output": { "KRX_FWDG_ORD_ORGNO":"00950","ODNO":"0000117101","ORD_TMD":"121530" } }
```
> **핵심**: 정정·취소는 **새 ODNO**를 만들고 `orgn_odno`로 원주문을 가리킨다. Phoenix는 주문 identity를
> "원주문 ODNO 체인(orgn_odno 링크)"으로 모델링하고, watermark는 **원주문 단위 누적 체결수량**에 적용한다.

---

## 2. KIS 미국 주문 — 상태별 응답 예시

제출(`/trading/order`, `TTTT1002U` 매수 / `TTTT1006U` 매도) 성공:
```jsonc
{ "rt_cd":"0","msg_cd":"APBK0013","msg1":"주문 전송 완료 되었습니다.",
  "output": { "KRX_FWDG_ORD_ORGNO":"00950","ODNO":"0030001234","ORD_TMD":"223015" } }
```

체결/미체결은 **US 전용 조회**로 관측(현행 코드 미구현 — Phoenix 신규):
```jsonc
// 체결내역 inquire-ccnl(TTTS3035R) output — [KIS규격]
// ── 부분체결 ──
{ "odno":"0030001234","orgn_odno":"0000000000","pdno":"TSLA","prdt_name":"TESLA",
  "sll_buy_dvsn_cd":"02","ft_ord_qty":"10","ft_ccld_qty":"4","nccs_qty":"6",   // 미체결수량
  "ft_ccld_unpr3":"250.30","ft_ccld_amt3":"1001.20","ord_tmd":"223015",
  "prcs_stat_name":"부분체결","rjct_rson_name":"" }
// ── 체결완료 ── { ...,"ft_ccld_qty":"10","nccs_qty":"0","prcs_stat_name":"체결" }
// ── 거절     ── { ...,"ft_ccld_qty":"0","prcs_stat_name":"거부","rjct_rson_name":"주문가능금액부족" }

// 미체결 inquire-nccs(TTTS3018R) output — [KIS규격]
{ "odno":"0030001234","pdno":"TSLA","ft_ord_qty":"10","nccs_qty":"6",
  "ft_ord_unpr3":"250.00","sll_buy_dvsn_cd":"02","ord_tmd":"223015" }
```
> US도 `ft_ccld_qty`(누적 체결수량)를 제공하므로 **Phase 1 watermark delta 모델이 동일 적용**된다.
> 잔고는 `inquire-balance`(TTTS3012R) `output1`의 `ovrs_cblc_qty`(해외잔고수량)·`pchs_avg_pric`(평단)로 확증.

---

## 3. 영구 ID 분석 — ODNO / ORD_TMD / 체결번호 / 체결시간

각 필드가 **durable idempotency ID로 쓸 수 있는지** 실제 KIS 규격 기준:

| 필드 | 성격 | 유일성 | 영구 ID 적격 |
|---|---|---|---|
| `ODNO` (주문번호) | 거래소 부여 주문번호 | **당일·채번지점 내 유일**. 날짜 넘어가면 재사용 가능 | ⚠️ 단독 불가 → **복합키 필요** |
| `KRX_FWDG_ORD_ORGNO`/`ord_gno_brno` | 채번지점번호 | 지점 식별 | ODNO 네임스페이스 보조 |
| `orgn_odno` (원주문번호) | 정정·취소 체인 링크 | 원주문 식별 | 주문 체인 결속용 |
| `ORD_TMD` (주문시각) | 주문 **접수**시각 HHMMSS | 초 단위 → 동시 주문 충돌 가능 | ❌ 식별자 아님 |
| **체결번호(체결ISNO/CNTG 등)** | per-fill 고유번호 | **웹소켓 실시간체결통보(H0STCNI0)에서만** 제공 | ✅ per-fill이면 최적 (단 REST엔 없음) |
| **체결시간** | 체결 시각 | 단독 비유일 | ❌ 보조만 |

**결론 (실측 기반 설계 결정):**

1. **주문 durable 키 (REST로 확보 가능)** =
   `order_key = (ord_dt, ord_gno_brno, odno)` — 당일 스코프 ODNO를 **날짜+지점으로 네임스페이스**한
   복합키. 정정/취소는 `orgn_odno`로 원주문에 결속해 **한 논리 주문 = 원주문 order_key**로 통합.

2. **per-fill durable ID는 REST에 없다.** `inquire-daily-ccld`/`inquire-ccnl`은 **주문단위 누적 스냅샷**
   (`tot_ccld_qty`/`ft_ccld_qty`)만 주고 **체결번호를 노출하지 않는다**. 체결번호는 국내
   **실시간체결통보 웹소켓(H0STCNI0/9)** 에서만 나온다.

3. 따라서 Phoenix 인바운드 멱등은 **REST-only 기준으로 설계**한다:
   - idempotency 단위 = `order_key`, 반영 단위 = **누적 체결수량 watermark**
     (`applied_qty`, delta=`max(0, cum-applied)`) — Phase 1 §5.2 그대로. per-fill ID가 없어도 정확·멱등.
   - `idempotency_key = f"exec:{ord_dt}:{ord_gno_brno}:{odno}:{cum_qty}"`.
4. **(선택) 웹소켓 체결통보 도입 시**: 체결번호+체결시간+ODNO로 per-fill 이벤트를 durable하게 얻어
   지연을 초→밀리초로 줄이고 폴링 부하를 없앨 수 있다. 단 필수는 아니며, 도입해도 REST watermark가
   **정합 대조(source of truth reconciliation)** 역할로 유지된다(웹소켓 유실 대비).

---

## 4. SQLite WAL — 백업 / 복구 / 손상 대응

**설정**: `journal_mode=WAL`, `synchronous=FULL`(체결 반영 트랜잭션), `wal_autocheckpoint=1000`,
`busy_timeout=5000`, `foreign_keys=ON`.

**백업 (온라인, 무중단):**
- 1차: `sqlite3` **Online Backup API**(`conn.backup(dst)`) 또는 `VACUUM INTO 'phoenix-YYYYMMDD-HHMM.db'`
  — WAL 포함 일관 스냅샷을 잠금 최소화로 생성. `cp`로 파일 복사는 **금지**(WAL/‑shm 불일치 위험).
- 주기: (a) 세션 시작·종료 시, (b) N분 주기(예 15분), (c) 매 스냅샷 직후. 보관: 로컬 최근 K개 +
  원격(오브젝트스토리지) 일 1회. 파일은 append-only 이벤트 로그라 증분 백업 친화적.
- 무결성 사전검증: 백업 직후 사본에 `PRAGMA integrity_check` + `PRAGMA wal_checkpoint(TRUNCATE)`.

**복구 (재시작 정상 경로):**
1. `PRAGMA quick_check` → OK면 그대로 사용(WAL 자동 재생은 SQLite가 처리).
2. RecoveryManager: snapshot 로드 → 증분 이벤트 재생 → 브로커 reconcile(§7·§9).

**손상 대응 (Phase 1 케이스 H 구체화):**
| 단계 | 조치 |
|---|---|
| 감지 | 부팅 시 `PRAGMA integrity_check`; 런타임 `SQLITE_CORRUPT`/`SQLITE_IOERR` 예외 |
| 격리 | 즉시 **Safe-Halt**(신규·청산 주문 전면 중단). GAP2식 "조용한 `{}` 리셋" 절대 금지 |
| 1차 복구 | WAL 재생 시도(`wal_checkpoint`), `.recover`(`sqlite3 dst .recover`)로 살릴 수 있는 이벤트 추출 |
| 2차 복구 | 최신 정상 **백업 + snapshot** 로 롤백 → 백업 시점 이후는 **브로커 조회로 재구성**(체결·잔고·미체결) → reconcile 이벤트로 적재 |
| 승인 | 재구성 상태를 브로커 잔고와 대조 리포트 → **운영자 수동 승인** 후에만 게이트 해제(§10) |

이벤트가 append-only라 부분 손상 시에도 **손상 지점 이전까지는 결정론적으로 재생 가능**하고, 이후는
브로커가 진실이므로 회수 가능하다.

---

## 5. Projection Snapshot — 생성 시점 / 삭제 정책 / Replay 속도

**스냅샷이란**: `positions/order_index/daily_pnl` projection의 특정 `last_seq` 시점 직렬화본
(`snapshot_meta`). 부팅 시 스냅샷 로드 후 `seq > last_seq` 이벤트만 재생 → 부팅 가속. **진실이 아니라
최적화**이며 언제든 이벤트 전량 재생으로 대체 가능.

**생성 시점:**
- 이벤트 **N건마다**(예 `N=5,000`) 또는 **T분마다**(예 10분) 중 먼저 도래 시.
- **세션 종료 reconciliation 직후**(가장 중요한 스냅샷 — 하루 마감 상태 고정, §9).
- Safe 조건: 진행 중 미종결 주문이 없거나, 있다면 그 order_index watermark까지 포함해 일관 시점에 생성.

**삭제 정책:**
- `snapshot_meta`는 **단일 최신본만** 유지(WHERE id=1 upsert) + 직전 1개를 백업 폴더에 보존(부팅 실패 롤백용).
- **이벤트는 삭제하지 않음**(권위 원천). 장기 보관 압박 시: 스냅샷보다 오래된 이벤트를 **아카이브
  테이블/파일로 이관**(cold storage)하되, 최소 **직전 스냅샷 이후 + 최근 M세션**은 hot 유지.
  이관 전 반드시 스냅샷 무결 검증. 완전 재생이 필요하면 아카이브를 다시 attach.

**Replay 속도(스냅샷 활용 시):** 스냅샷 간격 N=5,000이면 부팅 재생은 **평균 ≤ N건**. 이벤트당 projection
적용을 Python에서 ~50k–150k evt/s로 잡으면 5,000건 재생 = **수십 ms**. 즉 정상 부팅 재생은 사실상 무시 가능.

---

## 6. Replay 예상 시간 — 100만 Event

전량 재생(스냅샷 무시, 최악/재구축 시나리오) 가정:

| 처리 방식 | 처리율(가정) | 100만 evt 소요 |
|---|---|---|
| SQLite 순차 read만 | ~1–3M rows/s | 0.3–1 s |
| read + Python projection 적용(딕셔너리 갱신) | 80k–150k evt/s | **7–13 s** |
| read + 복잡 검증/객체화 포함 | 30k–60k evt/s | 17–33 s |

**결론**: 100만 이벤트 전량 재생은 대략 **10초 내외(≈7~30초 범위)**. 다만 **정상 운영에서는 스냅샷 덕에
전량 재생을 하지 않는다**(§5). 전량 재생은 (a) 스냅샷 손상, (b) projection 스키마 변경, (c) 감사/검증 시에만
발생. 가속 옵션: 배치 트랜잭션·`PRAGMA synchronous=OFF`(재생 전용 임시)·순수 dict 프로젝션. 실계좌 규모
추정: 하루 수백~수천 체결 → 100만 이벤트는 **수년치**에 해당하므로 실무 부팅 재생은 항상 짧다.

---

## 7. Broker vs Event 불일치 — 우선순위

**원칙: 축(axis)에 따라 권위가 다르다.**

| 축 | 권위 | 이유 |
|---|---|---|
| **현재 보유 수량**(position qty) | **브로커** | 실제 정산 잔고가 물리적 진실. 내부는 관측 지연/유실 가능 |
| **주문 종결 상태**(체결/취소/거절) | **브로커** | 체결 여부는 거래소 사실 |
| **인과 이력·실현손익 귀속·순서** | **이벤트 로그** | 브로커는 "지금 얼마 보유"만 알고 "왜/언제/어느 청산의 손익"은 모름 |
| **의도(intent)·전략 맥락** | **이벤트 로그** | 브로커에 존재하지 않는 정보 |

**불일치 해소 절차(항상 이벤트로 기록):**
1. reconcile 시 projection qty ≠ 브로커 qty 감지.
2. **브로커 qty를 채택**하되 **`PositionReconciled` 이벤트**로 조정(조용한 덮어쓰기 금지) — Δ, 원인 후보,
   관련 order_key 기록.
3. Δ가 미반영 체결로 설명되면 해당 `order_key`에 `ExecutionObserved`(watermark 전진)로 정규 반영,
   손익도 `RealizedPnlBooked`로 귀속.
4. Δ가 설명 불가(외부 수동매매·배당·유상증자 등)면 `PositionReconciled(reason=UNEXPLAINED)`로 남기고
   **운영자 알림**. 반복·대규모 Δ면 Safe-Halt.
5. **브로커 조회가 stale/합성 fallback**(`kis_api.py:1083-1097`)일 땐 신뢰하지 않고 reconcile 보류
   (fallback 판별 플래그 필요) — 잘못된 "0 보유"로 포지션을 지우지 않기 위함.

요약: **수량·종결은 브로커가 이기고, 역사·손익·의도는 이벤트가 이긴다. 브로커 승리도 반드시 이벤트로 남긴다.**

---

## 8. 실시간 Polling — TPS 계산

**한도**: KIS 실전 REST는 **앱키당 1초 20건**(코드가 참조하는 `EGW00201 "초당 20건"`, `app.py:686`).
안전계수 0.6 → **가용 예산 ≈ 12 req/s**(버스트·재시도 여유).

**폴링 대상 최소화(핵심)**: 전 종목이 아니라 **미종결 주문이 있는 종목·주문만** 폴링한다. 대부분 시간엔
미종결 주문이 0~소수.

예산 배분(정규장, 활성 주문 A개 가정):
```
account_sweep : inquire-balance + inquire-psbl-rvsecncl  → 2 req / cycle
active_orders : inquire-daily-ccld 는 계정단위 1콜로 A개 주문 커버 → 1 req / cycle
US(활성 시)   : inquire-ccnl + inquire-nccs               → 2 req / cycle
시세/전략      : 기존 전략 경로가 별도 사용(불변)          → 예산에서 분리 관리
─────────────────────────────────────────────────────────
Phoenix reconcile 비용 ≈ 3~5 req / cycle
```
- 활성 주문 존재 시 cycle 간격 **1s**(비용 3~5 req/s ≪ 12) → 여유. 활성 주문 없으면 cycle **5~10s**로 완화.
- `inquire-daily-ccld`는 **계정 단위 집계 조회**라 주문 수 A와 무관하게 1콜 → A가 커도 TPS 폭증 없음.
- backoff: `EGW00201` 수신 시 지수 백오프(기존 `_on_api_error` 재사용), 시세/전략 예산과 **분리 카운터**로
  상호 잠식 방지. 웹소켓 체결통보 도입 시 폴링 비용은 사실상 0(§3-4).

**결론**: 미종결 기반 선택 폴링으로 정규 운영 TPS는 **≤5 req/s**, 한도 20의 25% 이내. 안전.

---

## 9. 장 종료 후 Reconciliation

세션 종료(국내 15:30 이후 / 미국 정규장 마감 후) 시 **전량 대사**:
1. **미체결 정리**: `inquire-psbl-rvsecncl`(US: `inquire-nccs`)로 잔존 미체결 확인 → 정책상 자동취소 대상은
   취소 후 종결(기존 15:20 취소 로직을 Phoenix 게이트/이벤트로 이관, `app.py:464`).
2. **당일 체결 정합**: `inquire-daily-ccld`(US: `inquire-ccnl`)로 **당일 전 주문 누적 체결** 재조회 →
   각 order_key watermark와 대조해 폴링 중 놓친 delta를 `ExecutionObserved`로 보정 반영.
3. **잔고 대사**: `inquire-balance`(US: `TTTS3012R`)로 종목별 보유수량/평단 확증 → 불일치 시 §7 절차.
4. **일일 마감**: `DailySessionReset` 이벤트로 세션 손익 확정·다음 세션 키 개시(재시작과 새 세션을 명확히 구분).
5. **마감 스냅샷 + 백업**(§4·§5): 하루의 확정 상태를 고정. 이후 재부팅은 이 스냅샷에서 즉시 복원.
6. **일일 리포트**: 체결·실현손익·미반영 Δ·reconcile 이벤트 요약(감사 추적).

이로써 장중 폴링이 일부 유실돼도 **매일 1회 완전 대사로 수렴**한다(브로커=진실).

---

## 10. 실계좌 운영 중 DB 손상 — Fail-Safe 절차

원칙 10(실계좌 주문·배포 금지)은 **설계 검증 단계** 규칙이며, 아래는 향후 실계좌 운영을 가정한
**안전 정지 절차** 설계다(지금 배포하지 않음).

```
[감지]  런타임 SQLITE_CORRUPT/IOERR  또는  부팅 integrity_check 실패
   │
   ▼
[즉시 Safe-Halt]  ── OrderGate 전면 차단: 신규·추가매수·청산 전부 보류
   │              ── 진행 중 트랜잭션은 SQLite가 원자 롤백(부분반영 없음)
   │              ── 운영자 즉시 알림(텔레그램/notifier) : "DB 손상 — 자동매매 중단"
   ▼
[증거 보존]  손상 DB·WAL·shm 파일을 타임스탬프로 보관(덮어쓰기 금지)
   │
   ▼
[재구성]  ① 최신 정상 백업 + 스냅샷 로드 (§4)
          ② `.recover`로 손상본에서 추가 이벤트 최대 회수
          ③ 백업 시점 이후 공백은 브로커 조회로 재구성:
             - inquire-daily-ccld/ccnl(당일 체결)  - inquire-balance(잔고)
             - inquire-psbl-rvsecncl/nccs(미체결)
          ④ 회수·재구성 결과를 PositionReconciled/ExecutionObserved 이벤트로 새 정상 DB에 적재
   ▼
[대사 검증]  재구성 projection vs 브로커 잔고 100% 대조 리포트 생성
   │          불일치 잔존 시 → 계속 Halt
   ▼
[수동 승인 게이트]  운영자가 리포트 확인·승인해야만 RecoveryCompleted 이벤트 발행 → 게이트 해제
   │              (자동 재개 금지 — 손상 후 무인 재개는 금지)
   ▼
[정상 재개]  단, 최초 재개 후 첫 사이클은 신규 위험증가 주문을 1 cycle 유예(관측 우선)
```

**불변 규칙**:
- 손상 상태에서 **주문을 내지 않는다**(신규·청산 불문). "포지션 보호를 위한 긴급청산"조차 잔고 확증
  전에는 금지 — 손상 DB 기반 매도가 더 위험하기 때문.
- **조용한 자동 복구·리셋 금지**(GAP2 안티패턴). 모든 재구성은 이벤트로 남고 운영자 승인으로만 재개.
- **브로커가 최종 안전망**: DB가 완전 소실돼도 브로커 잔고+당일체결로 포지션·손익을 재구성할 수 있어야
  하며, 이것이 §7(브로커 우선 수량) 원칙의 실전 근거다.

---

## 부록 — Phase 1.5가 Phase 1에 추가/확정한 것
- 주문 durable 키를 **복합키 `(ord_dt, ord_gno_brno, odno)` + orgn_odno 체인**으로 확정(§3).
- per-fill ID는 REST에 없음을 실측 확인 → **누적수량 watermark 모델이 정답**임을 재확인(§3).
- **US 미체결/체결/취소 조회 어댑터 신규 필요**를 마이그레이션 범위에 편입(§0·§2·§9).
- WAL 백업/손상/복구·스냅샷 수명주기·TPS 예산·장마감 대사·실계좌 Fail-Safe를 운영 절차로 구체화(§4~10).
