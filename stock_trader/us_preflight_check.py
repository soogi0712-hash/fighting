"""
미국주식 실거래 사전 점검 스크립트 v2 (Pre-flight Check)
=========================================================
실 응답 필드 기준으로 수정된 버전:
  - output1: ovrs_pdno, ovrs_cblc_qty, pchs_avg_pric, now_pric2, ovrs_excg_cd
  - output2: tot_evlu_pfls_amt(평가금액), ovrs_tot_pfls(손익) — frcr_dncl_amt_2 없음
  - TTTS3007R: ITEM_CD 필수 / frcr_ord_psbl_amt1 = USD주문가능(환전포함) / exrt = 환율
  - HHDFS00000300: 미국장외 OPSQ2001 → yfinance 폴백
  - TTTT1002U: 주문 (지정가, 현재가 70% → 미체결)

단계:
  1. KIS 토큰 발급
  2. 해외주식 보유잔고 조회   (TTTS3012R)
  3. USD 예수금/평가금액 조회 (TTTS3012R output2 + TTTS3007R exrt 환율)
  4. 주문가능금액 조회        (TTTS3007R)
  5. 미국주식 1주 매수 주문   (TTTT1002U) — AAPL or 보유종목 거래소 기준
"""

import os, sys, json, time, requests
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import Config

BASE_URL   = Config.BASE_URL
APP_KEY    = Config.KIS_APP_KEY
APP_SECRET = Config.KIS_APP_SECRET
ACCOUNT_NO = Config.KIS_ACCOUNT_NO
ACC_NO, ACC_PROD = (ACCOUNT_NO.split('-') if '-' in ACCOUNT_NO else (ACCOUNT_NO, '01'))

SEP = "─" * 60

def banner(step, title):
    print(f"\n{'═'*60}")
    print(f"  STEP {step}: {title}")
    print(f"{'═'*60}")

def show(label, data):
    rt  = data.get('rt_cd',  'N/A')
    mc  = data.get('msg_cd', 'N/A')
    m1  = data.get('msg1',   'N/A')
    ico = "✅" if rt == "0" else "❌"
    print(f"  {ico}  rt_cd  : {rt}")
    print(f"       msg_cd : {mc}")
    print(f"       msg1   : {m1!r}")

def do_get(url, hdrs, params):
    r = requests.get(url, headers=hdrs, params=params, timeout=15)
    try: return r.status_code, r.json()
    except: return r.status_code, {}

def do_post(url, hdrs, body):
    r = requests.post(url, headers=hdrs, json=body, timeout=15)
    try: return r.status_code, r.json()
    except: return r.status_code, {}

def hashkey(body):
    r = requests.post(f"{BASE_URL}/uapi/hashkey",
        headers={'content-type':'application/json','appkey':APP_KEY,'appsecret':APP_SECRET},
        json=body, timeout=10)
    return r.json().get('HASH','')

# ════════════════════════════════════════════════════════════
# STEP 1: 토큰 발급
# ════════════════════════════════════════════════════════════
banner(1, "KIS OAuth2 토큰 발급")
print(f"  url    : {BASE_URL}/oauth2/tokenP")
print(f"  appkey : {APP_KEY[:8]}...")

t0 = time.time()
r = requests.post(f"{BASE_URL}/oauth2/tokenP",
    json={"grant_type":"client_credentials","appkey":APP_KEY,"appsecret":APP_SECRET}, timeout=15)
elapsed = time.time() - t0
d = r.json()

if 'access_token' not in d:
    print(f"  ❌  발급 실패: {d}")
    sys.exit(1)

TOKEN      = d['access_token']
expires_in = int(d.get('expires_in', 0))
print(f"  HTTP   : {r.status_code}  ({elapsed*1000:.0f}ms)")
print(f"  ✅  token : {TOKEN[:20]}...{TOKEN[-6:]}")
print(f"       만료 : {expires_in//3600}시간 후")

