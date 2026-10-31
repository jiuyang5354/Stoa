from stock_agent import sandbox, uploads


def test_table_model_path_is_readable_in_sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("ALLOW_CODE", "1")
    item = uploads.save("持仓.csv", "代码,数量\n0700.HK,200\n".encode())
    content, _, _ = uploads.for_model(uploads.mark("统计数量", [item["id"]]))
    relative = f"uploads/{item['id']}/持仓.csv"
    assert f"/workspace/{relative}" in content
    assert (tmp_path / "workspace" / relative).read_bytes() == uploads.file_path(item["id"]).read_bytes()
    assert str(tmp_path / "uploads") not in content


def test_unavailable_staging_does_not_claim_file_is_readable(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_AGENT_HOME", str(tmp_path))
    monkeypatch.setenv("ALLOW_CODE", "1")
    item = uploads.save("a.csv", b"x,y\n1,2\n")
    monkeypatch.setattr(sandbox, "stage_input", lambda *a: None)
    content, _, _ = uploads.for_model(uploads.mark("统计", [item["id"]]))
    assert "无法放入" in content
    assert "pd.read_excel" not in content
