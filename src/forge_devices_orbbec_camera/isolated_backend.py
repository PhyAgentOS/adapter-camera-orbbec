"""Process-isolated Orbbec capture backend.

The Dora parent process owns Dora/Zenoh/OTEL file descriptors. A clean child
process owns pyorbbecsdk and the USB pipeline, preventing native ``select()``
calls from sharing Dora's descriptor table.
"""

from __future__ import annotations

import multiprocessing as mp
import queue
import time
from multiprocessing.queues import Queue
from typing import Any

import numpy as np
from forge_common import get_logger

from .backend import (
    _FRAME_STALL_TIMEOUT_SEC,
    CaptureBackend,
    OrbbecPointCloud,
    OrbbeFrame,
)
from .config import OrbbecConfig

logger = get_logger(__name__)

FramePacket = tuple[int, OrbbeFrame]
_CAPTURE_RESTART_LIMIT = 2
_CAPTURE_RESTART_BACKOFF_SEC = 1.0
_CAPTURE_STABLE_RESET_SEC = 60.0
_RECOVERY_SIGNAL_GRACE_SEC = 0.75


def _is_camera_disconnected(error: Exception) -> bool:
    message = str(error).lower()
    return any(
        term in message
        for term in (
            "no such device",
            "disconnected",
            "device not found",
            "i/o error",
            "未发现 orbbec 设备",
            "未找到序列号",
            "设备断开",
        )
    )


def _freeze_received_array(value: np.ndarray, dtype: np.dtype) -> np.ndarray:
    """Restore immutable contiguous columns after multiprocessing unpickling."""
    array = np.asarray(value, dtype=dtype)
    if not array.flags.c_contiguous or not array.flags.aligned:
        array = np.array(array, dtype=dtype, order="C", copy=True)
    current: object | None = array
    while isinstance(current, np.ndarray):
        current.setflags(write=False)
        current = current.base
    return array


def _freeze_received_point_cloud(cloud: OrbbecPointCloud) -> OrbbecPointCloud:
    float32 = np.dtype(np.float32)
    x = _freeze_received_array(cloud.x, float32)
    y = _freeze_received_array(cloud.y, float32)
    z = _freeze_received_array(cloud.z, float32)
    rgb: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None
    if cloud.rgb is not None:
        uint8 = np.dtype(np.uint8)
        rgb = tuple(_freeze_received_array(channel, uint8) for channel in cloud.rgb)
    return OrbbecPointCloud(
        width=cloud.width,
        height=cloud.height,
        is_dense=cloud.is_dense,
        x=x,
        y=y,
        z=z,
        rgb=rgb,
    )


def _put_latest(frame_queue: Queue, packet: FramePacket) -> None:
    """Keep only the newest frames so the parent never builds backlog."""
    while True:
        try:
            frame_queue.put_nowait(packet)
            return
        except queue.Full:
            try:
                frame_queue.get_nowait()
            except queue.Empty:
                return


def _put_status(status_queue: Queue, status: str, message: str = "") -> None:
    try:
        status_queue.put_nowait((status, message))
    except queue.Full:
        pass