def H(tr_id):
    return {'content-type':'application/json; charset=utf-8',
            'authorization':f'Bearer {TOKEN}','appkey':APP_KEY,
            'appsecret':APP_SECRET,'tr_id':tr_id,'custtype':'P'}
def HH(tr_id, body):
    h = H(tr_id); h['hashkey'] = hashkey(body); return h

time.sleep(0.4)

# ════════════════════════════════════════════════════════════
# STEP 2: 해외주식 보유잔고 조회  (TTTS3012R)
# ════════════════════════════════════════════════════════════
banner(2, "해외주식 보유잔고 조회 (TTTS3012R)")
_tr2   = "TTTS3012R"
_url2  = f"{BASE_URL}/uapi/overseas-stock/v1/trading/inquire-balance"
_p2    = {"CANO":ACC_NO,"ACNT_PRDT_CD":ACC_PROD,
          "OVRS_EXCG_CD":"NASD","TR_CRCY_CD":"USD",
          "CTX_AREA_FK200":"","CTX_AREA_NK200":""}
print(f"  tr_id  : {_tr2}")
print(f"  url    : {_url2}")
print(f"  CANO   : {ACC_NO}  ACNT_PRDT_CD: {ACC_PROD}")

s2, d2 = do_get(_url2, H(_tr2), _p2)
print(f"  HTTP   : {s2}")
show("잔고", d2)

_out1 = d2.get('output1', [])
_out2 = d2.get('output2', {})
if isinstance(_out2, list): _out2 = _out2[0] if _out2 else {}

print(f"\n  [보유종목 {len(_out1)}개]")
_holdings_data = []
for _h in _out1:
    sym   = _h.get('ovrs_pdno', _h.get('pdno',''))       # ★ 실제 필드명
    name  = _h.get('ovrs_item_name', _h.get('prdt_name',''))
    qty   = int(_h.get('ovrs_cblc_qty', _h.get('ccld_qty_smtl', 0)) or 0)  # ★
    avg   = float(_h.get('pchs_avg_pric', 0) or 0)
    cur   = float(_h.get('now_pric2', 0) or 0)
    pnl   = float(_h.get('evlu_pfls_rt', 0) or 0)
    excd  = _h.get('ovrs_excg_cd', 'NASD')
    eval_ = float(_h.get('ovrs_stck_evlu_amt', 0) or 0)
    print(f"    {sym:6s} ({excd}) {name[:20]:20s} | "
          f"보유:{qty}주  매입${avg:.4f}  현재${cur:.4f}  "
          f"평가${eval_:.2f}  수익률{pnl:.2f}%")
    _holdings_data.append({"sym":sym,"excd":excd,"qty":qty,"cur":cur})

if not _out1:
    print("    (보유 종목 없음)")

print(f"\n  [output2 요약]")
for k in ['frcr_pchs_amt1','tot_evlu_pfls_amt','ovrs_tot_pfls',
          'ovrs_rlzt_pfls_amt','tot_pftrt','rlzt_erng_rt']:
    print(f"    {k:30s}: {_out2.get(k,'N/A')}")

time.sleep(0.4)

# ════════════════════════════════════════════════════════════
# STEP 3: USD 예수금 / 평가금액 요약
# ════════════════════════════════════════════════════════════
banner(3, "USD 예수금 / 평가금액 요약")

# output2에서 집계 가능한 값들
_total_eval_usd  = float(_out2.get('tot_evlu_pfls_amt', 0) or 0)  # 총 평가금액(USD)
_total_pfls_usd  = float(_out2.get('ovrs_tot_pfls', 0) or 0)      # 평가손익(USD)
_pchs_amt_usd    = float(_out2.get('frcr_pchs_amt1', 0) or 0)     # 매입금액(USD)

