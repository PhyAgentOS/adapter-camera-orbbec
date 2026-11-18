"""Capture lifecycle regressions; no USB device is opened by these tests."""

from __future__ import annotations

import multiprocessing as mp
import queue
import threading
from unittest import mock

import numpy as np
import pytest
import test_message_builders as fixtures

from forge_devices_orbbec_camera import backend_orbbec, isolated_backend
from forge_devices_orbbec_camera.backend import OrbbeFrame
from forge_devices_orbbec_camera.config import OrbbecConfig


def _frame() -> OrbbeFrame:
    return OrbbeFrame(
        color=np.zeros((1, 1, 3), dtype=np.uint8),
        depth=np.ones((1, 1), dtype=np.float32),
        ir=np.zeros((1, 1), dtype=np.uint8),
    )


@pytest.mark.parametrize("missing", ["color", "depth", "ir"])
def test_readiness_waits_for_all_enabled_streams(missing: str) -> None:
    pipeline = mock.Mock()
    pipeline.wait_for_frames.return_value = object()
    backend = fixtures.BackendLifecycleTests._loop_backend(OrbbecConfig(), pipeline)
    partial, complete = _frame(), _frame()
    setattr(partial, missing, None)

    def convert(_frameset):
        assert not backend._init_done.is_set()
        assert not backend._first_frame_event.is_set()
        assert backend._latest_frame is None
        if backend._convert_frameset.call_count == 1:
            return partial
        return complete

    first_frame = backend._first_frame_event.set

    def ready():
        first_frame()
        backend._stop_event.set()

    backend._first_frame_event.set = ready
    backend._convert_frameset = mock.Mock(side_effect=convert)
    backend._capture_loop()
    assert backend._init_done.is_set()
    assert backend._first_frame_event.is_set()
    assert backend._frame_seq == 1
    assert backend._latest_frame is complete


def test_jpeg_fast_path_and_disabled_streams_allow_readiness() -> None:
    config = OrbbecConfig()
    config.depth.enabled = False
    config.ir.enabled = False
    backend = fixtures.BackendLifecycleTests._loop_backend(config, mock.Mock())
    assert backend._has_required_streams(
        OrbbeFrame(None, None, None, color_jpeg=b"jpeg")
    )
    assert not backend._has_required_streams(
        OrbbeFrame(np.empty((0, 0, 3), dtype=np.uint8), None, None)
    )
    config.depth.enabled = True
    config.point_cloud.enabled = True
    assert backend._has_required_streams(_frame())  # No optional point cloud yet.


@pytest.mark.parametrize("invalid_frameset", [False, True])
def test_post_first_frame_stall_terminates_on_empty_or_invalid_frames(
    invalid_frameset,
) -> None:
    now = [0.0]
    pipeline = mock.Mock()
    backend = fixtures.BackendLifecycleTests._loop_backend(OrbbecConfig(), pipeline)

    def wait(_timeout):
        now[0] += 2.0
        if now[0] == 2.0 or invalid_frameset:
            return object()
        return None

    pipeline.wait_for_frames.side_effect = wait
    backend._convert_frameset = mock.Mock(
        side_effect=[_frame()] + [OrbbeFrame(None, None, None)] * 3
    )
    with mock.patch.object(
        backend_orbbec.time, "monotonic", side_effect=lambda: now[0]
    ):
        backend._capture_loop()
    assert "5s" in backend._terminal_error
    assert backend._latest_frame is None
    assert backend._frame_seq == 1
    pipeline.stop.assert_called_once_with()


@pytest.mark.parametrize("method", ["capture_frame", "wait_new_frame"])
def test_readers_reject_cache_when_sdk_is_stuck(method) -> None:
    backend = fixtures.BackendLifecycleTests._loop_backend(OrbbecConfig(), mock.Mock())
    backend._first_frame_event.set()
    backend._latest_frame = _frame()
    backend._last_frame_at = 10.0
    with (
        mock.patch.object(backend_orbbec.time, "monotonic", return_value=15.0),
        pytest.raises(RuntimeError, match="5s"),
    ):
        getattr(backend, method)(*(() if method == "capture_frame" else (0, 0)))
    assert backend._latest_frame is None
    assert backend._stop_event.is_set()


