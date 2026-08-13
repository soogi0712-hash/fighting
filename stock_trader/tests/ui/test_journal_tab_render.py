"""매매일지 탭 화면 렌더링 테스트 (headless — flask 미설치 환경에서도 동작).

dashboard.html 템플릿에 매매일지 탭의 필수 UI 요소·JS 배선·색상 관례가
포함되어 있는지 정적 검증한다. (flask/app import 없이 템플릿 파일 자체 검사)
"""
import os
import re
import unittest

_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
_TPL = os.path.join(_ROOT, "templates", "dashboard.html")


class JournalTabRenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(_TPL, encoding="utf-8") as f:
            cls.html = f.read()

    def test_tab_button_and_content_exist(self):
        self.assertIn("매매일지", self.html)
        self.assertIn("showTab('tab-journal'", self.html)
        self.assertIn('id="tab-journal"', self.html)

    def test_showtab_wired(self):
        # showTab 배열에 tab-journal 포함 + 지연로드 분기
        self.assertRegex(self.html, r"\['tab-chart'.*'tab-journal'.*\]")
        self.assertIn("if(id==='tab-journal') loadJournal();", self.html)

    def test_summary_cards_present(self):
        for cid in ["jc-all", "jc-kr", "jc-us", "jc-trades", "jc-wl", "jc-winrate", "jc-avg"]:
            self.assertIn(f'id="{cid}"', self.html, cid)

    def test_filters_present(self):
        for fid in ["jf-from", "jf-to", "jf-market", "jf-status", "jf-exit", "jf-q"]:
            self.assertIn(f'id="{fid}"', self.html, fid)
        # 상태·매도사유 옵션
        for opt in ["보유중", "확인대기", "청산완료", "거절",
                    "트레일링", "익절", "손절", "시간청산", "수동", "기타"]:
            self.assertIn(opt, self.html, opt)

    def test_table_columns_present(self):
        for col in ["시장", "종목", "매수일시", "매도일시", "수량",
                    "평균매수가", "평균매도가", "실현손익", "수익률",
                    "매수사유", "매도사유", "상태"]:
            self.assertIn(col, self.html, col)

    def test_js_functions_present(self):
        for fn in ["function loadJournal(", "function loadJournalCard(",
                   "function loadJournalList(", "function openJournalModal(",
                   "function closeJournalModal(", "function resetJournalFilter("]:
            self.assertIn(fn, self.html, fn)

    def test_detail_modal_and_timeline_labels(self):
        self.assertIn('id="jnl-modal"', self.html)
        self.assertIn('id="jm-body"', self.html)
        # 상세 모달 항목 (신호/주문/접수/체결/타임라인/트레일링/점수/사유)
        for lbl in ["신호 발생", "주문 제출", "주문번호", "접수", "부분체결",
                    "완전체결", "타임라인", "트레일링 최고가", "보유시간",
                    "보유 중 최고수익률", "보유 중 최대하락률"]:
            self.assertIn(lbl, self.html, lbl)

    def test_uses_existing_journal_apis(self):
        self.assertIn("/api/trading/journal-card", self.html)
        self.assertIn("/api/trading/journal?", self.html)
        self.assertIn("/api/trading/journal/", self.html)

    def test_profit_red_loss_blue_convention(self):
        # 국내 관례: 수익 빨강(--red), 손실 파랑(--blue)
        self.assertRegex(self.html, r"n>0\?'color:var\(--red\)':'color:var\(--blue\)'")

    def test_auto_refresh_30s_and_manual(self):
        # 30초 자동갱신(탭 표시 중 + 토글 on) + 수동 새로고침 버튼
        self.assertIn('id="jnl-auto"', self.html)
        # 자동갱신 setInterval 블록: tab-journal 가시성 + jnl-auto + loadJournal + 30000
        self.assertRegex(
            self.html,
            r"tab-journal[\s\S]{0,200}?jnl-auto[\s\S]{0,120}?loadJournal\(\)[\s\S]{0,40}?30000",
        )
        self.assertIn('onclick="loadJournal()"', self.html)

    def test_graceful_dash_for_missing(self):
        # 결측값 '-' 처리 헬퍼 존재
        self.assertIn("function _jDash(", self.html)

    def test_responsive_viewport(self):
        self.assertIn('name="viewport"', self.html)

    # ── 리뷰 회귀: XSS 방지 (이스케이프 헬퍼 + 적용) ──────────
    def test_xss_escape_helper_and_applied(self):
        self.assertIn("function _jEsc(", self.html)
        self.assertIn("&lt;", self.html)  # 이스케이프 매핑 존재
        # 종목명·매수사유는 이스케이프 적용, 원시 삽입 금지
        self.assertIn("_jEscDash(r.name)", self.html)
        self.assertIn("_jEscDash(r.entry_reason)", self.html)
        self.assertNotIn("${_jDash(r.name)}", self.html)
        self.assertNotIn("${r.name}", self.html)
        self.assertNotIn("${r.entry_reason}", self.html)
        # note 이스케이프(부분치환 제거)
        self.assertIn("_jEsc(ev.note)", self.html)
        self.assertNotIn("String(ev.note).replace(/</g", self.html)
        # 모달 사유/전략도 이스케이프
        self.assertIn("_jEscDash(x.exit_reason)", self.html)
        self.assertIn("_jEscDash(e.strategy_name)", self.html)

    # ── 리뷰 회귀: 탭 클릭 시 중복 loadJournal 없음 ───────────
    def test_no_double_load_on_tab_click(self):
        # 탭 버튼 onclick 은 showTab 만 호출(loadJournal 중복 호출 금지)
        self.assertIn('''onclick="showTab('tab-journal',this)"''', self.html)
        self.assertNotIn('''showTab('tab-journal',this);loadJournal()''', self.html)
        # 로드 트리거는 showTab 분기 한 곳
        self.assertIn("if(id==='tab-journal') loadJournal();", self.html)

    # ── 리뷰 회귀: 자동갱신 타이머 단일 등록 ──────────────────
    def test_single_autorefresh_timer(self):
        # 가드형 자동갱신 블록(탭 표시 중 + 토글 on)이 정확히 1회만 등록
        self.assertEqual(self.html.count("auto && auto.checked){ loadJournal(); }"), 1)
        # setInterval 로 직접 loadJournal 을 무조건 호출하는 형태는 없어야 함(가드 필수)
        self.assertNotIn("setInterval(loadJournal", self.html)

    # ── 리뷰 회귀: US 환율 표시 정책(모달·카드) ───────────────
    def test_us_fx_policy_shown(self):
        self.assertIn("원화 환산", self.html)
        self.assertIn("환율 미확인", self.html)

    # ── 리뷰 회귀: 모바일 반응형(테이블 가로스크롤 + 모달) ────
    def test_mobile_responsive_containers(self):
        # 목록은 table-wrap(overflow-x)로 감싸 모바일 가로 스크롤
        self.assertRegex(self.html, r'class="table-wrap"[\s\S]*?id="jnl-tbody"')
        # 필터바 flex-wrap 으로 줄바꿈
        self.assertRegex(self.html, r'id="jf-from"[\s\S]{0,400}?')
        self.assertIn("flex-wrap:wrap", self.html)
        # 모달: 화면 넘칠 때 스크롤 + 반응형 폭
        m = re.search(r'id="jnl-modal"[\s\S]{0,400}', self.html)
        self.assertIsNotNone(m)
        self.assertIn("overflow:auto", m.group(0))
        self.assertIn("width:100%", m.group(0))
        self.assertIn("max-width:", m.group(0))


if __name__ == "__main__":
    unittest.main()