# USD 예수금: TTTS3012R에 frcr_dncl_amt_2 없음 → TTTS3007R의 exrt + 별도 예수금 조회
# KIS 해외 예수금 전용: CTRP6504R (해외주식 예수금) 시도
_usd_dep_tr = "CTRP6504R"
_usd_dep_url = f"{BASE_URL}/uapi/overseas-stock/v1/trading/inquire-deposit"
_usd_dep_p = {"CANO":ACC_NO,"ACNT_PRDT_CD":ACC_PROD,
              "CRCY_CD":"USD","CTX_AREA_FK200":"","CTX_AREA_NK200":""}
print(f"  [USD 예수금 조회] tr_id={_usd_dep_tr}")
s_dep, d_dep = do_get(_usd_dep_url, H(_usd_dep_tr), _usd_dep_p)
print(f"  HTTP   : {s_dep}")
show("예수금", d_dep)
_dep_out = d_dep.get('output1',[])
if isinstance(_dep_out, list): _dep_out = _dep_out[0] if _dep_out else {}
if isinstance(_dep_out, dict):
    print(f"\n  예수금 output1 키:")
    for k,v in list(_dep_out.items())[:15]:
        print(f"    {k:35s}: {v}")

_cash_usd = float(_dep_out.get('frcr_dncl_amt', _dep_out.get('frcr_dncl_amt_2',
                  _dep_out.get('dncl_amt', 0))) or 0) if isinstance(_dep_out,dict) else 0.0

print(f"\n  ──── USD 자산 요약 ────")
print(f"  USD 예수금      : ${_cash_usd:.2f}")
print(f"  해외주식 평가   : ${_total_eval_usd:.2f}")
print(f"  해외주식 손익   : ${_total_pfls_usd:.2f}")
print(f"  매입금액 합계   : ${_pchs_amt_usd:.2f}")

time.sleep(0.4)

# ════════════════════════════════════════════════════════════
# STEP 4: 주문가능금액 조회  (TTTS3007R)
# ════════════════════════════════════════════════════════════
banner(4, "해외주식 주문가능금액 조회 (TTTS3007R)")
_tr4  = "TTTS3007R"
_url4 = f"{BASE_URL}/uapi/overseas-stock/v1/trading/inquire-psamount"
# ★ ITEM_CD 필수: 빈값이면 APBN0746 에러
_test_sym  = _holdings_data[0]['sym'] if _holdings_data else "AAPL"
_test_excd = _holdings_data[0]['excd'] if _holdings_data else "NASD"
_p4 = {"CANO":ACC_NO,"ACNT_PRDT_CD":ACC_PROD,
       "OVRS_EXCG_CD":_test_excd,
       "OVRS_ORD_UNPR":"0",
       "ITEM_CD":_test_sym}   # ★ 빈값 금지
print(f"  tr_id        : {_tr4}")
print(f"  url          : {_url4}")
print(f"  ITEM_CD      : {_test_sym}  OVRS_EXCG_CD: {_test_excd}")
print(f"  OVRS_ORD_UNPR: 0 (시장가 기준)")

s4, d4 = do_get(_url4, H(_tr4), _p4)
print(f"  HTTP         : {s4}")
show("주문가능금액", d4)

_out4 = d4.get('output', {})
if isinstance(_out4, list): _out4 = _out4[0] if _out4 else {}

_krw_avail = float(_out4.get('ovrs_ord_psbl_amt',  0) or 0)  # 원화주문가능(환전포함 KRW)
_usd_avail = float(_out4.get('frcr_ord_psbl_amt1', 0) or 0)  # USD주문가능(환전포함)
_exrt      = float(_out4.get('exrt', 0) or 0)                # 적용환율
_max_qty   = int(_out4.get('ovrs_max_ord_psbl_qty', 0) or 0) # 최대주문가능수량

print(f"\n  [주문가능금액 상세]")
for k in ['ovrs_ord_psbl_amt','frcr_ord_psbl_amt1','ord_psbl_frcr_amt',
          'ovrs_max_ord_psbl_qty','max_ord_psbl_qty','exrt',
          'echm_af_ord_psbl_amt','sll_ruse_psbl_amt']:
    print(f"    {k:35s}: {_out4.get(k,'N/A')}")

