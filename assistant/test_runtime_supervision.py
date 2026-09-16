# -*- coding: utf-8 -*-
"""桌面状态心跳、离线识别与监护器握手的离线回归测试。"""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import runtime_state
from process_utils import pid_running

HERE = Path(__file__).resolve().parent


def load_pyw(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


widget_v3 = load_pyw("widget_v3_test", "desktop_widget_v3.pyw")
supervisor = load_pyw("supervisor_test", "saylo_supervisor.pyw")


class RuntimeSupervisionTests(unittest.TestCase):
    def test_windows_liveness_probe_is_read_only_for_calling_process(self):
        self.assertTrue(pid_running(os.getpid()))

    def test_heartbeat_advances_without_main_loop_state_change(self):
        with tempfile.TemporaryDirectory() as folder:
            status = Path(folder) / "status.json"
            control = Path(folder) / "control.json"
            with (patch.object(runtime_state, "STATUS_FILE", status),
                  patch.object(runtime_state, "CONTROL_FILE", control),
                  patch.object(runtime_state, "HEARTBEAT_INTERVAL_SEC", 0.02)):
                bridge = runtime_state.RuntimeBridge()
                bridge.set("idle", label="ready")
                first = json.loads(status.read_text(encoding="utf-8"))["heartbeat_at"]
                bridge.start_heartbeat()
                time.sleep(0.07)
                bridge.close()
                second = json.loads(status.read_text(encoding="utf-8"))["heartbeat_at"]
                self.assertGreater(second, first)

    def test_disconnected_status_cannot_look_ready(self):
        with tempfile.TemporaryDirectory() as folder:
            status = Path(folder) / "status.json"
            with patch.object(runtime_state, "STATUS_FILE", status):
                runtime_state.mark_disconnected("test failure")
            data = json.loads(status.read_text(encoding="utf-8"))
            self.assertEqual(data["phase"], "error")
            self.assertEqual(data["pid"], 0)
            self.assertEqual(data["label"], "后台已断开")

    def test_sending_preserves_current_emotion_mode(self):
        with tempfile.TemporaryDirectory() as folder:
            status = Path(folder) / "status.json"
            control = Path(folder) / "control.json"
            with (patch.object(runtime_state, "STATUS_FILE", status),
                  patch.object(runtime_state, "CONTROL_FILE", control)):
                bridge = runtime_state.RuntimeBridge()
                bridge.set("thinking", mode=9, label="happy")
                bridge.set("sending", label="sending")
                data = json.loads(status.read_text(encoding="utf-8"))
                self.assertEqual(data["phase"], "sending")
                self.assertEqual(data["mode"], 9)

    def test_widget_rejects_stale_or_missing_process(self):
        now = time.time()
        alive = {"phase": "idle", "pid": os.getpid(), "heartbeat_at": now}
        stale = {"phase": "idle", "pid": os.getpid(),
                 "heartbeat_at": now - widget_v3.STATUS_STALE_SEC - 1}
        missing = {"phase": "idle", "pid": 0, "heartbeat_at": now}
        self.assertEqual(widget_v3.SayloWidgetV3._validated_state(alive)["phase"], "idle")
        self.assertEqual(widget_v3.SayloWidgetV3._validated_state(stale)["label"], "后台已断开")
        self.assertEqual(widget_v3.SayloWidgetV3._validated_state(missing)["label"], "后台已断开")

    def test_supervisor_handshake_requires_current_child_pid(self):
        with tempfile.TemporaryDirectory() as folder:
            status = Path(folder) / "status.json"
            agent_pid = Path(folder) / "saylo.pid"
            started = time.time()
            status.write_text(json.dumps({
                "phase": "idle", "pid": os.getpid(), "heartbeat_at": started,
            }), encoding="utf-8")
            agent_pid.write_text(str(os.getpid()), encoding="utf-8")
            with (patch.object(supervisor, "STATUS_FILE", status),
                  patch.object(supervisor, "AGENT_PID", agent_pid)):
                self.assertTrue(supervisor.status_ready(started))
                agent_pid.write_text("0", encoding="utf-8")
                self.assertFalse(supervisor.status_ready(started))

    def test_key_unlock_retry_uses_capped_exponential_backoff(self):
        self.assertEqual(supervisor.key_retry_delay(1), 5.0)
        self.assertEqual(supervisor.key_retry_delay(2), 10.0)
        self.assertEqual(supervisor.key_retry_delay(3), 20.0)
        self.assertEqual(supervisor.key_retry_delay(20), 60.0)


if __name__ == "__main__":
    unittest.main()
