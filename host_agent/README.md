# host_agent（Windows 配套程序 · M1 工作线）

> 目录名 `host_agent/` 对应开发手册 7.1 推荐的 `host-agent/` 结构；Python 包名不可含连字符，故用下划线。

## 当前范围（WP-A1）

已实现：

- `protocol/spec.py`：DBX-JSONL-1 常量与白名单（kind / capability / payload.action / result_source），唯一真源
- `protocol/envelope.py`：JSONL 编解码 + 长度/格式/必填字段/字段类型/白名单校验，拒绝时给出结构化原因
- `protocol/lifecycle.py`：去重键、幂等、identity_conflict、revision 单调、终态、重启会话、状态持久化
- `transport/base.py`：与介质无关的分帧器，超长帧丢弃后连接可恢复；Transport 抽象接口
- `transport/virtual.py`：内存虚拟设备，用于无硬件回放
- `agent.py`：HostReceiver，把字节流变成可执行消息与拒绝记录；`on_accept` 是能力层后续接入点

未实现（后续工作包，**不得据本目录宣称已完成**）：

- WP-A2 五类剧本虚拟设备回放脚本（成功/离线/超时/重复/非法帧的编排器）
- WP-A3 宿主侧请求生命周期编排与能力调度
- WP-A4～A6 音频/显示投影/重启的真实 Windows API 调用与结果回读
- WP-A7 UI、日志轮转与导出
- 真实串口驱动（当前仅内存虚拟传输；无 pyserial 依赖）

## 运行验收

```bash
# 从仓库根目录
python -m unittest host_agent.tests.test_wp_a1 -v
```

31 项测试覆盖：信封负向用例（非法 JSON、非对象、超长消息/载荷、未知 kind/capability/action、缺字段、错版本、错项目、非法 revision、设备消息缺 uptime_ms）、分帧错误恢复、COM-06 端到端（拒绝后连接可恢复、混合批次部分接受）、生命周期（五阶段到 applied、重复忽略、身份冲突、陈旧修订、超时终态拒绝迟到结果、旧 boot 拒绝、宿主事实跨重启存活、持久化往返）、重连快照同步。

## 证据纪律

本目录的测试只验证协议与传输行为，**不验证 Windows 能力执行结果，不构成实机验收**。任何测试通过都不得写成 `host_observed` 或 `hardware_verified`。实机验收须按开发手册第 8.1 节记录板卡型号、固件版本、Windows 版本、操作步骤与原始日志。