print(f"\n  ──── 주문가능 요약 ────")
print(f"  원화 주문가능   : {_krw_avail:,.0f}원  (원화잔고 + 자동환전)")
print(f"  USD 주문가능    : ${_usd_avail:.2f}  (환전 포함 USD 환산)")
print(f"  적용 환율       : {_exrt:.2f}원/USD")
print(f"  최대주문수량    : {_max_qty}주")

if _usd_avail > 0:
    print(f"  ✅  USD 주문 가능 (${_usd_avail:.2f})")
elif _krw_avail > 0:
    print(f"  ⚠️  USD 직접 부족 → 원화 자동환전 주문 가능 ({_krw_avail:,.0f}원)")
else:
    print(f"  ❌  주문가능금액 없음")

time.sleep(0.4)

# ════════════════════════════════════════════════════════════
# STEP 5: 미국주식 1주 매수 주문 (TTTT1002U)
# ════════════════════════════════════════════════════════════
banner(5, f"미국주식 매수 주문 테스트 (TTTT1002U) — {_test_sym} 1주")

# ── 5-0: 현재가 조회 ─────────────────────────────────────────
_ptr  = "HHDFS00000300"
_purl = f"{BASE_URL}/uapi/overseas-price/v1/quotations/price"
_pp   = {"AUTH":"","EXCD":_test_excd,"SYMB":_test_sym}
print(f"  [5-0] {_test_sym} 현재가 (tr_id={_ptr}  EXCD={_test_excd})")
sp, dp = do_get(_purl, H(_ptr), _pp)
print(f"  HTTP   : {sp}")
show("현재가", dp)
_pout   = dp.get('output', {})
_cur_p  = float(_pout.get('last', 0) or 0)
print(f"  현재가 필드(last) : {_pout.get('last','N/A')}")
print(f"  거래소(rsym)      : {_pout.get('rsym','N/A')}")

if _cur_p <= 0:
    print(f"  ⚠️  KIS 현재가 0 (장외시간) → yfinance 폴백")
    try:
        import yfinance as yf
        _cur_p = float(yf.Ticker(_test_sym).fast_info.last_price)
        print(f"  yfinance 현재가 : ${_cur_p:.4f}")
    except Exception as e:
        # 보유 종목의 경우 잔고에서 now_pric2 사용
        if _holdings_data:
            _cur_p = _holdings_data[0]['cur']
        else:
            _cur_p = 200.0
        print(f"  yfinance 실패 → 잔고 현재가 사용: ${_cur_p:.4f}")

time.sleep(0.4)

# ── 5-1: 주문가격 결정 (현재가 70% = 미체결 보장) ─────────────
_test_price = round(_cur_p * 0.70, 2)
_need_usd   = _test_price

print(f"\n  [5-1] 주문 조건")
print(f"  {_test_sym} 현재가   : ${_cur_p:.4f}")
print(f"  테스트 주문가 (×0.70): ${_test_price:.2f}  ← 미체결 보장")
print(f"  필요 금액     : ${_need_usd:.2f}")
print(f"  USD 가용      : ${_usd_avail:.2f}")
print(f"  원화 가용     : {_krw_avail:,.0f}원")

_can_order = (_usd_avail >= _need_usd) or (_krw_avail > 0)
if not _can_order:
    print(f"  ❌  주문가능금액 부족 → 테스트 건너뜀")
else:
    print(f"  ✅  주문 조건 충족 → 주문 진행")

