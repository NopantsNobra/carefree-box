"""WP-A1 验收测试：COM-06 + 重连快照同步 + 幂等/修订/终态。

运行：python -m unittest discover -s host_agent -p 'test_*.py' -v

判定纪律：本测试只验证协议与传输行为，不验证 Windows 能力执行，
不把模拟结果写成实机通过。
"""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from host_agent.agent import HostReceiver
from host_agent.protocol import envelope, spec
from host_agent.protocol.lifecycle import LifecycleManager, payload_hash
from host_agent.transport.base import LineFramer
from host_agent.transport.virtual import VirtualTransport

from .helpers import make_message


def wire(msg: dict) -> bytes:
    return envelope.encode(msg)


class TestEnvelopeValidation(unittest.TestCase):
    """信封校验：长度/格式/白名单。对应 COM-06 的非法 JSON、超长帧、未知 action。"""

    def test_valid_device_submit_accepted(self):
        r = envelope.decode_line(wire(make_message()).rstrip(b"\n"))
        self.assertTrue(r.ok, r.detail)
        self.assertEqual(r.message["kind"], "device.task.submit")

    def test_bad_json_rejected(self):
        r = envelope.decode_line(b'{not json')
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, spec.REASON_BAD_JSON)

    def test_not_object_rejected(self):
        r = envelope.decode_line(b'[1,2,3]')
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, spec.REASON_NOT_OBJECT)

    def test_oversized_message_rejected(self):
        r = envelope.decode_line(b'x' * (spec.MAX_MESSAGE_BYTES + 1))
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, spec.REASON_TOO_LARGE)

    def test_oversized_payload_rejected(self):
        msg = make_message(payload={"action": "set_mute", "blob": "x" * (spec.MAX_PAYLOAD_BYTES + 1)})
        r = envelope.validate(msg)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, spec.REASON_PAYLOAD_TOO_LARGE)

    def test_unknown_action_rejected(self):
        msg = make_message(payload={"action": "format_disk"})
        r = envelope.validate(msg)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, spec.REASON_BAD_ACTION)
        self.assertIn("payload.action", r.bad_fields)

    def test_unknown_capability_rejected(self):
        r = envelope.validate(make_message(capability="arbitrary_capability"))
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, spec.REASON_BAD_CAPABILITY)

    def test_unknown_kind_rejected(self):
        r = envelope.validate(make_message(kind="device.do_anything"))
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, spec.REASON_BAD_KIND)

    def test_missing_field_rejected(self):
        msg = make_message()
        del msg["event_id"]
        r = envelope.validate(msg)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, spec.REASON_FIELD_MISSING)
        self.assertIn("event_id", r.bad_fields)

    def test_bad_protocol_version_rejected(self):
        r = envelope.validate(make_message(protocol_version="0.9"))
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, spec.REASON_BAD_PROTOCOL_VERSION)

    def test_wrong_project_rejected(self):
        r = envelope.validate(make_message(project_id="other-project"))
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, spec.REASON_BAD_PROJECT)

    def test_legacy_project_readonly(self):
        self.assertTrue(envelope.validate(make_message(project_id="desk-box-pro-v21")).ok)
        r = envelope.validate(make_message(project_id="desk-box-pro-v21", kind="host.result.deliver"))
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, spec.REASON_BAD_PROJECT)

    def test_bad_revision_rejected(self):
        for bad in (-1, True, "1"):
            with self.subTest(bad=bad):
                r = envelope.validate(make_message(revision=bad))
                self.assertFalse(r.ok)
                self.assertEqual(r.reason, spec.REASON_BAD_FIELD_TYPE)

    def test_device_message_requires_uptime_ms(self):
        msg = make_message()
        del msg["uptime_ms"]
        r = envelope.validate(msg)
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, spec.REASON_FIELD_MISSING)
        self.assertIn("uptime_ms", r.bad_fields)


