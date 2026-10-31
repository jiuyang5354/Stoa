"""Real Docker integration test, enabled explicitly in Linux CI."""
import os

import pytest

from stock_agent import sandbox

pytestmark = pytest.mark.skipif(os.getenv("STOA_SANDBOX_TEST") != "1", reason="requires sandbox Docker image")


def test_real_container_cannot_read_application_data_or_use_network(tmp_path, monkeypatch):
    monkeypatch.setenv("ALLOW_CODE", "1")
    monkeypatch.setenv("LLM_API_KEY", "synthetic-test-key")
    monkeypatch.setattr(sandbox, "home", lambda: tmp_path)
    (tmp_path / ".env").write_text("synthetic private config", encoding="utf-8")
    ws = tmp_path / "workspace"
    ws.mkdir(mode=0o777)
    ws.chmod(0o777)
    (ws / "escape").symlink_to(tmp_path / ".env")
    (ws / "parent").symlink_to(tmp_path)
    code = f"""
import os, socket
from pathlib import Path
import numpy as np
import pandas as pd
assert 'LLM_API_KEY' not in os.environ
assert not Path({str(tmp_path / '.env')!r}).exists()
assert not Path('/workspace/escape').exists()
assert not Path('/workspace/parent/.env').exists()
assert not Path('/data/.env').exists()
assert not Path('/var/run/docker.sock').exists()
assert os.getuid() != 0
try:
    Path('/forbidden').write_text('no')
except OSError:
    pass
else:
    raise AssertionError('root filesystem writable')
try:
    socket.create_connection(('1.1.1.1', 443), timeout=1)
except OSError:
    pass
else:
    raise AssertionError('network accessible')
Path('result.txt').write_text(str(pd.Series(np.arange(3)).sum()))
print('isolated-ok')
"""
    assert sandbox.run_python(code, timeout=30) == "isolated-ok"
    assert (ws / "result.txt").read_text() == "3"
