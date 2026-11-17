"""虚拟传输设备：内存字节管道，模拟串口连接/断线/重连。

用途（WP-A1 / WP-A2 前置）：
- 在无真实硬件下回放成功/离线/超时/重复/非法帧剧本；
- 验证 COM-06 错误帧恢复与重连后快照同步。

不模拟 Windows 能力结果，不产生硬件证据；只是字节管道。
"""
from __future__ import annotations

from collections import deque
from typing import Callable

from .base import Transport


class VirtualTransport(Transport):
    """双向内存管道。device_to_host / host_to_device 各一个字节队列。"""

    def __init__(self) -> None:
        self._dev_to_host: deque[bytes] = deque()
        self._host_to_dev: deque[bytes] = deque()
        self._connected = False
        self._callbacks: list[Callable[[str, str], None]] = []

    # ---- Transport 接口 ----
    def connect(self) -> None:
        if not self._connected:
            self._connected = True
            self._emit("connected", "virtual")

    def disconnect(self) -> None:
        if self._connected:
            self._connected = False
            self._emit("disconnected", "virtual")

    @property
    def connected(self) -> bool:
        return self._connected

    def read(self, max_bytes: int = 65536) -> bytes:
        if not self._connected:
            return b""
        out = bytearray()
        while self._dev_to_host and len(out) < max_bytes:
            part = self._dev_to_host.popleft()
            room = max_bytes - len(out)
            out.extend(part[:room])
            if len(part) > room:
                self._dev_to_host.appendleft(part[room:])
                break
        return bytes(out)

    def write(self, data: bytes) -> int:
        if not self._connected:
            raise ConnectionError("transport disconnected")
        self._host_to_dev.append(bytes(data))
        return len(data)

    def on_event(self, callback: Callable[[str, str], None]) -> None:
        self._callbacks.append(callback)

    def _emit(self, event: str, detail: str) -> None:
        for cb in list(self._callbacks):
            cb(event, detail)

    # ---- 虚拟设备侧控制（测试/回放用，不属于宿主 API）----
    def device_send(self, data: bytes) -> None:
        """设备侧写入宿主可读队列；断线时不入队（模拟送达失败）。"""
        if self._connected:
            self._dev_to_host.append(bytes(data))

    def device_read(self, max_bytes: int = 65536) -> bytes:
        out = bytearray()
        while self._host_to_dev and len(out) < max_bytes:
            part = self._host_to_dev.popleft()
            room = max_bytes - len(out)
            out.extend(part[:room])
            if len(part) > room:
                self._host_to_dev.appendleft(part[room:])
                break
        return bytes(out)

    def pending_device_bytes(self) -> int:
        return sum(len(x) for x in self._dev_to_host)

    def clear_queues(self) -> None:
        self._dev_to_host.clear()
        self._host_to_dev.clear()