# ── 5-2: 주문 전송 ────────────────────────────────────────────
if _can_order:
    _otr  = "TTTT1002U"
    _ourl = f"{BASE_URL}/uapi/overseas-stock/v1/trading/order"
    _obody = {
        "CANO":            ACC_NO,
        "ACNT_PRDT_CD":    ACC_PROD,
        "OVRS_EXCG_CD":    _test_excd,
        "PDNO":            _test_sym,
        "ORD_DVSN":        "00",
        "ORD_QTY":         "1",
        "OVRS_ORD_UNPR":   f"{_test_price:.2f}",
        "ORD_SVR_DVSN_CD": "0",
    }

    print(f"\n  [5-2] 주문 요청 바디")
    print(f"  tr_id          : {_otr}")
    print(f"  url            : {_ourl}")
    print(f"  PDNO           : {_test_sym}")
    print(f"  OVRS_EXCG_CD   : {_test_excd}")
    print(f"  ORD_QTY        : 1")
    print(f"  ORD_DVSN       : 00 (지정가)")
    print(f"  OVRS_ORD_UNPR  : {_test_price:.2f}")
    print(f"  CANO           : {ACC_NO}  ACNT_PRDT_CD: {ACC_PROD}")

    t0 = time.time()
    _so, _do = do_post(_ourl, HH(_otr, _obody), _obody)
    elapsed = time.time() - t0

    _rt   = _do.get('rt_cd',  'N/A')
    _mc   = _do.get('msg_cd', 'N/A')
    _m1   = _do.get('msg1',   'N/A')
    _oout = _do.get('output', {})
    _odno = _oout.get('ODNO', _oout.get('odno', ''))
    _otm  = _oout.get('ORD_TMD', _oout.get('ord_tmd', ''))

    print(f"\n  [5-3] 주문 응답")
    print(f"  HTTP           : {_so}  ({elapsed*1000:.0f}ms)")
    print(f"  tr_id          : {_otr}")
    print(f"  rt_cd          : {_rt}")
    print(f"  msg_cd         : {_mc}")
    print(f"  msg1           : {_m1!r}")
    print(f"  주문번호 (ODNO): {_odno if _odno else '(없음)'}")
    print(f"  주문시각       : {_otm if _otm else '(없음)'}")

    if _rt == "0":
        print(f"\n  ✅  주문 접수 성공!")
        print(f"  ⚠️  미체결 주문 {_odno} → KIS 앱/HTS에서 확인 후 취소하세요")
    elif _mc in ("APBN0745","APBN0768","APBN0126") or "장" in (_m1 or ""):
        print(f"\n  ⚠️  미국장 휴장 또는 주문가능시간 아님 (msg_cd={_mc})")
        print(f"       주문 바디 자체는 정상 — 장중 재실행 시 접수 예상")
    elif "잔고" in (_m1 or "") or "부족" in (_m1 or ""):
        print(f"\n  ❌  잔고 부족: {_m1!r}")
    else:
        print(f"\n  ❌  주문 실패 — 상세:")
        print(json.dumps(_do, ensure_ascii=False, indent=4))

# ════════════════════════════════════════════════════════════
# 최종 요약
# ════════════════════════════════════════════════════════════
print(f"\n{'═'*60}")
print(f"  미국주식 실거래 사전 점검 완료  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print(f"{'═'*60}")
print(f"  1. 토큰 발급       : ✅  (24시간 유효)")
print(f"  2. 해외잔고 조회   : {'✅' if d2.get('rt_cd')=='0' else '❌'}  (rt_cd={d2.get('rt_cd')}  보유 {len(_out1)}종목)")
print(f"  3. USD 예수금      : ${_cash_usd:.2f}  /  평가 ${_total_eval_usd:.2f}")
print(f"  4. 주문가능금액    : USD ${_usd_avail:.2f}  (환율 {_exrt:.2f}원)  최대{_max_qty}주")
if _can_order:
    _s5 = "✅ 주문 접수" if _rt=="0" else f"⚠️ 장외시간({_mc})" if "장" in (_m1 or "") else f"❌ {_mc}"
    print(f"  5. 주문 테스트     : {_s5}  ODNO={_odno}")
else:
    print(f"  5. 주문 테스트     : ⏭️ 건너뜀 (잔고 부족)")
print(f"{'═'*60}\n")