def test_close_wakes_waiters_and_rejects_old_cache() -> None:
    backend = fixtures.BackendLifecycleTests._loop_backend(OrbbecConfig(), mock.Mock())
    backend._thread = mock.Mock()
    backend._thread.is_alive.return_value = False
    backend._latest_frame = _frame()
    backend.close()
    for call in (backend.capture_frame, lambda: backend.wait_new_frame(0, 0)):
        with pytest.raises(RuntimeError, match="已关闭"):
            call()
    assert backend._latest_frame is None


def test_close_reports_thread_that_did_not_stop() -> None:
    backend = fixtures.BackendLifecycleTests._loop_backend(OrbbecConfig(), mock.Mock())
    backend._thread = mock.Mock()
    backend._thread.is_alive.return_value = True
    with pytest.raises(RuntimeError, match="未能停止"):
        backend.close()


def _proxy():
    proxy = isolated_backend.IsolatedOrbbecBackend.__new__(
        isolated_backend.IsolatedOrbbecBackend
    )
    proxy._closed = False
    proxy._config = OrbbecConfig()
    proxy._terminal_error = None
    proxy._latest_frame = _frame()
    proxy._latest_seq = 1
    proxy._last_frame_at = 0.0
    proxy._recovering_until = None
    proxy._recover_min_seq = -1
    proxy._usb_device_node = None
    proxy._frame_queue = queue.Queue()
    proxy._status_queue = queue.Queue()
    proxy._stop_event = threading.Event()
    proxy._process = mock.Mock()
    proxy._process.is_alive.return_value = True
    return proxy


def test_proxy_reports_usb_disconnect_before_frame_timeout(tmp_path) -> None:
    proxy = _proxy()
    device = tmp_path / "033"
    device.touch()
    proxy._usb_device_node = device
    proxy._next_usb_check = 0.0
    proxy._config.device_serial = "AY6Z363011Y"
    with mock.patch.object(isolated_backend.time, "monotonic", return_value=0.2):
        proxy._check_capture_state()
    device.unlink()
    with (
        mock.patch.object(isolated_backend.time, "monotonic", return_value=0.5),
        pytest.raises(RuntimeError, match="AY6Z363011Y.*已断开"),
    ):
        proxy._check_capture_state()
    assert proxy._latest_frame is None
    assert proxy._stop_event.is_set()


def test_proxy_rejects_stale_cache_even_when_worker_still_alive() -> None:
    proxy = _proxy()
    # Repeated sequence numbers must not refresh the watchdog.
    proxy._frame_queue.put((1, _frame()))
    with mock.patch.object(isolated_backend.time, "monotonic", return_value=5.0):
        proxy._drain_frames()
        proxy._check_capture_state()
    assert proxy._latest_frame is None
    assert not proxy._stop_event.is_set()
    with mock.patch.object(isolated_backend.time, "monotonic", return_value=5.76):
        with pytest.raises(RuntimeError, match="采集重试后"):
            proxy.capture_frame()
        with pytest.raises(RuntimeError, match="采集重试后"):
            proxy.wait_new_frame(0, 0)
    assert proxy._latest_frame is None
    assert proxy._stop_event.is_set()


def test_proxy_worker_error_remains_terminal_after_status_is_consumed() -> None:
    proxy = _proxy()
    proxy._status_queue.put(("error", "camera disconnected"))
    with pytest.raises(RuntimeError, match="camera disconnected"):
        proxy.capture_frame()
    with pytest.raises(RuntimeError, match="camera disconnected"):
        proxy.capture_frame()
    assert proxy._latest_frame is None


