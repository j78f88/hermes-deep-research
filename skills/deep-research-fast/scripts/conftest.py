"""Offline suite: sandbox new worker locks and refuse real network or Hermes launches."""
import socket
import subprocess
from pathlib import Path

import pytest

import fast_worker as fw


@pytest.fixture(autouse=True)
def offline_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(fw, "SLOT_DIR", tmp_path / "host-slots")
    monkeypatch.setattr(fw, "HOST_SLOTS", 24)
    def denied(*args, **kwargs):
        raise AssertionError("Network calls are forbidden in the offline suite")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    original = subprocess.Popen
    def local_only(command, *args, **kwargs):
        executable = command[0] if isinstance(command, (list, tuple)) else command.split()[0]
        if "hermes" in Path(str(executable)).name.lower():
            raise AssertionError("Real Hermes calls are forbidden in the offline suite")
        return original(command, *args, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", local_only)
