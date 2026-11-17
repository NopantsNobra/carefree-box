"""剧本化能力执行器（仅用于虚拟回放与测试）。

明确标注 result_source='software_simulation'：任何返回结果都不得被解释为
Windows 真机观察或实机验收。真实能力适配器由 A4～A6 另行实现。
"""
from __future__ import annotations

from typing import Any

from ..capabilities.base import CapabilityResult


class ScriptedExecutor:
    """按预设结果返回，不调用任何系统 API。

    late=True 时返回 None，模拟"结果在截止时间前未到达"，由编排器置
    timeout_result_unknown；这不是失败也不是成功。
    """

    def __init__(self, status: str = "success", observed: dict[str, Any] | None = None,
                 result_source: str = "software_simulation", late: bool = False) -> None:
        if result_source not in ("software_simulation", "unknown"):
            # 防止剧本执行器伪造 host_observed / hardware_verified
            raise ValueError("scripted executor must not claim observed/hardware result_source")
        self.status = status
        self.observed = observed or {}
        self.result_source = result_source
        self.late = late
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def execute(self, capability: str, action: str, payload: dict[str, Any]) -> CapabilityResult | None:
        self.calls.append((capability, action, dict(payload)))
        if self.late:
            return None
        return CapabilityResult(
            status=self.status,
            action=action,
            observed={**self.observed, "scripted": True},
            result_source=self.result_source,
        )
