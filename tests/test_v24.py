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
