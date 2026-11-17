"""WP-A1 宿主接收入口：把传输字节流变成可执行消息与拒绝记录。

职责边界：
- 只做接收、分帧、校验、生命周期判定、日志；
- 不执行任何 Windows 能力，不生成业务结果（由 WP-A3/A4/A5/A6 实现）；
- 不宣称任何操作执行成功。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .protocol import envelope, spec
from .protocol.lifecycle import LifecycleDecision, LifecycleManager
from .transport.base import FrameEvent, LineFramer, Transport


@dataclass
class InboundRecord:
    raw: bytes
    outcome: str                 # accept | duplicate_ignored | identity_conflict | reject
    reason: str = ""
    message: dict[str, Any] | None = None
    bad_fields: list[str] = field(default_factory=list)


class HostReceiver:
    def __init__(self, transport: Transport, lifecycle: LifecycleManager | None = None) -> None:
        self.transport = transport
        self.lifecycle = lifecycle or LifecycleManager()
        self.framer = LineFramer()
        self.inbound: list[InboundRecord] = []
        self._on_accept: list[Callable[[dict[str, Any], LifecycleDecision], None]] = []
        self._on_record: list[Callable[[InboundRecord], None]] = []

    def on_accept(self, callback: Callable[[dict[str, Any], LifecycleDecision], None]) -> None:
        """注册已接收消息的处理器（能力层后续接入点）。"""
        self._on_accept.append(callback)

    def on_record(self, callback: Callable[[InboundRecord], None]) -> None:
        """注册全部入站记录的观察者（含拒绝/重复/冲突），供编排器记录事件序列。"""
        self._on_record.append(callback)

    def reset_framer(self) -> None:
        """重连后调用：丢弃残帧，防止跨会话粘连。"""
        self.framer.reset()

    def pump(self) -> list[InboundRecord]:
        """从传输读取可用字节并处理，返回本次新增的入站记录。"""
        chunk = self.transport.read()
        if not chunk:
            return []
        frame_events = self.framer.feed(chunk)
        new: list[InboundRecord] = []
        for ev in frame_events:
            rec, deferred_accept = self._handle_frame(ev)
            if rec is not None:
                self.inbound.append(rec)
                new.append(rec)
                # 先记录入站事件（保证 submit 在 host 事件之前），再触发编排
                for cb in self._on_record:
                    cb(rec)
                if deferred_accept is not None:
                    msg, decision = deferred_accept
                    for cb in self._on_accept:
                        cb(msg, decision)
        return new

    def _handle_frame(self, ev: FrameEvent) -> tuple[InboundRecord | None, tuple | None]:
        """返回 (入站记录, 延迟触发的 accept 回调参数)。

        延迟 accept 回调是为了让编排器产生的 host 事件排在该入站记录之后。
        """
        if ev.kind == "line":
            result = envelope.decode_line(ev.data)
            if not result.ok:
                return InboundRecord(ev.data, "reject", result.reason or "invalid",
                                     bad_fields=result.bad_fields), None
            msg = result.message
            assert msg is not None
            decision = self.lifecycle.process(msg)
            deferred = (msg, decision) if decision.action == "accept" else None
            return InboundRecord(ev.data, decision.action, decision.reason, msg), deferred
        if ev.kind == "error":
            return InboundRecord(ev.data, "reject", ev.reason), None
        if ev.kind == "dropped":
            # 超长帧被丢弃：记录为拒绝事件，连接保持可恢复（COM-06）
            return InboundRecord(ev.data, "reject", spec.REASON_TOO_LARGE), None
        return None, None

    def rejected_reasons(self) -> list[str]:
        return [r.reason for r in self.inbound if r.outcome == "reject"]

    def accepted_messages(self) -> list[dict[str, Any]]:
        return [r.message for r in self.inbound
                if r.outcome == "accept" and r.message is not None]

    def export_log(self) -> list[dict[str, Any]]:
        return [{
            "outcome": r.outcome,
            "reason": r.reason,
            "bad_fields": r.bad_fields,
            "kind": r.message.get("kind") if r.message else None,
            "request_id": r.message.get("request_id") if r.message else None,
            "event_id": r.message.get("event_id") if r.message else None,
            "raw_len": len(r.raw),
        } for r in self.inbound]
