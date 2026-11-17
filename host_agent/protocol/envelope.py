"""DBX-JSONL-1 信封编解码与校验。

设计要点（对应 WP-A1 / COM-06）：
- 每行一条 JSON 消息，以 \\n 结束；超长/残缺/非法 JSON 一律不执行，返回结构化拒绝。
- 校验顺序固定：字节长度 -> JSON 解析 -> 对象类型 -> 必填字段 -> 字段类型 ->
  协议版本 -> project_id -> kind 白名单 -> capability 白名单 -> action 白名单 -> result_source。
- 校验结果只描述"是否可执行"，不推进任何业务状态（生命周期见 lifecycle.py）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from . import spec


@dataclass
class ValidationResult:
    ok: bool
    message: dict[str, Any] | None = None
    reason: str | None = None
    detail: str = ""
    # 拒绝时哪些字段有问题，便于日志与错误恢复
    bad_fields: list[str] = field(default_factory=list)


def encode(message: dict[str, Any]) -> bytes:
    """把消息对象编码为一行 UTF-8 JSONL（含 \\n）。"""
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return body + b"\n"


def validate(message: dict[str, Any]) -> ValidationResult:
    """校验单个已解析的消息对象。不修改入参。"""
    bad: list[str] = []

    # 必填字段
    missing = [k for k in sorted(spec.REQUIRED_FIELDS) if k not in message]
    if missing:
        return ValidationResult(False, reason=spec.REASON_FIELD_MISSING,
                                detail="missing: " + ",".join(missing), bad_fields=missing)

    # 字段类型（信封层）
    if not isinstance(message["protocol_version"], str):
        bad.append("protocol_version")
    if not isinstance(message["project_id"], str):
        bad.append("project_id")
    if not isinstance(message["device_id"], str) or not message["device_id"]:
        bad.append("device_id")
    if not isinstance(message["boot_id"], str) or not message["boot_id"]:
        bad.append("boot_id")
    if not isinstance(message["event_id"], str) or not message["event_id"]:
        bad.append("event_id")
    if not isinstance(message["request_id"], str) or not message["request_id"]:
        bad.append("request_id")
    if not isinstance(message["revision"], int) or isinstance(message["revision"], bool) or message["revision"] < 0:
        bad.append("revision")
    if not isinstance(message["kind"], str):
        bad.append("kind")
    if not isinstance(message["capability"], str):
        bad.append("capability")
    if not isinstance(message["payload"], dict):
        bad.append("payload")
    if not isinstance(message["result_source"], str):
        bad.append("result_source")
    if message["sent_at"] is not None and not isinstance(message["sent_at"], str):
        bad.append("sent_at")
    if "expires_at" in message and message["expires_at"] is not None and not isinstance(message["expires_at"], str):
        bad.append("expires_at")
    if "uptime_ms" in message and (not isinstance(message["uptime_ms"], int) or message["uptime_ms"] < 0):
        bad.append("uptime_ms")
    if bad:
        return ValidationResult(False, reason=spec.REASON_BAD_FIELD_TYPE,
                                detail="invalid types: " + ",".join(bad), bad_fields=bad)

    # 设备消息必须提供 uptime_ms（开发手册 6.2）
    if message["kind"] in spec.DEVICE_TO_HOST_KINDS and message["kind"] != "device.boot":
        if "uptime_ms" not in message:
            return ValidationResult(False, reason=spec.REASON_FIELD_MISSING,
                                    detail="device message requires uptime_ms", bad_fields=["uptime_ms"])

    # 协议版本
    if message["protocol_version"] != spec.PROTOCOL_VERSION:
        return ValidationResult(False, reason=spec.REASON_BAD_PROTOCOL_VERSION,
                                detail=message["protocol_version"], bad_fields=["protocol_version"])

    # project_id：新开发只接受 v30；旧 v21 作为历史兼容，仅允许读取device方向事件
    if message["project_id"] in spec.LEGACY_PROJECT_IDS:
        if message["kind"] not in spec.DEVICE_TO_HOST_KINDS:
            return ValidationResult(False, reason=spec.REASON_BAD_PROJECT,
                                    detail="legacy project only for device->host", bad_fields=["project_id"])
    elif message["project_id"] != spec.PROJECT_ID:
        return ValidationResult(False, reason=spec.REASON_BAD_PROJECT,
                                detail=message["project_id"], bad_fields=["project_id"])

    # kind 白名单
    if message["kind"] not in spec.MESSAGE_KINDS:
        return ValidationResult(False, reason=spec.REASON_BAD_KIND,
                                detail=message["kind"], bad_fields=["kind"])

    # capability 白名单
    if message["capability"] not in spec.CAPABILITIES:
        return ValidationResult(False, reason=spec.REASON_BAD_CAPABILITY,
                                detail=message["capability"], bad_fields=["capability"])

    # payload.action 白名单（若消息带 action）
    payload = message["payload"]
    if "action" in payload:
        action = payload["action"]
        allowed = spec.ACTION_WHITELIST.get(message["capability"], frozenset())
        if action not in allowed:
            return ValidationResult(False, reason=spec.REASON_BAD_ACTION,
                                    detail=f"{message['capability']}.{action}", bad_fields=["payload.action"])

    # result_source 白名单
    if message["result_source"] not in spec.RESULT_SOURCES:
        return ValidationResult(False, reason=spec.REASON_BAD_RESULT_SOURCE,
                                detail=message["result_source"], bad_fields=["result_source"])

    # payload 大小
    payload_bytes = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if payload_bytes > spec.MAX_PAYLOAD_BYTES:
        return ValidationResult(False, reason=spec.REASON_PAYLOAD_TOO_LARGE,
                                detail=str(payload_bytes), bad_fields=["payload"])

    return ValidationResult(True, message=message)


def decode_line(raw: bytes) -> ValidationResult:
    """解析并校验一行 JSONL 字节（不含尾部 \\n）。

    返回 ValidationResult：失败时 ok=False 且 reason 指明拒绝原因，
    调用方不得执行该消息。
    """
    if len(raw) > spec.MAX_MESSAGE_BYTES:
        return ValidationResult(False, reason=spec.REASON_TOO_LARGE,
                                detail=f"{len(raw)}>{spec.MAX_MESSAGE_BYTES}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        return ValidationResult(False, reason=spec.REASON_BAD_JSON, detail=f"utf8: {e}")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        return ValidationResult(False, reason=spec.REASON_BAD_JSON, detail=str(e))
    if not isinstance(obj, dict):
        return ValidationResult(False, reason=spec.REASON_NOT_OBJECT, detail=type(obj).__name__)
    return validate(obj)
