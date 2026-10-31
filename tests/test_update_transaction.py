import hashlib
import zipfile
from pathlib import Path

import pytest

from stock_agent import updater


def test_download_requires_matching_sha256(tmp_path):
    package = tmp_path / "package.zip"
    package.write_bytes(b"synthetic package")
    updater.verify_download(package, "sha256:" + hashlib.sha256(package.read_bytes()).hexdigest())
    with pytest.raises(ValueError):
        updater.verify_download(package, "sha256:" + "0" * 64)
    with pytest.raises(ValueError):
        updater.verify_download(package, None)


def test_archive_rejects_path_traversal(tmp_path):
    package = tmp_path / "bad.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("../outside", "bad")
    with pytest.raises(ValueError):
        updater.extract_package(package, tmp_path / "out")
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize("healthy", [True, False])
def test_install_rolls_back_on_failed_start(tmp_path, healthy):
    from stock_agent.update_worker import replace_install
    install, source = tmp_path / "Stoa", tmp_path / "download" / "Stoa"
    install.mkdir(); source.mkdir(parents=True)
    (install / "version").write_text("old")
    (source / "version").write_text("new")
    plan = {"install": str(install), "source": str(source), "backup": str(tmp_path / "Stoa.previous-test"),
            "stage": str(tmp_path / "Stoa.next-test")}
    launches = []
    def launch(path):
        launches.append((path / "version").read_text())
        return None
    result = replace_install(plan, launch, lambda proc: healthy)
    assert result["ok"] is healthy
    assert (install / "version").read_text() == ("new" if healthy else "old")
    assert launches == (["new"] if healthy else ["new", "old"])
    assert result.get("rollback", False) is (not healthy)


def test_failed_copy_leaves_existing_install_untouched(tmp_path, monkeypatch):
    from stock_agent import update_worker
    install = tmp_path / "Stoa"; install.mkdir()
    (install / "version").write_text("old")
    plan = {"install": str(install), "source": str(tmp_path / "missing"),
            "backup": str(tmp_path / "Stoa.previous-test"), "stage": str(tmp_path / "Stoa.next-test")}
    result = update_worker.replace_install(plan, lambda path: pytest.fail("must not launch"), lambda proc: True)
    assert not result["ok"] and not result.get("rollback")
    assert (install / "version").read_text() == "old"


def test_readiness_checks_version_and_process_state():
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from stock_agent.update_worker import wait_ready
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(json.dumps({"version": "test-new"}).encode())
        def log_message(self, *a): pass
    class Process:
        def poll(self): return None
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert wait_ready(Process(), server.server_port, "test-new", timeout=1)
        assert not wait_ready(Process(), server.server_port, "wrong-version", timeout=.15)
    finally:
        server.shutdown(); server.server_close()


def test_update_keeps_installer_files_and_prunes_old_backups(tmp_path):
    from stock_agent.update_worker import replace_install
    install, source = tmp_path / "Stoa", tmp_path / "download" / "Stoa"
    install.mkdir(); source.mkdir(parents=True)
    (install / "Stoa.exe").write_text("old"); (install / "Uninstall.exe").write_text("nsis")
    (source / "Stoa.exe").write_text("new")
    old_backup = tmp_path / "Stoa.previous-1"; old_backup.mkdir(); (old_backup / "Stoa.exe").write_text("older")
    stale_stage = tmp_path / "Stoa.next-1"; stale_stage.mkdir()
    other = tmp_path / "Other.previous-1"; other.mkdir()
    plan = {"install": str(install), "source": str(source), "backup": str(tmp_path / "Stoa.previous-2"),
            "stage": str(tmp_path / "Stoa.next-2")}
    assert replace_install(plan, lambda p: None, lambda proc: True)["ok"]
    assert (install / "Stoa.exe").read_text() == "new"
    assert (install / "Uninstall.exe").read_text() == "nsis"  # NSIS 卸载入口不能丢
    assert (tmp_path / "Stoa.previous-2" / "Stoa.exe").read_text() == "old"
    assert not old_backup.exists() and not stale_stage.exists() and other.exists()
