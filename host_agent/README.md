# host_agent（Windows 配套程序 · M1 工作线）

> 目录名 `host_agent/` 对应开发手册 7.1 推荐的 `host-agent/` 结构；Python 包名不可含连字符，故用下划线。

## 当前范围（WP-A1 / WP-A2 / WP-A3）

已实现：

- `protocol/spec.py`：DBX-JSONL-1 常量与白名单（kind / capability / payload.action / result_source），唯一真源
- `protocol/envelope.py`：JSONL 编解码 + 长度/格式/必填字段/字段类型/白名单校验，拒绝时给出结构化原因
- `protocol/lifecycle.py`：去重键、幂等、identity_conflict、revision 单调、终态、重启会话、状态持久化
- `transport/base.py`：与介质无关的分帧器，超长帧丢弃后连接可恢复；Transport 抽象接口
- `transport/virtual.py`：内存虚拟设备，用于无硬件回放
- `agent.py`：HostReceiver，把字节流变成可执行消息与拒绝记录；`on_record`/`on_accept` 是接入点
- `capabilities/base.py`：能力执行器接口与安全默认（一律返回 `unsupported_capability`，不调用系统 API）
- `simulator/`：五类剧本（success / offline / timeout / duplicate / illegal_frame）+ 剧本执行器；事件序列与 `evidence/replay-online.json`、`replay-offline.json` 交叉验证
- `orchestrator.py`：HostOrchestrator，驱动五阶段、busy 单任务锁、超时/离线不确定终态、迟到结果拒绝、宿主事实持久化

未实现（后续工作包，**不得据本目录宣称已完成**）：

- WP-A4～A6 音频/显示投影/重启的真实 Windows API 调用与结果回读
- WP-A7 UI、日志轮转与导出
- 真实串口驱动（当前仅内存虚拟传输；无 pyserial 依赖）
- 心跳/离线判定定时器（起始值已定义，未接入调度）

## 运行验收

```bash
# 从仓库根目录，运行全部 54 项
python -m unittest discover -s . -t . -p 'test_*.py' -v
```

覆盖：

- **WP-A1（31 项）**：信封负向用例（非法 JSON、非对象、超长消息/载荷、未知 kind/capability/action、缺字段、错版本、错项目、非法 revision、设备消息缺 uptime_ms）、分帧错误恢复、COM-06 端到端、生命周期（五阶段、重复、身份冲突、陈旧修订、超时终态、旧 boot、持久化往返）、重连快照同步
- **WP-A2（12 项）**：五剧本构建、证据文件加载、成功五阶段、离线 `completed_delivery_unknown`、超时 `timeout_result_unknown`、重复只执行一次、非法帧后恢复、与 `replay-online/offline.json` 事件序列逐项交叉验证
- **WP-A3（11 项）**：busy 锁不排队、白名单拦截任意 action、默认执行器返回不支持、剧本执行器不得伪造 `host_observed`/`hardware_verified`、宿主事实跨重启存活、旧 boot 结果不能复活事实、不确定终态不改写为成功/失败

## 证据纪律

本目录的测试只验证协议与传输行为，**不验证 Windows 能力执行结果，不构成实机验收**。任何测试通过都不得写成 `host_observed` 或 `hardware_verified`。实机验收须按开发手册第 8.1 节记录板卡型号、固件版本、Windows 版本、操作步骤与原始日志。
