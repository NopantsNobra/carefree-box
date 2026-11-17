"""帧累积与错误恢复（与具体传输介质无关）。

COM-06 要求：非法 JSON、超长帧、未知 action 不执行，连接可恢复。
本模块负责把字节流切成一行行 JSONL；单行校验交给 protocol.envelope。

错误恢复策略：
- 逐字节累积到缓冲，遇 \\n 提交一行。
- 行超过 MAX_MESSAGE_BYTES 时，丢弃该行剩余字节直到下一个 \\n，
  并产出一条 frame_too_long 错误事件，连接保持可用。
- 空行忽略。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..protocol import spec


@dataclass
class FrameEvent:
    kind: str          # "line" | "error" | "dropped"
    data: bytes = b""
    reason: str = ""


class LineFramer:
    """增量式 JSONL 分帧器，带超长帧恢复。"""

    def __init__(self, max_bytes: int | None = None) -> None:
        self.max_bytes = max_bytes or spec.MAX_MESSAGE_BYTES
        self._buf = bytearray()
        self._dropping = False
        self.dropped_frames = 0
        self.total_lines = 0

    def feed(self, chunk: bytes) -> list[FrameEvent]:
        """喂入任意字节块，返回按序产生的帧事件。"""
        events: list[FrameEvent] = []
        for byte in chunk:
            if byte == 0x0A:  # \n
                if self._dropping:
                    self._dropping = False
                    events.append(FrameEvent("dropped", reason="frame_too_long"))
                else:
                    line = bytes(self._buf)
                    self._buf.clear()
                    if line:
                        self.total_lines += 1
                        if len(line) > self.max_bytes:
                            events.append(FrameEvent("error", line, "frame_too_long"))
                        else:
                            events.append(FrameEvent("line", line))
                    # 空行忽略
                continue

            if self._dropping:
                continue
            if len(self._buf) >= self.max_bytes:
                # 超限：进入丢弃态直到换行，连接不中断
                self._dropping = True
                self._buf.clear()
                self.dropped_frames += 1
                continue
            self._buf.append(byte)
        return events

    def reset(self) -> None:
        """重连时清空缓冲，避免跨会话残帧粘连。"""
        self._buf.clear()
        self._dropping = False


class TransportError(Exception):
    pass


class Transport:
    """传输接口。真实串口实现与虚拟设备均实现此接口。

    宿主只依赖本接口，不直接依赖 pyserial 等具体库。
    """

    def connect(self) -> None:
        raise NotImplementedError

    def disconnect(self) -> None:
        raise NotImplementedError

    @property
    def connected(self) -> bool:
        raise NotImplementedError

    def read(self, max_bytes: int = 65536) -> bytes:
        """非阻塞读取；无数据返回 b''。"""
        raise NotImplementedError

    def write(self, data: bytes) -> int:
        raise NotImplementedError

    def on_event(self, callback: Callable[[str, str], None]) -> None:
        """注册连接事件回调：callback(event, detail)，event 取 connected/disconnected/error。"""
        raise NotImplementedError
