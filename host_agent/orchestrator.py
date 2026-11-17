"""WP-A3 宿主侧请求生命周期编排。

在 WP-A1 的 LifecycleManager 原语之上驱动五阶段流程：
    device.task.submit -> host.task.accepted -> host.task.completed
    -> host.result.deliver -> device.result.applied

安全与证据规则：
- 同一时刻仅一个活动请求；忙碌期新请求返回 busy，不排队（开发手册 3.3）。
- 能力调用交给 CapabilityExecutor；返回 None 表示结果迟到，置
  timeout_result_unknown；默认执行器返回 unsupported_capability。
- 宿主已完成但设备离线：置 completed_delivery_unknown，不伪造 applied。
- 迟到结果只记录 expired，不推进状态（6.6）。
- accepted/completed 是宿主事实，持久化后不因设备重启倒退。
- 宿主事件携带所关联请求的 device boot_id，避免与生命周期 boot 校验冲突。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .agent import HostReceiver, InboundRecord
from .capabilities.base import CapabilityExecutor, CapabilityResult, UnsupportedExecutor, is_whitelisted_action
from .protocol import envelope, spec
from .protocol.lifecycle import LifecycleDecision, LifecycleManager
from .transport.base import Transport

COMPLETE_TIMEOUT_MS = 5000
DELIVER_TIMEOUT_MS = 1000


@dataclass
class EventRecord:
    event_id: str
    kind: str
    revision: int
    outcome: str
    reason: str
    request_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id, "kind": self.kind, "revision": self.revision,
            "outcome": self.outcome, "reason": self.reason, "request_id": self.request_id,
        }


class HostOrchestrator:
    def __init__(
        self,
        transport: Transport,
        receiver: HostReceiver | None = None,
        lifecycle: LifecycleManager | None = None,
        executor: CapabilityExecutor | None = None,
    ) -> None:
        self.transport = transport
        self.receiver = receiver or HostReceiver(transport, lifecycle)
        self.lifecycle = self.receiver.lifecycle
        self.executor = executor or UnsupportedExecutor()
        self.events: list[EventRecord] = []
        self.host_messages: list[dict[str, Any]] = []   # 所有宿主产出的信封（含内部事件），供审计
        self._busy_request_id: str | None = None
        self._host_seq = 0
        self._event_seq = 0
        self.last_error: str = ""
        # hold_complete=True：提交后只发 accepted，能力完成由 release_completion() 显式触发。
        # 用于回放"宿主接受后、完成前设备离线"的时序（offline 剧本）。
        self.hold_complete = False
        self._held: dict[str, tuple[str, str, dict[str, Any]]] = {}
        self.receiver.on_record(self._on_inbound_record)
        self.receiver.on_accept(self._on_accepted)

    @property
    def busy(self) -> bool:
        return self._busy_request_id is not None

    def release_completion(self) -> list[EventRecord]:
        """释放被 hold_complete 暂停的能力完成，返回新增事件。"""
        before = len(self.events)
        held = list(self._held.items())
        self._held.clear()
        for rid, (capability, action, payload) in held:
            self._complete_one(rid, capability, action, payload)
        return self.events[before:]

    def pump(self) -> list[EventRecord]:
        before = len(self.events)
        self.receiver.pump()
        return self.events[before:]

    def event_log(self) -> list[dict[str, Any]]:
        return [e.as_dict() for e in self.events]

    # ---- 持久化：宿主事实不因重启倒退 ----
    def export_state(self) -> dict[str, Any]:
        state = self.lifecycle.export_state()
        state["orchestrator"] = {
            "schema_version": 1,
            "busy_request_id": self._busy_request_id,
            "host_seq": self._host_seq,
            "event_seq": self._event_seq,
            "events": [e.as_dict() for e in self.events],
        }
        return state

    def import_state(self, state: dict[str, Any]) -> None:
        self.lifecycle.import_state(state)
        o = state.get("orchestrator", {})
        self._busy_request_id = o.get("busy_request_id")
        self._host_seq = o.get("host_seq", 0)
        self._event_seq = o.get("event_seq", 0)
        self.events = [EventRecord(**e) for e in o.get("events", [])]

    def save_state(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.export_state(), ensure_ascii=False, indent=2), encoding="utf-8")

    def load_state(self, path: str | Path) -> None:
        self.import_state(json.loads(Path(path).read_text(encoding="utf-8")))

    # ---- 入站记录与分派 ----
    @staticmethod
    def _evidence_outcome(action: str) -> str:
        """把 lifecycle action 映射为证据文件使用的 outcome 词表。"""
        return {"accept": "accepted", "reject": "rejected"}.get(action, action)

    def _on_inbound_record(self, rec: InboundRecord) -> None:
        outcome = self._evidence_outcome(rec.outcome)
        if rec.message is None:
            self.events.append(EventRecord("", "", 0, outcome, rec.reason))
            return
        m = rec.message
        # reason 统一为证据口径：submit->submitted，applied->result_applied
        reason = rec.reason
        if m["kind"] == "device.task.submit" and reason == "device.task.submit":
            reason = "submitted"
        elif m["kind"] == "device.result.applied" and reason == "device.result.applied":
            reason = "result_applied"
        self.events.append(EventRecord(
            m["event_id"], m["kind"], m["revision"], outcome, reason, m["request_id"],
        ))

    def _on_accepted(self, msg: dict[str, Any], decision: LifecycleDecision) -> None:
        if msg["kind"] == "device.task.submit":
            self._handle_submit(msg)
        elif msg["kind"] == "device.result.applied":
            self._handle_applied(msg)

    def _handle_submit(self, msg: dict[str, Any]) -> None:
        rid = msg["request_id"]
        if self._busy_request_id is not None:
            self.last_error = "busy"
            self.events.append(EventRecord(
                msg["event_id"], "host.task.rejected", msg["revision"], "rejected", "busy", rid,
            ))
            return
        capability = msg["capability"]
        action = msg.get("payload", {}).get("action", "")
        if not is_whitelisted_action(capability, action):
            self.events.append(EventRecord(
                msg["event_id"], "host.task.rejected", msg["revision"], "rejected", spec.REASON_BAD_ACTION, rid,
            ))
            return

        self._busy_request_id = rid
        accept = self._emit_host(capability, rid, "host.task.accepted",
                                 {"note": "accepted"}, "task_accepted")
        if accept is None:
            self._busy_request_id = None
            return

        if self.hold_complete:
            self._held[rid] = (capability, action, dict(msg["payload"]))
            return
        self._complete_one(rid, capability, action, dict(msg["payload"]))

    def _complete_one(self, rid: str, capability: str, action: str, payload: dict[str, Any]) -> None:
        result: CapabilityResult | None = self.executor.execute(capability, action, payload)
        if result is None:
            # 结果迟到：置不确定终态，记录 expired，不推进为失败/成功
            self.lifecycle.expire(rid, "timeout_result_unknown")
            self.events.append(EventRecord("", "host.task.completed", 3, "rejected", "expired", rid))
            self._busy_request_id = None
            return

        completed = self._emit_host(capability, rid, "host.task.completed",
                                    result.to_payload(), "task_completed", result.result_source)
        if completed is None:
            self._busy_request_id = None
            return

        if not self.transport.connected:
            # 宿主完成但设备离线：送达未知，不伪造 applied（COM-04）
            self.events.append(EventRecord("", "host.result.deliver", 4,
                                           "rejected", "device_offline_delivery_unknown", rid))
            self.lifecycle.expire(rid, "completed_delivery_unknown")
            self._busy_request_id = None
            return

        self._emit_host(capability, rid, "host.result.deliver",
                        result.to_payload(), "result_delivered", result.result_source,
                        to_device=True)

    def _handle_applied(self, msg: dict[str, Any]) -> None:
        rid = msg["request_id"]
        if self._busy_request_id == rid:
            self._busy_request_id = None

    # ---- 宿主消息生成 ----
    def _emit_host(self, capability: str, rid: str, kind: str, payload: dict[str, Any],
                   reason: str, result_source: str = "unknown",
                   to_device: bool = False) -> dict[str, Any] | None:
        rec = self.lifecycle.get(rid)
        if rec is None:
            self.last_error = "unknown_request"
            return None
        self._event_seq += 1
        self._host_seq += 1
        msg = {
            "protocol_version": spec.PROTOCOL_VERSION,
            "project_id": spec.PROJECT_ID,
            "device_id": "host-agent",
            "boot_id": rec.boot_id,          # 关联请求的设备启动会话，避免 boot_mismatch
            "event_id": f"evt-host-{self._host_seq:05d}",
            "request_id": rid,
            "revision": self._event_seq,
            "kind": kind,
            "capability": capability,
            "payload": payload,
            "result_source": result_source,
            "sent_at": None,
        }
        result = envelope.validate(msg)
        if not result.ok:
            self.last_error = f"host emit rejected: {result.reason} {result.detail}"
            return None
        decision = self.lifecycle.process(msg)
        if decision.action != "accept":
            self.last_error = f"host emit lifecycle: {decision.reason}"
            return None
        # host.task.accepted / host.task.completed 是宿主内部事件（事实），
        # 只有 host.result.deliver 才需要设备在线并实际送达。
        if to_device:
            if not self.transport.connected:
                return None
            self.transport.write(envelope.encode(msg))
        self.host_messages.append(dict(msg))
        self.events.append(EventRecord(
            msg["event_id"], kind, msg["revision"], "accepted", reason, rid,
        ))
        return msg