def _capture_worker(
    config: OrbbecConfig,
    frame_queue: Queue,
    status_queue: Queue,
    stop_event: Any,
) -> None:
    """Run pyorbbecsdk in a clean child process with no Dora imports."""
    backend: CaptureBackend | None = None
    try:
        from .backend_orbbec import OrbbecBackend

        sent_seq = 0
        restarts = 0
        while not stop_event.is_set():
            try:
                backend = OrbbecBackend(config)
                after_seq = -1
                healthy_since: float | None = None
                while not stop_event.is_set():
                    timeout = (
                        max(float(config.init_timeout_sec), 0.5)
                        if after_seq < 0
                        else 0.5
                    )
                    frame, seq = backend.wait_new_frame(after_seq, timeout=timeout)
                    if seq <= after_seq:
                        continue
                    now = time.monotonic()
                    if healthy_since is None:
                        healthy_since = now
                    elif restarts and now - healthy_since >= _CAPTURE_STABLE_RESET_SEC:
                        restarts = 0
                    sent_seq += 1
                    _put_latest(frame_queue, (sent_seq, frame))
                    if sent_seq == 1:
                        _put_status(status_queue, "ready")
                    after_seq = seq
            except Exception as exc:
                if stop_event.is_set():
                    break
                if _is_camera_disconnected(exc) or restarts >= _CAPTURE_RESTART_LIMIT:
                    raise
                restarts += 1
                _put_status(status_queue, "recovering", str(sent_seq))
                logger.warning(
                    "[orbbec_camera] 采集失败，关闭并重开 Pipeline (%s/%s): %s",
                    restarts,
                    _CAPTURE_RESTART_LIMIT,
                    exc,
                )
            finally:
                if backend is not None:
                    backend.close()
                    backend = None
            if stop_event.wait(_CAPTURE_RESTART_BACKOFF_SEC):
                break
    except KeyboardInterrupt:
        _put_status(status_queue, "stopped")
    except BaseException as exc:  # native SDK wrappers can raise broad types
        _put_status(status_queue, "error", str(exc))
        raise
    finally:
        # Only discard packets on requested shutdown: an active reader may
        # otherwise be halfway through receiving one when capture fails.
        if stop_event.is_set():
            frame_queue.cancel_join_thread()
        frame_queue.close()