def test_failed_first_frame_never_reports_ready_and_cleans_up() -> None:
    camera = mock.Mock()
    camera.wait_new_frame.side_effect = RuntimeError("no valid first frame")
    frames, statuses = mock.Mock(), queue.Queue()
    stop = threading.Event()
    with (
        mock.patch.object(backend_orbbec, "OrbbecBackend", return_value=camera),
        mock.patch.object(isolated_backend, "_CAPTURE_RESTART_BACKOFF_SEC", 0.0),
        pytest.raises(RuntimeError, match="no valid first frame"),
    ):
        isolated_backend._capture_worker(OrbbecConfig(), frames, statuses, stop)
    assert statuses.get_nowait() == ("recovering", "0")
    assert statuses.get_nowait() == ("recovering", "0")
    assert statuses.get_nowait() == ("error", "no valid first frame")
    assert statuses.empty()
    assert camera.close.call_count == 3
    frames.cancel_join_thread.assert_not_called()
    frames.close.assert_called_once_with()


def test_worker_reopens_pipeline_and_keeps_sequence_monotonic_after_stall() -> None:
    first, second = mock.Mock(), mock.Mock()
    first.wait_new_frame.side_effect = [
        (_frame(), 1),
        RuntimeError("连续 5s 未收到完整有效帧"),
    ]
    stop = threading.Event()

    def recovered_frame(*_args, **_kwargs):
        stop.set()
        return _frame(), 1

    second.wait_new_frame.side_effect = recovered_frame
    frames, statuses = queue.Queue(), queue.Queue()
    frames.close = mock.Mock()
    frames.cancel_join_thread = mock.Mock()
    with (
        mock.patch.object(
            backend_orbbec, "OrbbecBackend", side_effect=[first, second]
        ) as build,
        mock.patch.object(isolated_backend, "_CAPTURE_RESTART_BACKOFF_SEC", 0.0),
    ):
        isolated_backend._capture_worker(OrbbecConfig(), frames, statuses, stop)

    assert build.call_count == 2
    assert [frames.get_nowait()[0], frames.get_nowait()[0]] == [1, 2]
    assert statuses.get_nowait() == ("ready", "")
    assert statuses.get_nowait() == ("recovering", "1")
    assert statuses.empty()
    first.close.assert_called_once_with()
    second.close.assert_called_once_with()


def test_proxy_drops_queued_frames_from_failed_pipeline() -> None:
    proxy = _proxy()
    proxy._status_queue.put(("recovering", "2"))
    proxy._frame_queue.put((2, _frame()))
    proxy._drain_status()
    proxy._drain_frames()
    assert proxy._latest_frame is None
    assert proxy._recover_min_seq == 2
    proxy._frame_queue.put((3, _frame()))
    proxy._drain_frames()
    assert proxy._latest_seq == 3
    assert proxy._latest_frame is not None
    assert proxy._recovering_until is None


def test_late_recovery_status_does_not_erase_newer_frame() -> None:
    proxy = _proxy()
    proxy._frame_queue.put((2, _frame()))
    proxy._drain_frames()
    proxy._status_queue.put(("recovering", "1"))
    proxy._drain_status()
    assert proxy._latest_seq == 2
    assert proxy._latest_frame is not None
    assert proxy._recovering_until is None


def test_first_frame_failure_reopens_before_reporting_ready() -> None:
    failed, healthy = mock.Mock(), mock.Mock()
    failed.wait_new_frame.side_effect = RuntimeError("no valid first frame")
    stop = threading.Event()

    def healthy_frame(*_args, **_kwargs):
        stop.set()
        return _frame(), 1

    healthy.wait_new_frame.side_effect = healthy_frame
    frames, statuses = queue.Queue(), queue.Queue()
    frames.close = mock.Mock()
    frames.cancel_join_thread = mock.Mock()
    with (
        mock.patch.object(
            backend_orbbec, "OrbbecBackend", side_effect=[failed, healthy]
        ),
        mock.patch.object(isolated_backend, "_CAPTURE_RESTART_BACKOFF_SEC", 0.0),
    ):
        isolated_backend._capture_worker(OrbbecConfig(), frames, statuses, stop)
    assert statuses.get_nowait() == ("recovering", "0")
    assert statuses.get_nowait() == ("ready", "")
    assert frames.get_nowait()[0] == 1
    failed.close.assert_called_once_with()
    healthy.close.assert_called_once_with()


