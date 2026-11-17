"""测试消息构造器。只构造合法信封，便于逐字段篡改做负向测试。"""
from __future__ import annotations

from typing import Any

from ..protocol import spec


def make_message(
    kind: str = "device.task.submit",
    capability: str = "windows_audio",
    payload: dict[str, Any] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "protocol_version": spec.PROTOCOL_VERSION,
        "project_id": spec.PROJECT_ID,
        "device_id": "deskbox-test-01",
        "boot_id": "boot-0001",
        "event_id": "evt-0001",
        "request_id": "req-0001",
        "revision": 1,
        "kind": kind,
        "capability": capability,
        "payload": payload if payload is not None else {"action": "set_mute", "muted": True},
        "result_source": "unknown",
        "sent_at": None,
        "uptime_ms": 1234,
    }
    msg.update(overrides)
    return msg
