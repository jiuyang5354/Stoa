"""desktop 单实例守护：残留进程留下的互斥锁必须能被新实例接管，而不是弹框即死。"""
import sys

import pytest

from stock_agent.desktop import _pid_alive, _read_pid, singleton


def test_read_pid_tolerates_everything(tmp_path):
    assert _read_pid(tmp_path) == 0                       # 文件不存在
    (tmp_path / "desktop.pid").write_text("   \n", encoding="utf-8")
    assert _read_pid(tmp_path) == 0                       # 空白
    (tmp_path / "desktop.pid").write_text("坏数据", encoding="utf-8")
    assert _read_pid(tmp_path) == 0                       # 非 int
    (tmp_path / "desktop.pid").write_text("1234\n", encoding="utf-8")
    assert _read_pid(tmp_path) == 1234                    # 正常


def test_pid_alive_rejects_fakes(tmp_path):
    assert _pid_alive(0) is False                          # 无 pid
    assert _pid_alive(-1) is False                         # 非法
    if sys.platform == "win32":
        assert _pid_alive(4_000_000_000) is False          # 不存在的超大 pid
    else:
        import os
        assert _pid_alive(os.getpid()) is True             # /proc 上自己活着
        assert _pid_alive(999999) is False


def test_singleton_is_noop_outside_windows(tmp_path, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("非 win32 分支测试")
    monkeypatch.setattr(sys, "platform", "linux")
    assert singleton(tmp_path) is None                      # 非桌面平台早退，不建文件不退出
