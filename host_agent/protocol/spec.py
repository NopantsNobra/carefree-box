"""DBX-JSONL-1 协议常量与白名单。

唯一真源：开发手册 V3.1 第 6 章、evidence/adapter-contract.json。
任何校验不得在本文件之外另立白名单；本文件变更即协议变更，须升 protocol_version。
"""
from __future__ import annotations

PROTOCOL_VERSION = "1.0"
PROJECT_ID = "desk-box-pro-v30"
# 旧原型的 project_id，仅作历史兼容读取；新开发不得产出该值
LEGACY_PROJECT_IDS = frozenset({"desk-box-pro-v21"})

MAX_MESSAGE_BYTES = 2048          # 开发手册 6.1：单条消息最大 2048 字节
MAX_PAYLOAD_BYTES = 1024          # 开发默认值，防 payload 撑爆；M2/M3 实测后可冻结调整
MAX_LINE_BUFFER_BYTES = MAX_MESSAGE_BYTES * 4   # 帧累积缓冲上限，超出即进入恢复

MESSAGE_KINDS = frozenset({
    "device.boot",
    "device.state.snapshot",
    "device.task.submit",
    "host.task.accepted",
    "host.task.completed",
    "host.result.deliver",
    "device.result.applied",
    "device.power.state",
})

# 由设备发往宿主的 kind（宿主应作为请求/事件接收）
DEVICE_TO_HOST_KINDS = frozenset({
    "device.boot",
    "device.state.snapshot",
    "device.task.submit",
    "device.result.applied",
    "device.power.state",
})
# 由宿主发往设备的 kind（宿主产出）
HOST_TO_DEVICE_KINDS = frozenset({
    "host.task.accepted",
    "host.task.completed",
    "host.result.deliver",
})

CAPABILITIES = frozenset({
    "voice_control",
    "microphone_mute",
    "led_output",
    "windows_audio",
    "windows_display",
    "windows_projection",
    "windows_restart",
    "device_power",
    "custom_text",   # 原型沙盒演示用，不进入实机生产命令表
})

# capability -> 允许的 payload.action 白名单。action 与 payload 必须来自白名单，
# 不接受自由文本拼接（开发手册 3.2 / 7.5）。
ACTION_WHITELIST: dict[str, frozenset[str]] = {
    "voice_control": frozenset({"set_voice_enabled"}),
    "microphone_mute": frozenset({"set_microphone_enabled"}),
    "led_output": frozenset({"set_light_position"}),
    "windows_audio": frozenset({"set_mute", "restore_audio"}),
    "windows_display": frozenset({"set_display_power", "restore_session"}),
    "windows_projection": frozenset({"set_projection"}),
    "windows_restart": frozenset({"request_restart"}),
    "device_power": frozenset({"set_logical_power"}),
    "custom_text": frozenset({"display_text"}),
}

RESULT_SOURCES = frozenset({
    "unknown",
    "host_observed",
    "hardware_verified",
    "software_simulation",
    "documentation_claim",
    "maintainer_history",
})

LIFECYCLE = ("submitted", "accepted", "completed", "delivered", "applied")

# 公共信封必填字段（开发手册 6.2）。uptime_ms 对设备消息必填，对宿主事件不适用。
REQUIRED_FIELDS = frozenset({
    "protocol_version", "project_id", "device_id", "boot_id", "event_id",
    "request_id", "revision", "kind", "capability", "payload",
    "result_source", "sent_at",
})
OPTIONAL_FIELDS = frozenset({"expires_at", "uptime_ms"})

# 拒绝原因枚举（错误码列表首版约定；实机评审后固化）
REASON_FIELD_MISSING = "field_missing"
REASON_BAD_JSON = "bad_json"
REASON_NOT_OBJECT = "not_object"
REASON_TOO_LARGE = "message_too_large"
REASON_PAYLOAD_TOO_LARGE = "payload_too_large"
REASON_BAD_PROTOCOL_VERSION = "unsupported_protocol_version"
REASON_BAD_PROJECT = "unexpected_project_id"
REASON_BAD_KIND = "unknown_kind"
REASON_BAD_CAPABILITY = "unknown_capability"
REASON_BAD_ACTION = "unknown_action"
REASON_BAD_REVISION = "invalid_revision"
REASON_BAD_RESULT_SOURCE = "unknown_result_source"
REASON_BAD_FIELD_TYPE = "invalid_field_type"

REJECT_REASONS = frozenset({
    REASON_FIELD_MISSING, REASON_BAD_JSON, REASON_NOT_OBJECT, REASON_TOO_LARGE,
    REASON_PAYLOAD_TOO_LARGE, REASON_BAD_PROTOCOL_VERSION, REASON_BAD_PROJECT,
    REASON_BAD_KIND, REASON_BAD_CAPABILITY, REASON_BAD_ACTION, REASON_BAD_REVISION,
    REASON_BAD_RESULT_SOURCE, REASON_BAD_FIELD_TYPE,
})
