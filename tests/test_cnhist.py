"""v0.37 大中华区历史数据：申万指数/横截面、交易所统计、ETF 期权、国家统计局降级、上海金、完整日线序列、ALFRED vintage、
财务统一字段与双源校验、Tushare 只在自填 token 时启用。全部离线（HTTP 用假响应）。"""
import json

import pytest

from stock_agent import cnhist


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    cnhist._cache.clear()


class R:
    def __init__(self, obj=None, text=None, status=200):
        self._obj, self.status_code = obj, status
        self.text = text if text is not None else json.dumps(obj, ensure_ascii=False)

    def json(self):
        return self._obj if self._obj is not None else json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code} Client Error: Forbidden")


def _trend(code, closes, start_day=20):
    days = [f"2018-03-{d:02d}" for d in range(start_day, start_day + len(closes))]
    return {"code": "200", "data": [{"swindexcode": code, "bargaindate": d, "openindex": c, "maxindex": c, "minindex": c, "closeindex": c,
                                      "markup": 0, "bargainamount": 1, "bargainsum": 2} for d, c in zip(days, closes)]}


def test_sw_rank_single_day_uses_prev_close(monkeypatch):
    def fake(method, url, **kw):
        code = kw["params"]["swindexcode"]
        drop = {"801770": 0.9329, "801750": 0.9372}.get(code, 0.99)
        return R(_trend(code, [100.0, 100.0, 100.0 * drop]))  # 03-20, 03-21, 03-22
    monkeypatch.setattr(cnhist.requests, "request", fake)
    r = cnhist.sw_rank("2018-03-22", version="2014", top=3)
    assert r["count"] == 28 and r["version"] == "2014"
    top = r["rows_ascending"]
    assert [x["name"] for x in top[:2]] == ["通信", "计算机"]
    assert top[0]["change_pct"] == pytest.approx(-6.71, abs=0.01) and top[0]["start_base"] == "2018-03-21"


def test_sw_index_by_name_and_range(monkeypatch):
    monkeypatch.setattr(cnhist.requests, "request", lambda m, u, **kw: R(_trend(kw["params"]["swindexcode"], [10.0, 11.0, 12.1, 13.0])))
    r = cnhist.sw_index("食品饮料", "2018-03-21", "2018-03-22")
    assert r["index"] == "801120" and r["name"] == "食品饮料"
    assert r["range"]["change_pct_from_prev_close"] == pytest.approx(21.0)
    assert r["columns"][0] == "date" and len(r["rows"]) == 2  # 短区间逐日列出


def test_sw_index_unknown_name_is_error_not_number(monkeypatch):
    monkeypatch.setattr(cnhist.requests, "request", lambda m, u, **kw: R({"code": "200", "data": {"results": [], "next": None}}))
    r = cnhist.sw_index("不存在的行业")
    assert "error" in r and "rows" not in r


def test_exchange_stats_szse_parses_and_steps_back(monkeypatch):
    calls = []

    def fake(method, url, **kw):
        d = kw["params"]["txtQueryDate"]
        calls.append(d)
        data = [] if d == "2022-12-31" else [{"lbmc": "股票", "zqsl": "2,778", "cjje": "3,546.30", "sjzz": "324,219.14", "ltsz": "265,243.54"},
                                              {"lbmc": "&nbsp;&nbsp;主板A股", "zqsl": "1504", "cjje": "2,459.47", "sjzz": "210,968.81", "ltsz": "185,350.35"}]
        return R([{"metadata": {}, "data": data}])
    monkeypatch.setattr(cnhist.requests, "request", fake)
    r = cnhist.exchange_stats("sz", "2022-12-31")
    assert calls[:2] == ["2022-12-31", "2022-12-30"] and r["date"] == "2022-12-30"
    assert r["rows"][0] == {"category": "股票", "count": 2778.0, "turnover_100m": 3546.3, "total_mv_100m": 324219.14, "float_mv_100m": 265243.54}
    assert r["rows"][1]["category"] == "主板A股"


def test_exchange_stats_bse_gives_hint():
    r = cnhist.exchange_stats("bj", "2022-12-30")
    assert "error" in r and "web_search" in r["hint"]


def test_etf_option_by_code_and_by_listing(monkeypatch):
    daily = "/*x*/ ([" + ",".join(json.dumps({"d": d, "o": "0.1", "h": "0.2", "l": "0.05", "c": c, "v": "100"}) for d, c in
                                    (("2025-04-28", "0.10"), ("2025-04-29", "0.12"), ("2025-04-30", "0.13"))) + "])"
    quote = 'var hq_str_CON_OP_10009001="' + ",".join(["0"] * 7 + ["3.3000"] + ["0"] * 29 + ["300ETF沽5月3300"]) + '";'

    def fake(method, url, **kw):
        if "getSymbolInfo" in url:
            return R(text=daily)
        if "OP_DOWN_5103002505" in url:
            return R(text='var hq_str_OP_DOWN_5103002505="CON_OP_10009001,";')
        return R(text=quote)
    monkeypatch.setattr(cnhist.requests, "request", fake)
    r = cnhist.etf_option(underlying="300ETF", month="2505", cp="P", strike=3.3)
    assert r["code"] == "10009001" and r["name"] == "300ETF沽5月3300"
    assert r["rows"][-1][r["columns"].index("change_pct")] == pytest.approx(8.3333, abs=1e-3)
    r2 = cnhist.etf_option(underlying="300ETF", month="2505", cp="P", strike=3.4)
    assert "error" in r2 and "合约编码" in r2["hint"]