class IsolatedOrbbecBackend:
    """CaptureBackend proxying frames from an SDK-only child process."""

    def __init__(self, config: OrbbecConfig) -> None:
        self._config = config
        self._latest_frame: OrbbeFrame | None = None
        self._latest_seq = -1
        self._closed = False
        self._terminal_error: str | None = None
        self._last_frame_at: float | None = None
        self._recovering_until: float | None = None
        self._recover_min_seq = -1
        self._ctx = mp.get_context("spawn")
        self._frame_queue: Queue = self._ctx.Queue(maxsize=2)
        self._status_queue: Queue = self._ctx.Queue(maxsize=8)
        self._stop_event = self._ctx.Event()
        self._process = self._ctx.Process(
            target=_capture_worker,
            args=(
                config,
                self._frame_queue,
                self._status_queue,
                self._stop_event,
            ),
            name="orbbec-sdk-capture",
            daemon=True,
        )
        self._process.start()
        try:
            self._wait_until_ready()
        except BaseException:
            self.close()
            raise

    def capture_frame(self) -> OrbbeFrame:
        self._ensure_open()
        self._raise_if_worker_failed()
        self._drain_status()
        self._drain_frames()
        self._check_capture_state()
        if self._latest_frame is not None:
            return self._latest_frame
        frame, _ = self.wait_new_frame(-1, timeout=float(self._config.init_timeout_sec))
        return frame

    def wait_new_frame(
        self,
        after_seq: int,
        timeout: float = 2.0,
    ) -> tuple[OrbbeFrame, int]:
        self._ensure_open()
        self._raise_if_worker_failed()
        self._drain_status()
        self._drain_frames()
        self._check_capture_state()
        if self._latest_frame is not None and self._latest_seq > after_seq:
            return self._latest_frame, self._latest_seq

        effective_timeout = max(float(timeout), 0.0)
        if after_seq < 0 and self._latest_frame is None:
            effective_timeout = max(
                effective_timeout, float(self._config.init_timeout_sec)
            )
        deadline = time.monotonic() + effective_timeout
        while True:
            self._raise_if_worker_failed()
            self._drain_status()
            self._drain_frames()
            self._check_capture_state()
            if self._recovering_until is not None:
                deadline = max(deadline, self._recovering_until)
            elif self._last_frame_at is not None:
                deadline = max(
                    deadline,
                    self._last_frame_at
                    + _FRAME_STALL_TIMEOUT_SEC
                    + _RECOVERY_SIGNAL_GRACE_SEC,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if self._latest_frame is None:
                    raise RuntimeError("等待 Orbbec 子进程首帧超时")
                return self._latest_frame, self._latest_seq
            try:
                packet = self._frame_queue.get(timeout=min(0.2, remaining))
            except queue.Empty:
                self._drain_status()
                continue
            self._accept_packet(packet)
            self._drain_frames()
            if self._latest_seq > after_seq:
                assert self._latest_frame is not None
                return self._latest_frame, self._latest_seq

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._latest_frame = None
        self._stop_event.set()
        if self._process.is_alive():
            self._process.join(timeout=3.0)
        if self._process.is_alive():
            logger.warning("[orbbec_camera] 采集子进程未及时停止，发送 terminate")
            self._process.terminate()
            self._process.join(timeout=2.0)
        if self._process.is_alive():
            logger.warning("[orbbec_camera] 采集子进程仍未停止，发送 kill")
            self._process.kill()
            self._process.join(timeout=1.0)
        if self._process.is_alive():
            raise RuntimeError("Orbbec 子进程未能停止，设备资源可能尚未释放")
        self._close_queue(self._frame_queue)
        self._close_queue(self._status_queue)

    def _wait_until_ready(self) -> None:
        timeout = max(
            (_CAPTURE_RESTART_LIMIT + 1) * float(self._config.init_timeout_sec)
            + _CAPTURE_RESTART_LIMIT * _CAPTURE_RESTART_BACKOFF_SEC
            + 5.0,
            5.0,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._raise_if_worker_failed(allow_starting=True)
            try:
                status, message = self._status_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if status == "ready":
                return
            if status == "error":
                raise RuntimeError(f"Orbbec 子进程初始化失败: {message}")
        raise RuntimeError(f"等待 Orbbec 子进程初始化超时（{timeout:.0f}s）")

    def _drain_frames(self) -> None:
        while True:
            try:
                packet = self._frame_queue.get_nowait()
            except queue.Empty:
                return
            self._accept_packet(packet)

    def _accept_packet(self, packet: FramePacket) -> None:
        seq, frame = packet
        if int(seq) > max(self._latest_seq, self._recover_min_seq):
            self._last_frame_at = time.monotonic()
            self._recovering_until = None
            if frame.point_cloud is not None:
                frame.point_cloud = _freeze_received_point_cloud(frame.point_cloud)
            self._latest_seq = int(seq)
            self._latest_frame = frame

    def _drain_status(self) -> None:
        while True:
            try:
                status, message = self._status_queue.get_nowait()
            except queue.Empty:
                return
            if status == "error":
                self._terminal_error = message
                self._latest_frame = None
                raise RuntimeError(f"Orbbec 子进程采集失败: {message}")
            if status == "recovering":
                failed_seq = int(message)
                if self._latest_seq > failed_seq:
                    continue
                self._recover_min_seq = max(self._recover_min_seq, failed_seq)
                self._latest_frame = None
                self._recovering_until = (
                    time.monotonic()
                    + float(self._config.init_timeout_sec)
                    + _CAPTURE_RESTART_BACKOFF_SEC
                    + 5.0
                )

    def _check_capture_state(self) -> None:
        if self._terminal_error is None and self._last_frame_at is not None:
            now = time.monotonic()
        else:
            now = None
        if now is not None and now - self._last_frame_at >= _FRAME_STALL_TIMEOUT_SEC:
            self._latest_frame = None
            if self._recovering_until is None and now < (
                self._last_frame_at
                + _FRAME_STALL_TIMEOUT_SEC
                + _RECOVERY_SIGNAL_GRACE_SEC
            ):
                return
            if self._recovering_until is None or now >= self._recovering_until:
                self._terminal_error = "采集重试后仍未收到子进程新帧，采集终止"
                self._stop_event.set()
        if self._terminal_error is not None:
            self._latest_frame = None
            raise RuntimeError(f"Orbbec 子进程采集失败: {self._terminal_error}")

    def _raise_if_worker_failed(self, *, allow_starting: bool = False) -> None:
        if self._process.is_alive():
            return
        code = self._process.exitcode
        if allow_starting and code is None:
            return
        if code is None:
            return
        self._drain_status()
        raise RuntimeError(f"Orbbec 子进程退出，exitcode={code}")

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Orbbec 隔离采集后端已关闭")

    @staticmethod
    def _close_queue(q: Any) -> None:
        try:
            q.close()
            q.join_thread()
        except Exception:
            pass