class TestFramerRecovery(unittest.TestCase):
    """错误帧恢复：超长帧丢弃后连接仍可用。对应 COM-06。"""

    def test_framer_splits_lines(self):
        f = LineFramer()
        evs = f.feed(b'{"a":1}\n{"b":2}\n')
        self.assertEqual([e.kind for e in evs], ["line", "line"])
        self.assertEqual(f.total_lines, 2)

    def test_framer_recovers_after_oversized_frame(self):
        f = LineFramer(max_bytes=16)
        good = b'{"ok":1}'
        evs = f.feed(b'x' * 40 + b'\n' + good + b'\n')
        kinds = [e.kind for e in evs]
        self.assertIn("dropped", kinds)
        self.assertIn("line", kinds)
        self.assertEqual(evs[-1].data, good)
        self.assertEqual(f.dropped_frames, 1)

    def test_framer_reset_clears_partial(self):
        f = LineFramer()
        f.feed(b'{"partial":1')
        f.reset()
        evs = f.feed(b'{"full":2}\n')
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].data, b'{"full":2}')


class TestHostReceiverCOM06(unittest.TestCase):
    """COM-06 端到端：非法 JSON/超长帧/未知 action 不执行，连接可恢复。"""

    def setUp(self):
        self.t = VirtualTransport()
        self.t.connect()
        self.r = HostReceiver(self.t)
        self.accepted = []
        self.r.on_accept(lambda msg, dec: self.accepted.append(msg))

    def test_illegal_json_rejected_then_valid_executed(self):
        self.t.device_send(b'{bad json\n')
        self.r.pump()
        self.assertEqual(self.r.rejected_reasons(), [spec.REASON_BAD_JSON])
        self.assertEqual(len(self.accepted), 0)
        self.t.device_send(wire(make_message()))
        self.r.pump()
        self.assertEqual(len(self.accepted), 1)

    def test_unknown_action_rejected_not_executed(self):
        self.t.device_send(wire(make_message(payload={"action": "delete_all"})))
        self.r.pump()
        self.assertEqual(self.r.rejected_reasons(), [spec.REASON_BAD_ACTION])
        self.assertEqual(len(self.accepted), 0)

    def test_oversized_frame_rejected_then_valid(self):
        self.t.device_send(b'x' * (spec.MAX_MESSAGE_BYTES + 32) + b'\n')
        self.r.pump()
        self.assertEqual(len(self.accepted), 0)
        self.t.device_send(wire(make_message()))
        self.r.pump()
        self.assertEqual(len(self.accepted), 1)

    def test_mixed_batch_partial_accept(self):
        good = wire(make_message(event_id="evt-good"))
        bad = wire(make_message(event_id="evt-bad", payload={"action": "nope"}))
        self.t.device_send(bad + good)
        self.r.pump()
        self.assertEqual(len(self.accepted), 1)
        self.assertEqual(self.accepted[0]["event_id"], "evt-good")


