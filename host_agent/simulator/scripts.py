"""五类剧本：success / offline / timeout / duplicate / illegal_frame。

每个剧本输出一组虚拟设备动作（字节帧、断线、迟到结果），由回放驱动按序注入。
剧本的事件 ID 与 evidence/replay-online.json、replay-offline.json 对齐，
便于做确定性序列交叉验证（见 tests/test_wp_a2.py）。

这些剧本只描述协议交互，不证明 Windows 能力或真实硬件行为。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..protocol import envelope, spec

ScenarioName = Literal["success", "offline", "timeout", "duplicate", "illegal_frame"]


def make_message(**overrides: Any) -> dict[str, Any]:
    """剧本自用的合法信封构造器，不依赖 tests 包。"""
    msg: dict[str, Any] = {
        "protocol_version": spec.PROTOCOL_VERSION,
        "project_id": spec.PROJECT_ID,
        "device_id": "deskbox-test-01",
        "boot_id": "boot-online-1",
        "event_id": "evt-0001",
        "request_id": "req-0001",
        "revision": 1,
        "kind": "device.task.submit",
        "capability": "windows_audio",
        "payload": {"action": "set_mute", "muted": True},
        "result_source": "unknown",
        "sent_at": None,
        "uptime_ms": 1234,
    }
    msg.update(overrides)
    return msg

SCENARIOS: tuple[ScenarioName, ...] = ("success", "offline", "timeout", "duplicate", "illegal_frame")


@dataclass
class DeviceAction:
    op: str                      # send | disconnect | connect
    data: bytes = b""
    note: str = ""


@dataclass
class Scenario:
    name: str
    actions: list[DeviceAction] = field(default_factory=list)
    # 期望的宿主侧事件序列（event_id, kind, outcome, reason），用于交叉验证
    expected_events: list[tuple[str, str, str, str]] = field(default_factory=list)
    expected_status: str = ""


def _submit(event_id: str, request_id: str, *, capability: str = "windows_audio",
            action: str = "set_mute", payload: dict[str, Any] | None = None,
            boot_id: str = "boot-online-1") -> bytes:
    return envelope.encode(make_message(
        kind="device.task.submit", capability=capability,
        payload=payload if payload is not None else {"action": action, "muted": True},
        event_id=event_id, request_id=request_id, boot_id=boot_id, revision=1,
    ))


def _applied(event_id: str, request_id: str, boot_id: str = "boot-online-1") -> bytes:
    return envelope.encode(make_message(
        kind="device.result.applied", event_id=event_id, request_id=request_id,
        boot_id=boot_id, revision=5, payload={"status": "success"},
    ))


def build_scenario(name: ScenarioName) -> Scenario:
    if name == "success":
        return Scenario(
            name=name,
            actions=[
                DeviceAction("send", _submit("evt-mute-submit-1", "req-mute-1"), "提交静音请求"),
                DeviceAction("send", _applied("evt-mute-applied-1", "req-mute-1"), "设备应用结果"),
            ],
            expected_events=[
                ("evt-mute-submit-1", "device.task.submit", "accept", "device.task.submit"),
                ("evt-mute-accepted-1", "host.task.accepted", "accept", "task_accepted"),
                ("evt-mute-completed-1", "host.task.completed", "accept", "task_completed"),
                ("evt-mute-delivered-1", "host.result.deliver", "accept", "result_delivered"),
                ("evt-mute-applied-1", "device.result.applied", "accept", "result_applied"),
            ],
            expected_status="applied",
        )
    if name == "offline":
        return Scenario(
            name=name,
            actions=[
                DeviceAction("send", _submit("evt-extend-submit-1", "req-extend-offline-1",
                                             capability="windows_projection", action="set_projection",
                                             payload={"action": "set_projection", "mode": "extend"},
                                             boot_id="boot-offline-1"), "提交投影请求"),
                # 宿主先 accepted，此刻设备离线；随后 release_completion() 触发完成与送达判定
                DeviceAction("disconnect", note="宿主接受后、完成前设备离线"),
            ],
            expected_events=[
                ("evt-extend-submit-1", "device.task.submit", "accept", "device.task.submit"),
                ("evt-extend-accepted-1", "host.task.accepted", "accept", "task_accepted"),
                ("evt-extend-completed-1", "host.task.completed", "accept", "task_completed"),
                ("evt-extend-deliver-1", "host.result.deliver", "reject", "device_offline_delivery_unknown"),
            ],
            expected_status="completed_delivery_unknown",
        )
    if name == "timeout":
        return Scenario(
            name=name,
            actions=[
                DeviceAction("send", _submit("evt-restart-submit-1", "req-restart-timeout-1",
                                             capability="windows_restart", action="request_restart",
                                             payload={"action": "request_restart", "mode": "confirmed_countdown"}),
                             "提交重启请求"),
                # 迟到的完成事件由执行器延迟产生（见 test_wp_a3 的 late_executor），
                # 剧本本身不提前发送；这里只保留期望序列用于交叉验证。
            ],
            expected_events=[
                ("evt-restart-submit-1", "device.task.submit", "accept", "device.task.submit"),
                ("evt-restart-accepted-1", "host.task.accepted", "accept", "task_accepted"),
                ("evt-restart-completed-late-1", "host.task.completed", "reject", "expired"),
            ],
            expected_status="timeout_result_unknown",
        )
    if name == "duplicate":
        dup = _submit("evt-mute-submit-1", "req-mute-1")
        return Scenario(
            name=name,
            actions=[
                DeviceAction("send", dup, "首次提交"),
                DeviceAction("send", dup, "同一事件重复提交"),
                DeviceAction("send", _applied("evt-mute-applied-1", "req-mute-1"), "应用结果"),
            ],
            expected_events=[
                ("evt-mute-submit-1", "device.task.submit", "accept", "device.task.submit"),
                ("evt-mute-submit-1", "device.task.submit", "duplicate_ignored", "same_event_id_and_payload"),
                ("evt-mute-accepted-1", "host.task.accepted", "accept", "task_accepted"),
                ("evt-mute-completed-1", "host.task.completed", "accept", "task_completed"),
                ("evt-mute-delivered-1", "host.result.deliver", "accept", "result_delivered"),
                ("evt-mute-applied-1", "device.result.applied", "accept", "result_applied"),
            ],
            expected_status="applied",
        )
    if name == "illegal_frame":
        return Scenario(
            name=name,
            actions=[
                DeviceAction("send", b"{not json\n", "非法 JSON"),
                DeviceAction("send", envelope.encode(make_message(payload={"action": "format_disk"})) + b"\n", "未知 action"),
                DeviceAction("send", b"x" * (spec.MAX_MESSAGE_BYTES + 16) + b"\n", "超长帧"),
                DeviceAction("send", _submit("evt-mute-submit-1", "req-mute-1"), "合法帧，验证连接可恢复"),
                DeviceAction("send", _applied("evt-mute-applied-1", "req-mute-1"), "应用结果"),
            ],
            expected_events=[
                ("", "", "reject", spec.REASON_BAD_JSON),
                ("", "", "reject", spec.REASON_BAD_ACTION),
                ("", "", "reject", spec.REASON_TOO_LARGE),
                ("evt-mute-submit-1", "device.task.submit", "accept", "device.task.submit"),
                ("evt-mute-accepted-1", "host.task.accepted", "accept", "task_accepted"),
                ("evt-mute-completed-1", "host.task.completed", "accept", "task_completed"),
                ("evt-mute-delivered-1", "host.result.deliver", "accept", "result_delivered"),
                ("evt-mute-applied-1", "device.result.applied", "accept", "result_applied"),
            ],
            expected_status="applied",
        )
    raise ValueError(f"unknown scenario: {name}")


def load_evidence_sequences(evidence_dir: str | Path) -> dict[str, list[dict[str, Any]]]:
    """读取确定性回放证据的事件序列，作为回归基准。"""
    d = Path(evidence_dir)
    out: dict[str, list[dict[str, Any]]] = {}
    for key, filename in (("online", "replay-online.json"), ("offline", "replay-offline.json")):
        obj = json.loads((d / filename).read_text(encoding="utf-8"))
        out[key] = [
            {"event_id": e["event_id"], "kind": e["kind"],
             "outcome": e["outcome"], "reason": e["reason"]}
            for e in obj["events"]
        ]
    return out
