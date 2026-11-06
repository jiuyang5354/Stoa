"""serve.ui_page：web.html 改动即时生效——不再要求重启应用，开着的窗口刷新就是新代码。"""
import time

from stock_agent import serve


def test_ui_page_picks_up_edits_without_restart(tmp_path, monkeypatch):
    fake = tmp_path / "web.html"
    fake.write_text("v1", encoding="utf-8")
    monkeypatch.setattr(serve, "WEB_HTML", fake)
    serve._UI.update(mtime=None, text="")
    assert serve.ui_page() == "v1"

    time.sleep(0.02)  # mtime 分辨率保险
    fake.write_text("v2", encoding="utf-8")
    assert serve.ui_page() == "v2"  # 改完即新：读盘，不重启


def test_ui_page_survives_missing_file(tmp_path, monkeypatch):
    """文件一瞬间没了（编辑器保存换文件）：沿用缓存，服务不炸。"""
    fake = tmp_path / "web.html"
    fake.write_text("<html>cached</html>", encoding="utf-8")
    monkeypatch.setattr(serve, "WEB_HTML", fake)
    serve._UI.update(mtime=None, text="")
    assert "cached" in serve.ui_page()

    fake.unlink()
    serve._UI["mtime"] = 0  # 强制下一读绕过缓存判断，让 stat/read 的失败分支被走到
    assert "cached" in serve.ui_page()
    assert serve.ui_page() == serve.ui_page()  # 之后稳定走缓存，不再碰那个消失的文件