class TestLifecycle(unittest.TestCase):
    """幂等、修订、终态、重启会话。对应 6.5/6.6。"""

    def setUp(self):
        self.lm = LifecycleManager()
        self.lm.observe_boot("boot-0001")

    def _seq(self, submission=True):
        # 每条消息必须携带唯一 event_id；去重键 = device_id+boot_id+event_id，
        # 共用同一 event_id 会被正确判为 identity_conflict，不是合法消息流。
        out = []
        if submission:
            out.append(make_message(kind="device.task.submit", revision=1, event_id="evt-r1",
                                    payload={"action": "set_mute", "muted": True}))
        out.append(make_message(kind="host.task.accepted", revision=2, event_id="evt-r2",
                                payload={"note": "accepted"}))
        out.append(make_message(kind="host.task.completed", revision=3, event_id="evt-r3",
                                payload={"status": "success", "observed": {"muted": True}}))
        out.append(make_message(kind="host.result.deliver", revision=4, event_id="evt-r4",
                                payload={"status": "success"}))
        out.append(make_message(kind="device.result.applied", revision=5, event_id="evt-r5",
                                payload={"status": "success"}))
        return out

    def test_full_lifecycle_reaches_applied(self):
        for m in self._seq():
            d = self.lm.process(m)
            self.assertEqual(d.action, "accept", d.reason)
        rec = self.lm.get("req-0001")
        self.assertEqual(rec.status, "applied")
        self.assertTrue(all([rec.submitted, rec.accepted, rec.completed, rec.delivered, rec.applied]))

    def test_duplicate_ignored(self):
        m = make_message()
        self.assertEqual(self.lm.process(m).action, "accept")
        d = self.lm.process(m)
        self.assertEqual(d.action, "duplicate_ignored")
        self.assertEqual(d.reason, "same_event_id_and_payload")

    def test_identity_conflict(self):
        m = make_message()
        self.lm.process(m)
        m2 = copy.deepcopy(m)
        m2["payload"] = {"action": "set_mute", "muted": False}
        d = self.lm.process(m2)
        self.assertEqual(d.action, "identity_conflict")
        self.assertEqual(self.lm.get("req-0001").submitted_hash, payload_hash(m["payload"]))

    def test_stale_revision_rejected(self):
        self.lm.process(make_message(revision=5))
        d = self.lm.process(make_message(event_id="evt-2", revision=3))
        self.assertEqual(d.action, "reject")
        self.assertEqual(d.reason, "stale_revision")

    def test_expired_terminal_rejects_late_result(self):
        self.lm.process(make_message())
        self.lm.expire("req-0001", "timeout_result_unknown")
        late = make_message(kind="host.task.completed", event_id="evt-late", revision=3)
        d = self.lm.process(late)
        self.assertEqual(d.action, "reject")
        self.assertEqual(d.reason, "expired")
        self.assertEqual(self.lm.get("req-0001").status, "timeout_result_unknown")

    def test_stale_boot_rejected_after_reboot(self):
        self.lm.process(make_message(kind="device.task.submit"))
        self.lm.observe_boot("boot-0002")
        d = self.lm.process(make_message(kind="device.result.applied", boot_id="boot-0001",
                                         event_id="evt-old-boot", revision=2))
        self.assertEqual(d.action, "reject")
        self.assertEqual(d.reason, "stale_boot")

    def test_host_facts_survive_reboot(self):
        for m in self._seq()[:3]:
            self.lm.process(m)
        before = self.lm.get("req-0001")
        self.lm.observe_boot("boot-0002")
        after = self.lm.get("req-0001")
        self.assertTrue(after.accepted and after.completed)
        self.assertEqual(after.status, before.status)

    def test_persistence_roundtrip(self):
        for m in self._seq():
            self.lm.process(m)
        saved = self.lm.export_state()
        fresh = LifecycleManager()
        fresh.import_state(saved)
        self.assertEqual(fresh.get("req-0001").status, "applied")
        self.assertEqual(fresh.current_boot_id, "boot-0001")


class TestReconnectSnapshot(unittest.TestCase):
    """重连后快照同步：断线清空残帧，重连后首个快照被处理。"""

    def test_reconnect_receives_snapshot(self):
        t = VirtualTransport()
        t.connect()
        r = HostReceiver(t)
        accepted = []
        r.on_accept(lambda m, d: accepted.append(m))

        t.device_send(b'{"partial":1')
        r.pump()
        t.disconnect()

        t.connect()
        r.reset_framer()
        snap = make_message(kind="device.state.snapshot", event_id="evt-snap",
                            payload={"switches": {"1": "mid", "2": "mid", "3": "mid", "4": "mid", "5": "mid"},
                                     "power": "on"})
        t.device_send(wire(snap))
        got = r.pump()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].outcome, "accept")
        self.assertEqual(accepted[0]["kind"], "device.state.snapshot")

    def test_disconnect_drops_device_bytes(self):
        t = VirtualTransport()
        t.connect()
        t.disconnect()
        # 断线期间设备侧写入不入队（模拟送达失败）
        t.device_send(wire(make_message()))
        self.assertEqual(t.pending_device_bytes(), 0)
        r = HostReceiver(t)
        t.connect()
        self.assertEqual(r.pump(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
