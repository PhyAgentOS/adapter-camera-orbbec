import threading
from unittest import mock

from forge_devices_orbbec_camera import main
from forge_devices_orbbec_camera.config import OrbbecConfig


def test_ticks_publish_the_two_most_recent_frames_in_order(monkeypatch):
    backend = mock.Mock()
    payload = object()
    frames_queued = threading.Event()
    stopped = threading.Event()

    def wait_for_frame(after_seq, *, timeout):
        if after_seq < 3:
            return None, max(1, after_seq + 1)
        frames_queued.set()
        stopped.wait(2)
        return None, after_seq

    def encode_frame(frame, config, seq):
        return main.EncodedOutputs(seq, seq, seq, payload, None, None, None)

    def ticks():
        assert frames_queued.wait(2), "encoder did not queue pending frames"
        for _ in range(3):
            yield {"type": "INPUT", "id": "tick"}
        yield {"type": "STOP"}

    backend.wait_new_frame.side_effect = wait_for_frame
    backend.close.side_effect = stopped.set
    node = mock.MagicMock()
    node.__iter__.side_effect = ticks
    monkeypatch.setattr(main, "create_backend", lambda config: backend)
    monkeypatch.setattr(main, "_encode_frame", encode_frame)
    monkeypatch.setattr("dora.Node", lambda: node)
    if hasattr(main, "Observation"):
        monkeypatch.setattr(main.Observation, "from_env", lambda: None)
    publish = mock.Mock()
    monkeypatch.setattr(main, "_send_output", publish)
    config = OrbbecConfig()
    config.depth.enabled = False
    config.ir.enabled = False

    assert main.run_node(config) == 0
    assert [call.args[3] for call in publish.call_args_list] == [2, 3]
