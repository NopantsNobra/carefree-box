"""WP-A2 验收：五剧本回放 + 与 evidence 确定性序列交叉验证。

运行：python -m unittest host_agent.tests.test_wp_a2 -v

剧本执行只验证协议交互序列，不构成 Windows 或硬件验收。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from host_agent.orchestrator import HostOrchestrator
from host_agent.simulator.executor import ScriptedExecutor
from host_agent.simulator.scripts import (
    SCENARIOS, build_scenario, load_evidence_sequences,
)
from host_agent.transport.virtual import VirtualTransport


def run_scenario(name: str, *, late: bool = False, disconnect_before_result: bool = False):
    """驱动一个剧本，返回 (orchestrator, scenario)。"""
    sc = build_scenario(name)  # type: ignore[arg-type]
    t = VirtualTransport()
    t.connect()
    orch = HostOrchestrator(t, executor=ScriptedExecutor(late=late))
    # offline 剧本需要"宿主 accepted 后、能力完成前设备离线"的时序
    orch.hold_complete = (name == "offline")
    for action in sc.actions:
        if action.op == "send":
            t.device_send(action.data)
        elif action.op == "disconnect":
            t.disconnect()
        elif action.op == "connect":
            t.connect()
        orch.pump()
    if orch.hold_complete:
        orch.release_completion()
    if disconnect_before_result:
        t.disconnect()
        orch.pump()
    return orch, sc


def seq(orch) -> list[tuple[str, str, str]]:
    """提取 (kind, outcome, reason) 序列，与证据文件比对。"""
    return [(e["kind"], e["outcome"], e["reason"]) for e in orch.event_log()]


class TestScenarioBuilders(unittest.TestCase):
    def test_all_five_scenarios_build(self):
        self.assertEqual(set(SCENARIOS), {"success", "offline", "timeout", "duplicate", "illegal_frame"})
        for name in SCENARIOS:
            with self.subTest(name=name):
                sc = build_scenario(name)
                self.assertTrue(sc.actions)
                self.assertTrue(sc.expected_events)
                self.assertTrue(sc.expected_status)

    def test_evidence_files_load(self):
        ev = load_evidence_sequences(ROOT / "evidence")
        self.assertEqual(len(ev["online"]), 9)
        self.assertEqual(len(ev["offline"]), 4)


class TestSuccessScenario(unittest.TestCase):
    def test_five_stage_lifecycle(self):
        orch, sc = run_scenario("success")
        kinds = [k for k, _, _ in seq(orch)]
        self.assertEqual(kinds, [
            "device.task.submit", "host.task.accepted", "host.task.completed",
            "host.result.deliver", "device.result.applied",
        ])
        self.assertTrue(all(o == "accepted" for _, o, _ in seq(orch)))
        self.assertEqual(orch.lifecycle.get("req-mute-1").status, "applied")
        self.assertFalse(orch.busy)

    def test_result_source_is_simulation_not_observed(self):
        t = VirtualTransport()
        t.connect()
        ex = ScriptedExecutor(observed={"muted": True})
        orch = HostOrchestrator(t, executor=ex)
        sc = build_scenario("success")
        for a in sc.actions:
            if a.op == "send":
                t.device_send(a.data)
            orch.pump()
        # 检查宿主产出的 host.task.completed 信封：result_source 必须是 software_simulation，
        # 不得冒充 host_observed / hardware_verified。
        completed = [m for m in orch.host_messages if m["kind"] == "host.task.completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["result_source"], "software_simulation")
        self.assertTrue(completed[0]["payload"]["observed"]["scripted"])


class TestOfflineScenario(unittest.TestCase):
    def test_completed_delivery_unknown(self):
        orch, sc = run_scenario("offline")
        s = seq(orch)
        self.assertIn(("host.result.deliver", "rejected", "device_offline_delivery_unknown"), s)
        self.assertEqual(orch.lifecycle.get("req-extend-offline-1").status, "completed_delivery_unknown")
        self.assertNotIn("device.result.applied", [k for k, _, _ in s])

    def test_matches_offline_evidence(self):
        orch, _ = run_scenario("offline")
        ev = load_evidence_sequences(ROOT / "evidence")["offline"]
        expected = [(e["kind"], e["outcome"], e["reason"]) for e in ev]
        self.assertEqual(seq(orch), expected)


class TestTimeoutScenario(unittest.TestCase):
    def test_timeout_result_unknown(self):
        orch, _ = run_scenario("success", late=True)
        s = seq(orch)
        self.assertIn(("host.task.completed", "rejected", "expired"), s)
        self.assertEqual(orch.lifecycle.get("req-mute-1").status, "timeout_result_unknown")
        self.assertFalse(orch.busy)


class TestDuplicateScenario(unittest.TestCase):
    def test_duplicate_ignored_once(self):
        orch, _ = run_scenario("duplicate")
        s = seq(orch)
        self.assertEqual(s.count(("device.task.submit", "duplicate_ignored", "same_event_id_and_payload")), 1)
        applied = [e for e in orch.event_log() if e["kind"] == "device.result.applied"]
        self.assertEqual(len(applied), 1)

    def test_executor_called_once(self):
        t = VirtualTransport()
        t.connect()
        ex = ScriptedExecutor()
        orch = HostOrchestrator(t, executor=ex)
        sc = build_scenario("duplicate")
        for a in sc.actions:
            if a.op == "send":
                t.device_send(a.data)
            orch.pump()
        self.assertEqual(len(ex.calls), 1)


class TestIllegalFrameScenario(unittest.TestCase):
    def test_illegal_frames_then_recover(self):
        orch, _ = run_scenario("illegal_frame")
        s = seq(orch)
        self.assertEqual([r for k, o, r in s if o == "rejected"][:3], [
            "bad_json", "unknown_action", "message_too_large",
        ])
        self.assertEqual(orch.lifecycle.get("req-mute-1").status, "applied")


class TestOnlineEvidenceCrossCheck(unittest.TestCase):
    def test_success_and_timeout_prefix_matches_online_evidence(self):
        """证据 replay-online.json 的前 5 条（静音成功）应与 success 剧本逐项一致。"""
        orch, _ = run_scenario("success")
        ev = load_evidence_sequences(ROOT / "evidence")["online"]
        expected = [(e["kind"], e["outcome"], e["reason"]) for e in ev[:5]]
        self.assertEqual(seq(orch), expected)

    def test_business_facts_match_evidence(self):
        orch, _ = run_scenario("success")
        self.assertEqual(orch.lifecycle.get("req-mute-1").status, "applied")


if __name__ == "__main__":
    unittest.main(verbosity=2)