def test_disconnected_device_does_not_restart() -> None:
    camera = mock.Mock()
    camera.wait_new_frame.side_effect = RuntimeError("device disconnected")
    frames, statuses = mock.Mock(), queue.Queue()
    stop = threading.Event()
    with (
        mock.patch.object(
            backend_orbbec, "OrbbecBackend", return_value=camera
        ) as build,
        pytest.raises(RuntimeError, match="device disconnected"),
    ):
        isolated_backend._capture_worker(OrbbecConfig(), frames, statuses, stop)
    build.assert_called_once()
    assert statuses.get_nowait() == ("error", "device disconnected")
    camera.close.assert_called_once_with()


def test_restart_budget_recovers_after_a_stable_stream() -> None:
    first, stable, third, recovered = (mock.Mock() for _ in range(4))
    first.wait_new_frame.side_effect = RuntimeError("first start failed")
    stable.wait_new_frame.side_effect = [
        (_frame(), 1),
        (_frame(), 2),
        RuntimeError("second stall"),
    ]
    third.wait_new_frame.side_effect = RuntimeError("third start failed")
    stop = threading.Event()

    def final_frame(*_args, **_kwargs):
        stop.set()
        return _frame(), 1

    recovered.wait_new_frame.side_effect = final_frame
    frames, statuses = queue.Queue(), queue.Queue()
    frames.close = mock.Mock()
    frames.cancel_join_thread = mock.Mock()
    with (
        mock.patch.object(
            backend_orbbec,
            "OrbbecBackend",
            side_effect=[first, stable, third, recovered],
        ) as build,
        mock.patch.object(isolated_backend, "_CAPTURE_RESTART_BACKOFF_SEC", 0.0),
        mock.patch.object(isolated_backend, "_CAPTURE_STABLE_RESET_SEC", 0.0),
    ):
        isolated_backend._capture_worker(OrbbecConfig(), frames, statuses, stop)

    assert build.call_count == 4
    assert [frames.get_nowait()[0] for _ in range(3)] == [1, 2, 3]
    assert [statuses.get_nowait()[0] for _ in range(4)] == [
        "recovering",
        "ready",
        "recovering",
        "recovering",
    ]


def _worker_with_pending_rgbd(frames, statuses, stop, cleaned):
    # Exercise the real worker/Queue feeder exit with a production-sized packet.
    camera = mock.Mock()
    frame = OrbbeFrame(
        np.zeros((720, 1280, 3), dtype=np.uint8),
        np.ones((720, 1280), dtype=np.float32),
        None,
    )

    def wait(*_args, **_kwargs):
        stop.set()
        return frame, 1

    camera.wait_new_frame.side_effect = wait
    camera.close.side_effect = cleaned.set
    with mock.patch.object(backend_orbbec, "OrbbecBackend", return_value=camera):
        isolated_backend._capture_worker(OrbbecConfig(), frames, statuses, stop)


def test_worker_exits_with_unread_large_frame_after_camera_cleanup() -> None:
    ctx = mp.get_context("spawn")
    frames, statuses = ctx.Queue(maxsize=2), ctx.Queue(maxsize=8)
    stop, cleaned = ctx.Event(), ctx.Event()
    worker = ctx.Process(
        target=_worker_with_pending_rgbd, args=(frames, statuses, stop, cleaned)
    )
    try:
        worker.start()
        assert statuses.get(timeout=15.0) == ("ready", "")
        assert cleaned.wait(5.0)
        worker.join(timeout=5.0)
        # Deliberately never drain the image queue.
        assert not worker.is_alive()
        assert worker.exitcode == 0
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=5.0)
        frames.close()
        statuses.close()
