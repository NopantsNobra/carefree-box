"""能力执行器抽象。

职责边界（开发手册 2.1 / 7.6）：
- 宿主程序执行白名单能力，不执行设备发来的任意字符串/脚本/Shell；
- 只有实际调用并取得可支持结果后，才产生完成事件与 `host_observed` 来源；
- 未实现的能力返回 `unsupported_capability`，不得伪造成功；
- 返回 None 表示结果在截止时间前未到达（超时），编排器据此置
  `timeout_result_unknown`，不可改写为失败或成功。

本模块只定义接口与安全的默认实现。真实 Windows API 由后续工作包（A4～A6）
实现，当前默认执行器对所有能力返回 unsupported_capability。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..protocol import spec


@dataclass(frozen=True)
class CapabilityResult:
    status: str          # success | partial | failure | unsupported_capability
    action: str
    observed: dict[str, Any]
    result_source: str   # host_observed / software_simulation / unknown ...

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "action": self.action,
            "observed": dict(self.observed),
        }


class CapabilityExecutor(Protocol):
    def execute(self, capability: str, action: str, payload: dict[str, Any]) -> CapabilityResult | None:
        ...


class UnsupportedExecutor:
    """安全默认：不调用任何系统 API，明确返回不支持。

    用于尚未实现能力层的阶段，确保编排器不会把"未执行"写成"已执行"。
    """

    def execute(self, capability: str, action: str, payload: dict[str, Any]) -> CapabilityResult:
        return CapabilityResult(
            status="unsupported_capability",
            action=action,
            observed={"reason": "no capability adapter installed", "capability": capability},
            result_source="unknown",
        )


def is_whitelisted_action(capability: str, action: str) -> bool:
    return action in spec.ACTION_WHITELIST.get(capability, frozenset())
