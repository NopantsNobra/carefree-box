"""请求生命周期、幂等与修订管理（开发手册 6.5 / 6.6 / 6.7）。

规则：
- 去重键 = device_id + boot_id + event_id。
- 相同身份 + 相同载荷 -> duplicate_ignored，不重复执行。
- 相同身份 + 不同载荷 -> identity_conflict，不得覆盖原事件。
- 同一 request_id 全生命周期保持不变，revision 单调递增；重复消息不伪造新进展。
- 设备重启（新 boot_id）后，旧 boot_id 的送达/应用默认拒绝。
- accepted/completed 是宿主事实，持久化后不被设备重启倒退。

本模块只管状态机与判定，不发出任何 Windows 动作，也不做仿真结果。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from . import spec


def payload_hash(payload: dict[str, Any]) -> str:
    """对 payload 做稳定哈希，用于 identity_conflict 判定（键排序后序列化）。"""
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def dedup_key(message: dict[str, Any]) -> tuple[str, str, str]:
    return (message["device_id"], message["boot_id"], message["event_id"])


@dataclass
class RequestRecord:
    request_id: str
    boot_id: str
    capability: str
    submitted_hash: str | None = None
    result_hash: str | None = None
    submitted: bool = False
    accepted: bool = False
    completed: bool = False
    delivered: bool = False
    applied: bool = False
    revision: int = 0
    status: str = "created"       # 终态异常：timeout_result_unknown / completed_delivery_unknown / rejected
    expires_at: str | None = None
    rejections: list[dict[str, str]] = field(default_factory=list)

    def snapshot(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "boot_id": self.boot_id,
            "capability": self.capability,
            "submitted_hash": self.submitted_hash,
            "result_hash": self.result_hash,
            "submitted": self.submitted,
            "accepted": self.accepted,
            "completed": self.completed,
            "delivered": self.delivered,
            "applied": self.applied,
            "revision": self.revision,
            "status": self.status,
            "expires_at": self.expires_at,
            "rejections": list(self.rejections),
        }


@dataclass
class LifecycleDecision:
    action: str       # "accept" | "duplicate_ignored" | "identity_conflict" | "reject"
    reason: str = ""
    request: RequestRecord | None = None


class LifecycleManager:
    """宿主侧请求事实管理。持久化通过 export_state/import_state 显式完成。"""

    def __init__(self) -> None:
        self._requests: dict[str, RequestRecord] = {}
        self._events: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._current_boot_id: str | None = None
        self._retired_boot_ids: set[str] = set()

    @property
    def current_boot_id(self) -> str | None:
        return self._current_boot_id

    def observe_boot(self, boot_id: str) -> None:
        """观察设备新启动会话。旧 boot_id  retired，但宿主事实不删除。"""
        if self._current_boot_id is not None and self._current_boot_id != boot_id:
            self._retired_boot_ids.add(self._current_boot_id)
        if boot_id in self._retired_boot_ids:
            self._retired_boot_ids.discard(boot_id)   # 同一会话重新观察，恢复为当前
        self._current_boot_id = boot_id

    def process(self, message: dict[str, Any]) -> LifecycleDecision:
        """对一条已通过 envelope 校验的消息做生命周期判定。

        只做判定与记录，不执行任何能力，不产生业务结果。
        """
        key = dedup_key(message)
        kind = message["kind"]
        boot_id = message["boot_id"]

        # 旧启动会话的迟到结果默认拒绝
        if self._current_boot_id is not None and boot_id in self._retired_boot_ids:
            rec = self._requests.get(message["request_id"])
            if rec is not None:
                rec.rejections.append({"event_id": message["event_id"], "reason": "stale_boot"})
            return LifecycleDecision("reject", "stale_boot", rec)

        # 事件级幂等
        existing = self._events.get(key)
        if existing is not None:
            if payload_hash(existing["payload"]) == payload_hash(message["payload"]):
                return LifecycleDecision("duplicate_ignored", "same_event_id_and_payload",
                                         self._requests.get(message["request_id"]))
            rec = self._requests.get(message["request_id"])
            if rec is not None:
                rec.rejections.append({"event_id": message["event_id"], "reason": "identity_conflict"})
            return LifecycleDecision("identity_conflict", "identity_conflict", rec)

        # 记录事件（accepted/completed 为宿主事实，先记再判）
        self._events[key] = {"kind": kind, "payload": dict(message["payload"])}

        request_id = message["request_id"]
        rec = self._requests.get(request_id)

        # 结果类消息必须关联已存在的请求
        if kind in ("host.task.completed", "host.result.deliver", "device.result.applied") and rec is None:
            return LifecycleDecision("reject", "unknown_request", None)

        if rec is None:
            rec = RequestRecord(
                request_id=request_id,
                boot_id=boot_id,
                capability=message["capability"],
                expires_at=message.get("expires_at"),
            )
            self._requests[request_id] = rec

        # 设备重启后，旧 boot_id 的送达/应用拒绝（新增倒数第二层防护）
        if rec.boot_id != boot_id and kind in ("host.result.deliver", "device.result.applied"):
            rec.rejections.append({"event_id": message["event_id"], "reason": "boot_mismatch"})
            return LifecycleDecision("reject", "boot_mismatch", rec)

        # revision 单调：重复消息不伪造新进展
        rev = message["revision"]
        if rev < rec.revision:
            rec.rejections.append({"event_id": message["event_id"], "reason": "stale_revision"})
            return LifecycleDecision("reject", "stale_revision", rec)
        rec.revision = max(rec.revision, rev)

        # 生命周期推进（只在未处于终态时推进）
        if rec.status in ("timeout_result_unknown", "completed_delivery_unknown"):
            rec.rejections.append({"event_id": message["event_id"], "reason": "expired"})
            return LifecycleDecision("reject", "expired", rec)

        if kind == "device.task.submit":
            rec.submitted = True
            rec.submitted_hash = payload_hash(message["payload"])
            rec.status = "submitted"
        elif kind == "host.task.accepted":
            rec.accepted = True
            rec.status = "accepted"
        elif kind == "host.task.completed":
            rec.completed = True
            rec.result_hash = payload_hash(message["payload"])
            rec.status = "completed"
        elif kind == "host.result.deliver":
            rec.delivered = True
            rec.status = "delivered"
        elif kind == "device.result.applied":
            rec.applied = True
            rec.status = "applied"

        return LifecycleDecision("accept", kind, rec)

    def expire(self, request_id: str, reason: str) -> None:
        """把请求置为不确定终态。不把超时改写成失败或成功。"""
        rec = self._requests.get(request_id)
        if rec is None:
            return
        rec.status = reason
        if reason == "timeout_result_unknown":
            rec.rejections.append({"event_id": "-", "reason": "expired"})

    def get(self, request_id: str) -> RequestRecord | None:
        return self._requests.get(request_id)

    def export_state(self) -> dict[str, Any]:
        """导出可持久化状态（宿主事实），供重启后恢复。"""
        return {
            "schema_version": 1,
            "current_boot_id": self._current_boot_id,
            "retired_boot_ids": sorted(self._retired_boot_ids),
            "requests": {rid: rec.snapshot() for rid, rec in self._requests.items()},
            "events": [
                {"device_id": k[0], "boot_id": k[1], "event_id": k[2], **v}
                for k, v in self._events.items()
            ],
        }

    def import_state(self, state: dict[str, Any]) -> None:
        """从持久化状态恢复。不覆盖已存在的更新事实。"""
        self._current_boot_id = state.get("current_boot_id")
        self._retired_boot_ids = set(state.get("retired_boot_ids", []))
        for rid, snap in state.get("requests", {}).items():
            self._requests[rid] = RequestRecord(
                request_id=rid,
                boot_id=snap["boot_id"],
                capability=snap["capability"],
                submitted_hash=snap.get("submitted_hash"),
                result_hash=snap.get("result_hash"),
                submitted=snap.get("submitted", False),
                accepted=snap.get("accepted", False),
                completed=snap.get("completed", False),
                delivered=snap.get("delivered", False),
                applied=snap.get("applied", False),
                revision=snap.get("revision", 0),
                status=snap.get("status", "created"),
                expires_at=snap.get("expires_at"),
                rejections=list(snap.get("rejections", [])),
            )
        for ev in state.get("events", []):
            key = (ev["device_id"], ev["boot_id"], ev["event_id"])
            self._events[key] = {"kind": ev["kind"], "payload": ev["payload"]}
