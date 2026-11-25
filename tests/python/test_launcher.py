import socket
import subprocess
import unittest
from unittest.mock import Mock, patch
from afterhours import run_afterhours as launcher


class LauncherTests(unittest.TestCase):
    def test_existing_listener_is_preserved(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            with patch.object(launcher, 'SERVICES', (('fixture', 'none.py', listener.getsockname()[1], '/'),)):
                with self.assertRaisesRegex(RuntimeError, 'already in use'):
                    launcher.check_ports()
            self.assertGreater(listener.fileno(), -1)

    def test_failed_child_is_reported_before_readiness(self):
        child = Mock()
        child.poll.return_value = 1
        with self.assertRaisesRegex(RuntimeError, 'stopped during startup'):
            launcher.wait_ready([child])

    def test_readiness_requires_the_expected_service_identity(self):
        child = Mock()
        child.poll.return_value = None
        service = ('music', 'afterhours.server', 9876, '/api/config')
        with patch.object(launcher, 'SERVICES', (service,)), \
             patch.object(launcher, 'read_json', side_effect=[{}, {'service': 'afterhours-music'}]) as read, \
             patch.object(launcher.time, 'sleep') as sleep:
            launcher.wait_ready([child])
        self.assertEqual(read.call_count, 2)
        sleep.assert_called_once()

    def test_cleanup_only_signals_running_children(self):
        active, stopped = Mock(), Mock()
        active.poll.return_value = None
        stopped.poll.return_value = 0
        launcher.stop_children([active, stopped])
        active.send_signal.assert_called_once()
        stopped.send_signal.assert_not_called()
        active.wait.assert_called_once_with(timeout=5)

    def test_cleanup_terminates_unresponsive_child(self):
        child = Mock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired('fixture', 5), 0]
        launcher.stop_children([child])
        child.terminate.assert_called_once()
        child.kill.assert_not_called()
