"""更新进程的失败路径：结果要写下来，临时目录要清掉，占用目录要说人话。

2026-11-01 真实踩到：在程序目录里开着终端时点更新，`Stoa -> Stoa.previous-*`
重命名被 Windows 拒绝（WinError 5），worker 只回了一句 `[WinError 5] 拒绝访问。`，
界面上没有任何可照做的提示；同时 %TEMP% 里堆了 10 个 stoa-update-* 目录（约 1.5 GB）。
"""
import json
import os
import sys
import time

from stock_agent import updater, update_worker


def test_permission_error_explains_busy_folder():
    msg = update_worker.explain(PermissionError(13, "拒绝访问。", r"D:\zanshi\2\Stoa"))
    assert "占用" in msg and "重试" in msg
    assert update_worker.explain(ValueError("压缩包损坏，已取消更新")) == "压缩包损坏，已取消更新"


def test_early_failure_still_writes_result(tmp_path, monkeypatch):
    """原程序 60 秒内没退出时，worker 过去会直接崩，结果文件留着上一次的旧内容。"""
    workdir = tmp_path / "stoa-update-test"
    workdir.mkdir()
    plan = {"install": str(tmp_path / "Stoa"), "source": str(tmp_path / "src"), "pid": 0,
            "version": "9.9.9", "port": 1, "backup": str(tmp_path / "Stoa.previous-1"),
            "stage": str(tmp_path / "Stoa.next-1"), "workdir": str(workdir),
            "result": str(tmp_path / "Stoa.update-result.json")}
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    def boom(pid):
        raise TimeoutError("原程序未退出，取消更新")

    monkeypatch.setattr(update_worker, "wait_parent", boom)
    monkeypatch.setattr(sys, "argv", ["update_worker.py", str(plan_path)])
    update_worker.main()

    saved = json.loads((tmp_path / "Stoa.update-result.json").read_text(encoding="utf-8"))
    assert saved["ok"] is False and saved["rollback"] is False
    assert "未退出" in saved["error"]
    assert saved["time"] <= time.time()
    assert not workdir.exists()          # 临时目录当场清掉


def test_cleanup_workdir_ignores_other_dirs(tmp_path):
    keep = tmp_path / "unrelated"
    keep.mkdir()
    update_worker.cleanup_workdir(str(keep))
    assert keep.exists()
    update_worker.cleanup_workdir(str(tmp_path / "does-not-exist"))


def test_clean_stale_only_removes_old_update_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(updater.tempfile, "gettempdir", lambda: str(tmp_path))
    old = tmp_path / "stoa-update-old"
    new = tmp_path / "stoa-update-new"
    other = tmp_path / "something-else"
    for d in (old, new, other):
        d.mkdir()
    past = time.time() - 7 * 3600
    os.utime(old, (past, past))
    os.utime(other, (past, past))

    assert updater.clean_stale() == 1
    assert not old.exists() and new.exists() and other.exists()   # 只删够旧的那份
