from unittest import mock

import pytest
from forge_devices_orbbec_camera import main
from forge_devices_orbbec_camera.config import OrbbecConfig


def test_failed_dora_registration_closes_open_capture_backend():
    backend = mock.Mock()
    with (
        mock.patch.object(main, "create_backend", return_value=backend),
        mock.patch("dora.Node", side_effect=RuntimeError("registration failed")),
        pytest.raises(RuntimeError, match="registration failed"),
    ):
        main.run_node(OrbbecConfig())
    backend.close.assert_called_once_with()


def test_encode_thread_start_failure_releases_backend_and_observer():
    backend, observation, thread = mock.Mock(), mock.Mock(), mock.Mock()
    thread.start.side_effect = RuntimeError("thread unavailable")
    with (
        mock.patch.object(main, "create_backend", return_value=backend),
        mock.patch("dora.Node"),
        mock.patch.object(main.Observation, "from_env", return_value=observation),
        mock.patch.object(main.threading, "Thread", return_value=thread),
        pytest.raises(RuntimeError, match="thread unavailable"),
    ):
        main.run_node(OrbbecConfig())
    backend.close.assert_called_once_with()
    observation.close.assert_called_once_with()
    thread.join.assert_not_called()


def test_encoder_that_survives_join_cannot_report_clean_shutdown():
    backend, thread = mock.Mock(), mock.Mock()
    thread.is_alive.return_value = True
    node = mock.MagicMock()
    node.__iter__.return_value = iter([{"type": "STOP"}])
    with (
        mock.patch.object(main, "create_backend", return_value=backend),
        mock.patch("dora.Node", return_value=node),
        mock.patch.object(main.Observation, "from_env", return_value=None),
        mock.patch.object(main.threading, "Thread", return_value=thread),
        pytest.raises(RuntimeError, match="encoder thread did not terminate"),
    ):
        main.run_node(OrbbecConfig())
    backend.close.assert_called_once_with()