def test_nbs_blocked_degrades_with_hint(monkeypatch):
    monkeypatch.setattr(cnhist.requests, "request", lambda m, u, **kw: R(text="<html>403</html>", status=403))
    r = cnhist.nbs(query="城市医疗救助")
    assert "error" in r and "统计年鉴" in r["hint"] and "results" not in r


def test_nbs_querydata_parses(monkeypatch):
    payload = {"returndata": {"wdnodes": [{"wdcode": "zb", "nodes": [{"code": "A0201", "cname": "国内生产总值", "unit": "亿元"}]}],
                              "datanodes": [{"data": {"hasdata": True, "data": 1349084.3}, "wds": [{"wdcode": "zb", "valuecode": "A0201"}, {"wdcode": "sj", "valuecode": "2024"}]},
                                            {"data": {"hasdata": False, "data": 0}, "wds": [{"wdcode": "zb", "valuecode": "A0201"}, {"wdcode": "sj", "valuecode": "2025"}]}]}}
    monkeypatch.setattr(cnhist.requests, "request", lambda m, u, **kw: R(payload))
    r = cnhist.nbs(zb="A0201", period="2024-2025")
    assert r["rows"] == [{"indicator": "国内生产总值", "unit": "亿元", "period": "2024", "value": 1349084.3}]


def test_gold_benchmark(monkeypatch):
    # 2025-05-06 00:00 北京时间 = 2025-05-05 16:00 UTC
    ts = 1746460800000
    monkeypatch.setattr(cnhist.requests, "request", lambda m, u, **kw: R({"zp": [[ts, 796.33]], "wp": [[ts, 792.86]]}))
    r = cnhist.gold("sge_benchmark", "2025-05-01", "2025-05-10")
    assert r["rows"] == [["2025-05-06", 796.33, 792.86]] and "周大福" in r["note"]


def test_series_payload_full_and_long(tmp_path):
    rows = [{"date": f"2024-01-{i:02d}", "open": 1, "high": 2, "low": 0.5, "close": i, "volume": 9} for i in range(1, 29)]
    out = cnhist.series_payload(rows, "x", True)
    assert out["_full_series"] and out["columns"] == ["date", "open", "high", "low", "close", "volume"] and len(out["rows"]) == 28
    assert out["csv"].startswith("/workspace/data/") and (tmp_path / "workspace" / "data" / "x.csv").exists()
    long = [{"date": str(i), "open": 1, "close": i} for i in range(500)]
    out = cnhist.series_payload(long, "y", True)
    assert out["columns"] == ["date", "close"] and len(out["rows"]) == 500
    out = cnhist.series_payload(long, "z", False)
    assert "rows" not in out and "full=true" in out["series_note"]


def test_price_history_full_series(monkeypatch):
    from stock_agent import edgar
    rows = [{"date": f"2023-{m:02d}-{d:02d}", "open": 1.0, "high": 2.0, "low": 0.5, "close": float(m * 100 + d), "volume": 1}
            for m in range(1, 8) for d in range(1, 29)]
    monkeypatch.setattr(edgar, "_yahoo_daily", lambda s, a, b: (list(rows), {"symbol": "BTC-USD"}))
    monkeypatch.setattr(edgar, "_fetch", lambda *a, **k: {})
    r = edgar.price_history("BTC-USD", start="2023-01-01", end="2023-07-28")
    assert "series" not in r and "full=true" in r["series_note"]
    r = edgar.price_history("BTC-USD", start="2023-01-01", end="2023-07-28", full=True)
    assert r["_full_series"] and len(r["rows"]) == len(rows) and r["columns"][0] == "date"


def test_macro_vintage_uses_alfred(monkeypatch):
    from stock_agent import macro
    seen = []

    def fake(url, params=None):
        seen.append((url, dict(params or {})))
        return "observation_date,TOTBKCR_20250718\n2024-12-25,17956.1\n" if "alfred" in url else "<title>Bank Credit | FRED</title>"
    monkeypatch.setattr(macro, "_get", fake)
    r = macro.series("TOTBKCR", "2024-12-01", "2024-12-31", vintage="2025-07-18")
    assert "alfredgraph" in seen[0][0] and seen[0][1]["vintage_date"] == "2025-07-18"
    assert r["vintage"] == "2025-07-18" and r["observations"] == [{"date": "2024-12-25", "value": 17956.1}] and "ALFRED" in r["source"]


