"""v0.24：国内行情源优先（腾讯证券）、对话随时停止。"""
import threading

from stock_agent import data


def test_tencent_codes_and_default_order(monkeypatch):
    monkeypatch.delenv("DATA_PROVIDERS", raising=False)
    assert data.provider_order()[0] == "tencent" and data.provider_order()[-1] == "yahoo"
    assert data._tx_code("600519.SS") == ["sh600519"] and data._tx_code("399006.SZ") == ["sz399006"]
    assert data._tx_code("0700.HK") == ["hk00700"] and data._tx_code("HSTECH.HK") == ["hkHSTECH"]
    assert data._tx_code("^GSPC") == ["usINX"] and data._tx_code("^HSI") == ["hkHSI"]
    data._TX_US["BRK.B"] = "usBRK.B.N"
    assert data._tx_code("BRK-B") == ["usBRK.B.N"]


def test_tencent_parse(monkeypatch):
    js = {"code": 0, "data": {"sh600519": {"qfqday": [["2026-10-28", "1400", "1410.5", "1420", "1390", "1"],
                                                       ["2026-10-29", "1410", "1392.05", "1415", "1388", "1"]]}}}
    monkeypatch.setattr(data, "_tx_json", lambda url, tries=3: js)
    ser = data.p_tencent("600519.SS", 30)
    assert list(ser.round(2)) == [1410.5, 1392.05] and str(ser.index[-1].date()) == "2026-10-29"


def test_stop_mid_turn_keeps_context_valid():
    from stock_agent import evals as ev
    from stock_agent.agent import Cancelled
    from stock_agent.memory import Memory
    a = ev.make_agent(ev.load_cases()["fixtures"], Memory(), ev.MockLLM(), [])
    stop = threading.Event()
    a._stop = stop
    real = a._tool

    def tool(name, args):
        stop.set()  # 第一个工具跑着时用户点了停止
        return real(name, args)
    a._tool = tool
    n0 = len(a.messages)
    assert a.ask("腾讯现在多少钱？") == "已停止。"
    assert a.last_meta["stopped"] and [m["role"] for m in a.messages[n0:]] == ["user", "assistant"]
    assert not any(m.get("tool_calls") or m["role"] == "tool" for m in a.messages)
    a._stop, a._tool = threading.Event(), real
    assert "已停止" not in a.ask("腾讯现在多少钱？")  # 停止后下一问照常
    assert issubclass(Cancelled, Exception)


def test_live_quotes_parse(monkeypatch):
    body = ('var hq_str_sh000001="上证指数,3605.6,3651.4,3599.0166,3660.2,3595.2,0,0,1,2' + ',0' * 20 + ',2026-10-30,15:00:00,00";\n'
            'var hq_str_rt_hkHSI="HSI,恒生指数,23011.29,22958.65,23400.02,22947.96,23294.420,335.77,1.46,0,0,1,2,0,0,0,0,2026/10/30,16:08:32";\n')

    class R:
        text, encoding = body, None
    monkeypatch.setattr(data.requests, "get", lambda *a, **k: R())
    out = data.live_quotes(["000001.SS", "^HSI", "AAPL"])
    assert out == {"000001.SS": (3599.0166, "2026-10-30"), "^HSI": (23294.42, "2026-10-30")}


def test_indices_overlay_live(monkeypatch, tmp_path):
    import pandas as pd
    from datetime import datetime
    from stock_agent import serve
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    ser = pd.Series([100.0, 101.0], index=pd.to_datetime(["2026-10-28", "2026-10-29"]))
    monkeypatch.setattr(data, "fetch_closes", lambda syms, period="1y": {s: ser for s in syms})
    monkeypatch.setattr(data, "live_quotes", lambda syms: {"000001.SS": (103.0, "2026-10-30")})
    real_tn = serve.trading_now
    monkeypatch.setattr(serve, "trading_now", lambda t=None: True)
    serve._IDX.clear(); serve._IDX.update(ts=0, data=None)
    sh = next(x for x in serve.indices() if x["symbol"] == "000001.SS")
    assert sh["last"] == 103.0 and sh["dates"][-1] == "2026-10-30" and sh["pct"] == round((103 / 101 - 1) * 100, 2)
    assert real_tn(datetime(2026, 10, 30, 10, 0)) and not real_tn(datetime(2026, 10, 31, 10, 0)) and not real_tn(datetime(2026, 10, 30, 17, 0))
