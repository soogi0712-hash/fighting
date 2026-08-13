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


if __name__ == "__main__":
    unittest.main()
