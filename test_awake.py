import unittest
from unittest.mock import Mock
from power import WindowsPower


class AwakeTests(unittest.TestCase):
    def test_system_only_request_and_release(self):
        power = WindowsPower.__new__(WindowsPower)
        power.kernel = Mock()
        power.kernel.SetThreadExecutionState.return_value = 0x80000000
        power.keep_awake(True)
        power.kernel.SetThreadExecutionState.assert_called_with(0x80000001)
        power.keep_awake(False)
        power.kernel.SetThreadExecutionState.assert_called_with(0x80000000)
