"""WP-A3 验收：busy 锁、白名单、不支持能力、状态持久化、延迟送达。

运行：python -m unittest host_agent.tests.test_wp_a3 -v

判定纪律：不调用任何 Windows API；剧本执行器结果均为 software_simulation。
"""
from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from host_agent.capabilities.base import UnsupportedExecutor, is_whitelisted_action
from host_agent.orchestrator import HostOrchestrator
from host_agent.protocol import envelope, spec
from host_agent.simulator.executor import ScriptedExecutor
from host_agent.simulator.scripts import make_message
from host_agent.transport.virtual import VirtualTransport


def submit(**overrides) -> bytes:
    return envelope.encode(make_message(**overrides))


def applied(event_id: str, request_id: str) -> bytes:
    return envelope.encode(make_message(
        kind="device.result.applied", event_id=event_id, request_id=request_id,
        revision=5, payload={"status": "success"},
    ))


class TestBusyRule(unittest.TestCase):
    """COM-08：忙碌期间第二请求返回 busy，不排队、不执行。"""

    def test_second_request_rejected_busy(self):
        t = VirtualTransport()
        t.connect()
        orch = HostOrchestrator(t, executor=ScriptedExecutor())
        t.device_send(submit(event_id="evt-1", request_id="req-1"))
        orch.pump()
        self.assertTrue(orch.busy)

        second = submit(event_id="evt-2", request_id="req-2",
                        capability="windows_projection",
                        payload={"action": "set_projection", "mode": "extend"})
        t.device_send(second)
        orch.pump()
        s = orch.event_log()
        self.assertIn("busy", [e["reason"] for e in s])
        self.assertIsNone(orch.lifecycle.get("req-2").result_hash)
        self.assertFalse(orch.lifecycle.get("req-2").accepted)

        # 第一个完成后 busy 解除
        t.device_send(applied("evt-1-applied", "req-1"))
        orch.pump()
        self.assertFalse(orch.busy)

    def test_busy_request_not_queued(self):
        t = VirtualTransport()
        t.connect()
        orch = HostOrchestrator(t, executor=ScriptedExecutor())
        t.device_send(submit(event_id="evt-1", request_id="req-1") + submit(event_id="evt-2", request_id="req-2"))
        orch.pump()
        # 第二个请求不应产生 accepted 事件
        accepted_for_2 = [e for e in orch.event_log()
                          if e["kind"] == "host.task.accepted" and e["request_id"] == "req-2"]
        self.assertEqual(accepted_for_2, [])


class TestCapabilitySafety(unittest.TestCase):
    def test_default_executor_returns_unsupported(self):
        ex = UnsupportedExecutor()
        r = ex.execute("windows_audio", "set_mute", {})
        self.assertEqual(r.status, "unsupported_capability")
        self.assertEqual(r.result_source, "unknown")

    def test_scripted_executor_cannot_claim_host_observed(self):
        with self.assertRaises(ValueError):
            ScriptedExecutor(result_source="host_observed")
        with self.assertRaises(ValueError):
            ScriptedExecutor(result_source="hardware_verified")

    def test_action_whitelist_blocks_arbitrary_action(self):
        self.assertFalse(is_whitelisted_action("windows_audio", "format_disk"))
        self.assertTrue(is_whitelisted_action("windows_audio", "set_mute"))

    def test_unsupported_action_not_executed(self):
        t = VirtualTransport()
        t.connect()
        orch = HostOrchestrator(t, executor=ScriptedExecutor())
        t.device_send(submit(event_id="evt-1", request_id="req-1",
                             payload={"action": "run_shell", "cmd": "rm -rf /"}))
        orch.pump()
        self.assertIn("unknown_action", [e["reason"] for e in orch.event_log()])
        self.assertIsNone(orch.lifecycle.get("req-1"))


class TestPersistence(unittest.TestCase):
    def test_host_facts_survive_orchestrator_restart(self):
        t = VirtualTransport()
        t.connect()
        orch = HostOrchestrator(t, executor=ScriptedExecutor())
        for raw in (submit(event_id="evt-1", request_id="req-1"),
                    applied("evt-1-applied", "req-1")):
            t.device_send(raw)
            orch.pump()
        rec = orch.lifecycle.get("req-1")
        self.assertEqual(rec.status, "applied")

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state.json"
            orch.save_state(p)

            t2 = VirtualTransport()
            t2.connect()
            fresh = HostOrchestrator(t2, executor=ScriptedExecutor())
            fresh.load_state(p)
            self.assertEqual(fresh.lifecycle.get("req-1").status, "applied")
            self.assertEqual(len(fresh.event_log()), len(orch.event_log()))

    def test_old_boot_result_cannot_revive_fact(self):
        t = VirtualTransport()
        t.connect()
        orch = HostOrchestrator(t, executor=ScriptedExecutor())
        t.device_send(submit(event_id="evt-1", request_id="req-1", boot_id="boot-0001"))
        orch.pump()
        orch.lifecycle.observe_boot("boot-0002")
        t.device_send(applied("evt-stale-applied", "req-1"))
        t.device_send(applied("evt-new-applied", "req-1"))
        # 上面两条应用事件的 boot_id 默认是 boot-online-1，与 submit 的 boot-0001 不同
        orch.pump()
        self.assertNotEqual(orch.lifecycle.get("req-1").status, "applied")


class TestReasonDiscipline(unittest.TestCase):
    """不确定终态不得改写为成功/失败。"""

    def test_timeout_is_not_failure(self):
        t = VirtualTransport()
        t.connect()
        orch = HostOrchestrator(t, executor=ScriptedExecutor(late=True))
        t.device_send(submit(event_id="evt-1", request_id="req-1"))
        orch.pump()
        rec = orch.lifecycle.get("req-1")
        self.assertEqual(rec.status, "timeout_result_unknown")
        self.assertFalse(rec.applied)
        self.assertFalse(rec.completed)

    def test_offline_is_not_success(self):
        t = VirtualTransport()
        t.connect()
        orch = HostOrchestrator(t, executor=ScriptedExecutor())
        orch.hold_complete = True
        t.device_send(submit(event_id="evt-1", request_id="req-1"))
        orch.pump()
        t.disconnect()
        orch.release_completion()
        rec = orch.lifecycle.get("req-1")
        self.assertEqual(rec.status, "completed_delivery_unknown")
        self.assertFalse(rec.applied)

    def test_no_fabricated_host_observed_in_events(self):
        t = VirtualTransport()
        t.connect()
        orch = HostOrchestrator(t, executor=ScriptedExecutor())
        t.device_send(submit(event_id="evt-1", request_id="req-1") + applied("evt-1-applied", "req-1"))
        orch.pump()
        # 剧本执行器不允许产生 host_observed 声明；事件 reason 中也不得出现该词
        self.assertNotIn("host_observed", [e["reason"] for e in orch.event_log()])


if __name__ == "__main__":
    unittest.main(verbosity=2)
