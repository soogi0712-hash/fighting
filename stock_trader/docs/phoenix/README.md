# Project Phoenix — Crash-Safe 체결 엔진 (설계 문서 / Phase 1)

> 상태: **설계 단계 (구현 전 · 승인 대기)**. 이 폴더는 설계 산출물만 포함하며 production 코드는 없다.
> 원칙 9(전략 점수·매매조건 불변) / 원칙 10(실계좌 주문·배포 금지) 준수.

## 문서
| # | 문서 | 내용 |
|---|---|---|
| 00 | [`00-GAP2-FAILURE-ANALYSIS.md`](./00-GAP2-FAILURE-ANALYSIS.md) | 기존 GAP2 구조 분석 + 6대 실패 원인(코드 근거) |
| 01 | [`01-PHOENIX-DESIGN.md`](./01-PHOENIX-DESIGN.md) | 아키텍처 / 이벤트 스키마 / 주문 상태머신 / 트랜잭션 모델 / 멱등 모델 / crash matrix(A~I) / replay·reconciliation / 복구 게이트 / position·PnL projection / 테스트·카오스 / 마이그레이션 |

## 한 줄 요약
여러 JSON 파일 + 메모리 큐(비원자·비영속)를 **단일 SQLite 이벤트 스토어**로 대체하고,
**이벤트 append와 projection 갱신을 하나의 트랜잭션**으로 커밋하며, 체결을 **누적 스냅샷의
watermark delta**로 멱등 반영하고, **제출 직전 복구 게이트**로 신규 위험증가 주문을 차단한다.