def test_cn_data_dispatches_new_kinds(monkeypatch):
    from stock_agent import cndata
    monkeypatch.setattr(cnhist, "gold", lambda *a, **k: {"ok": a})
    assert cndata.run_tool("cn_data", {"kind": "gold", "sub": "spot", "instid": "Au(T+D)"}) == {"ok": ("spot", "", "", "Au(T+D)", False)}
    spec = cndata.TOOL_SPECS[0]
    assert all(k in spec["description"] for k in cnhist.KINDS) and "full" in spec["parameters"]["properties"]


# ---------------------------------------------------------------- 财务统一字段 + 双源校验
def test_finunify_cross_flags_over_threshold():
    from stock_agent import finunify
    em = [{"period": "2024-12-31", "revenue": 100.0, "net_profit": 50.0, "gross_margin": 90.0, "net_margin": 50.0}]
    ya = [{"period": "2024-12-31", "revenue": 104.0, "net_profit": 56.0, "gross_margin": 90.1, "net_margin": 40.0}]
    cc = finunify.cross({"东方财富": em, "Yahoo Finance": ya})
    flagged = {it["field"] for it in cc["items"] if it["flag"]}
    assert flagged == {"net_profit"}  # 营收差 3.8% 不报；净利差 10.7% 报；净利率口径不同不比
    assert "Yahoo Finance" in cc["warnings"][0] and "定期报告" in cc["warnings"][0]


def test_finunify_eastmoney_adapter_names():
    from stock_agent import finunify
    rows = finunify.from_eastmoney([{"REPORT_DATE": "2024-12-31 00:00:00", "TOTALOPERATEREVE": 1.7e11, "XSMLL": 91.9, "ROEJQ": 36.0, "EPSJB": 68.6}])
    assert rows[0] == {"period": "2024-12-31", "basis": "cumulative", "currency": "CNY", "revenue": 1.7e11, "gross_margin": 91.9, "roe": 36.0, "eps": 68.6}


def test_fincheck_card_only_when_flagged():
    from stock_agent import cards
    r = {"symbol": "600519.SS", "name": "贵州茅台", "fin_crosscheck": {"threshold_pct": 5, "items": [
        {"period": "2024-12-31", "field": "net_profit", "label": "归母净利润", "values": {"东方财富": 50, "Yahoo Finance": 56}, "spread_pct": 10.7, "flag": True}]}}
    c = cards.build("get_financials", {}, r)
    assert c and c[0]["type"] == "fincheck" and c[0]["items"][0]["field"] == "net_profit"
    r["fin_crosscheck"]["items"][0]["flag"] = False
    assert cards.build("get_financials", {}, r) == []


def test_cite_binds_unified_fields():
    from stock_agent import cite
    src = cite.Sources()
    src.add("get_financials", {"symbol": "600519.SS"}, {"symbol": "600519.SS", "name": "贵州茅台", "source": "东方财富 F10",
                                                       "unified": [{"source": "东方财富", "period": "2024-12-31", "revenue": 174144000000.0, "gross_margin": 91.93}]})
    v = cite.verify("贵州茅台 2024 年营业总收入 1741.44 亿元，2024 年毛利率 91.93%。", src)
    assert v["ok"], v


def test_tushare_disabled_without_token(monkeypatch):
    from stock_agent.sources import tushare
    from stock_agent import data
    monkeypatch.setattr(tushare.requests, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应联网")))
    assert not tushare.enabled() and data.p_tushare("600519.SS", 30) is None


def test_tushare_financials_unified(monkeypatch):
    from stock_agent.sources import tushare
    monkeypatch.setenv("TUSHARE_TOKEN", "user-own-token")

    def fake(url, timeout=None, json=None):
        api = json["api_name"]
        assert json["token"] == "user-own-token"
        if api == "income":
            return R({"code": 0, "data": {"fields": ["end_date", "report_type", "total_revenue", "n_income_attr_p"], "items": [["20241231", "1", 1.0e11, 5.0e10]]}})
        return R({"code": 0, "data": {"fields": ["end_date", "grossprofit_margin", "roe_waa", "eps"], "items": [["20241231", 91.9, 36.0, 68.6]]}})
    monkeypatch.setattr(tushare.requests, "post", fake)
    r = tushare.financials("600519.SS")
    assert r["rows"][0] == {"period": "2024-12-31", "revenue": 1.0e11, "net_profit": 5.0e10, "gross_margin": 91.9, "roe": 36.0, "eps": 68.6}
    monkeypatch.setattr(tushare.requests, "post", lambda *a, **k: R({"code": 40203, "msg": "抱歉，您没有访问该接口的权限"}))
    assert "权限" in tushare.financials("600519.SS")["error"]
