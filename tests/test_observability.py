from __future__ import annotations

from unittest import mock

import pytest
from forge_common.metrics import BoundedMetricsSink
from forge_common.observability import Observer

from forge_devices_orbbec_camera import main as main_module
from forge_devices_orbbec_camera.config import OrbbecConfig
from forge_devices_orbbec_camera.observability import Observation


class FakeClock:
    def __init__(self, *wall_times: int) -> None:
        self._wall_times = iter(wall_times)
        self._monotonic_ns = 0

    def time_ns(self) -> int:
        return next(self._wall_times)

    def monotonic_ns(self) -> int:
        self._monotonic_ns += 1
        return self._monotonic_ns


@pytest.mark.parametrize("value", [None, "", "0", "true", "01"])
def test_observation_is_disabled_unless_environment_is_exactly_one(
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
) -> None:
    if value is None:
        monkeypatch.delenv("FORGE_OBSERVABILITY", raising=False)
    else:
        monkeypatch.setenv("FORGE_OBSERVABILITY", value)

    assert Observation.from_env() is None


def test_observation_is_enabled_by_exactly_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGE_OBSERVABILITY", "1")

    assert Observation.from_env() is not None


def test_each_output_is_a_distinct_new_origin_with_business_metadata() -> None:
    node = mock.Mock()
    sink = BoundedMetricsSink()
    observer = Observer(clock=FakeClock(100, 110, 120, 130), sink=sink)
    observation = Observation(sink=sink, observer=observer)

    outputs = (
        ("image/color", "orbbec_camera.color"),
        ("image/depth", "orbbec_camera.depth"),
        ("image/ir", "orbbec_camera.ir"),
        ("point_cloud", "orbbec_camera.point_cloud"),
    )
    for output_id, origin_id in outputs:
        observation.publish(
            node,
            output_id,
            mock.sentinel.payload,
            {"capture_timestamp_ns": 42, "frame_id": "camera_optical"},
            origin_id=origin_id,
        )

    metadata = [call.args[2] for call in node.send_output.call_args_list]
    assert [item["forge_origin_time_ns"] for item in metadata] == [100, 110, 120, 130]
    assert [item["forge_publish_time_ns"] for item in metadata] == [100, 110, 120, 130]
    assert [item["forge_origin_id"] for item in metadata] == [
        item[1] for item in outputs
    ]
    assert all(item["capture_timestamp_ns"] == 42 for item in metadata)
    assert all(item["frame_id"] == "camera_optical" for item in metadata)
    assert all(item["forge_obs_version"] == 1 for item in metadata)

    counters = sink.snapshot()["counters"]
    assert sum(item["count"] for item in counters if item["reason"] == "published") == 4


def test_observer_clock_failure_still_sends_business_metadata() -> None:
    node = mock.Mock()
    sink = BoundedMetricsSink()
    observer = Observer(clock=FakeClock(-1), sink=sink)
    observation = Observation(sink=sink, observer=observer)

    observation.publish(
        node,
        "image/color",
        mock.sentinel.payload,
        {"capture_timestamp_ns": 42},
        origin_id="orbbec_camera.color",
    )

    assert node.send_output.call_args.args[2] == {"capture_timestamp_ns": 42}


def test_send_failure_propagates_unchanged_and_is_counted() -> None:
    error = RuntimeError("send failed")
    node = mock.Mock()
    node.send_output.side_effect = error
    sink = BoundedMetricsSink()
    observer = Observer(clock=FakeClock(100), sink=sink)
    observation = Observation(sink=sink, observer=observer)

    with pytest.raises(RuntimeError) as caught:
        observation.publish(
            node,
            "image/color",
            mock.sentinel.payload,
            {},
            origin_id="orbbec_camera.color",
        )

    assert caught.value is error
    assert sink.snapshot()["counters"][0]["reason"] == "publish_failed"


class FakeEncodeThread:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.started = False
        self.joined = False

    def start(self) -> None:
        self.started = True

    def join(self, timeout: float | None = None) -> None:
        self.joined = True


def test_observability_start_failure_best_effort_closes_candidate() -> None:
    backend = mock.Mock()
    node = mock.Mock()
    node.__iter__ = mock.Mock(return_value=iter([{"type": "STOP"}]))
    candidate = mock.Mock()
    candidate.start.side_effect = RuntimeError("start failed")
    candidate.close.side_effect = RuntimeError("close failed")

    with (
        mock.patch.object(main_module, "create_backend", return_value=backend),
        mock.patch("dora.Node", return_value=node),
        mock.patch.object(main_module.threading, "Thread", FakeEncodeThread),
        mock.patch.object(main_module.Observation, "from_env", return_value=candidate),
    ):
        assert main_module.run_node(OrbbecConfig()) == 0

    candidate.close.assert_called_once_with()
    backend.close.assert_called_once_with()


def test_cleanup_continues_after_backend_close_failure() -> None:
    backend = mock.Mock()
    backend.close.side_effect = RuntimeError("backend close failed")
    node = mock.Mock()
    node.__iter__ = mock.Mock(return_value=iter([{"type": "STOP"}]))
    observation = mock.Mock()
    thread = FakeEncodeThread()

    with (
        mock.patch.object(main_module, "create_backend", return_value=backend),
        mock.patch("dora.Node", return_value=node),
        mock.patch.object(main_module.threading, "Thread", return_value=thread),
        mock.patch.object(main_module.Observation, "from_env", return_value=observation),
        pytest.raises(RuntimeError, match="backend close failed"),
    ):
        main_module.run_node(OrbbecConfig())

    assert thread.joined
    observation.close.assert_called_once_with()


def test_primary_exception_is_preserved_while_all_cleanup_runs() -> None:
    primary = ValueError("node failed")
    backend = mock.Mock()
    backend.close.side_effect = RuntimeError("backend close failed")
    node = mock.Mock()
    node.__iter__ = mock.Mock(side_effect=primary)
    observation = mock.Mock()
    thread = FakeEncodeThread()

    with (
        mock.patch.object(main_module, "create_backend", return_value=backend),
        mock.patch("dora.Node", return_value=node),
        mock.patch.object(main_module.threading, "Thread", return_value=thread),
        mock.patch.object(main_module.Observation, "from_env", return_value=observation),
        pytest.raises(ValueError) as raised,
    ):
        main_module.run_node(OrbbecConfig())

    assert raised.value is primary
    assert thread.joined
    observation.close.assert_called_once_with()


def test_metrics_failure_does_not_block_send() -> None:
    sink = mock.Mock()
    sink.increment.side_effect = RuntimeError("metrics failed")
    node = mock.Mock()
    observer = Observer(clock=FakeClock(100), sink=sink)
    observation = Observation(sink=sink, observer=observer)

    observation.publish(
        node,
        "image/color",
        mock.sentinel.payload,
        {},
        origin_id="orbbec_camera.color",
    )

    node.send_output.assert_called_once()
