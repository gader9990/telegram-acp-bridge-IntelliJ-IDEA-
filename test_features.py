from datetime import datetime, timedelta, timezone
import threading
import unittest
from unittest.mock import Mock

from bridge import Bridge
from power import WindowsPower, parse_wake, filetime


class FeaturesTests(unittest.TestCase):
    def setUp(self):
        self.agent, self.ide, self.messages = [], [], []
        self.now = 10
        self.receipt = threading.Event()
        self.backend = Mock()
        self.core = Bridge(self.agent.append, self.ide.append, self.notify,
                           42, 42, clock=lambda: self.now, power=self.backend)
        for method, params, result in [
            ("initialize", {}, {}),
            ("session/new", {"cwd": "D:/mod"}, {"sessionId": "s1"}),
        ]:
            self.core.from_ide({"id": 1, "method": method, "params": params})
            self.core.from_agent({"id": self.agent[-1]["id"], "result": result})
        self.core.command("/use s1")

    def notify(self, text, buttons=None):
        self.messages.append(text)
        return self.receipt

    def finish(self, reason="end_turn"):
        self.core.from_agent({"id": self.core.sessions['s1'].active,
                              "result": {"stopReason": reason}})

    def sleep_command(self, after=False):
        wake = datetime.now().astimezone() + timedelta(hours=2)
        self.core.command(f"{'/sleep_after' if after else '/sleep'} {wake:%Y-%m-%d %H:%M}")

    def test_queue_runs_serially(self):
        self.core.command("first")
        first = self.agent[-1]['id']
        self.core.command("/queue second")
        self.core.tick()
        self.assertEqual(self.core.sessions['s1'].active, first)
        self.finish()
        self.core.tick()
        self.assertEqual(self.agent[-1]['params']['prompt'][0]['text'], 'second')
        self.assertEqual(self.core.jobs, [])

    def test_cancel_pauses_queue_and_removes_sleep(self):
        self.core.command("first")
        self.core.command("/queue second")
        self.sleep_command(after=True)
        self.core.command("/cancel")
        self.finish("cancelled")
        self.core.tick()
        self.assertTrue(self.core.queue_paused)
        self.assertIsNone(self.core.sleep_plan)
        self.assertIsNone(self.core.sessions['s1'].active)
        self.core.command("/resumequeue")
        self.core.tick()
        self.assertEqual(self.agent[-1]['params']['prompt'][0]['text'], 'second')

    def test_last_replays_completed_text(self):
        self.core.command("test")
        self.core.from_agent({"method":"session/update", "params": {"sessionId":"s1", "update": {
            "sessionUpdate":"agent_message_chunk", "content":{"type":"text","text":"Готово"}}}})
        self.finish()
        self.core.command("/last")
        self.assertEqual(self.messages[-1], "Готово")

    def test_sleep_requires_delivered_notice_and_countdown(self):
        self.sleep_command()
        self.core.tick()
        self.now += 100
        self.core.tick()
        self.backend.hibernate_until.assert_not_called()
        self.receipt.set()
        self.core.tick()
        self.assertEqual(self.core.sleep_plan['stage'], 'countdown')
        self.now += 14
        self.core.tick()
        self.backend.hibernate_until.assert_not_called()
        done = threading.Event()
        self.backend.hibernate_until.side_effect = lambda wake: done.set()
        self.now += 2
        self.core.tick()
        self.assertTrue(done.wait(1))
        self.backend.hibernate_until.assert_called_once()

    def test_sleep_after_waits_for_all_queued_work(self):
        self.core.command("first")
        self.core.command("/queue second")
        self.sleep_command(after=True)
        self.core.tick()
        self.assertEqual(self.core.sleep_plan['stage'], 'waiting')
        self.finish()
        self.core.tick()
        self.assertEqual(self.core.sleep_plan['stage'], 'waiting')
        self.finish()
        self.core.tick()
        self.assertEqual(self.core.sleep_plan['stage'], 'delivery')

    def test_sleep_now_refuses_active_task(self):
        self.core.command("task")
        self.sleep_command()
        self.assertIsNone(self.core.sleep_plan)

    def test_new_job_cancels_countdown(self):
        self.sleep_command()
        self.core.tick()
        self.core.command("/queue new work")
        self.assertIsNone(self.core.sleep_plan)

    def test_nosleep_cancels_without_power_calls(self):
        self.sleep_command()
        self.core.command("/nosleep")
        self.core.tick()
        self.backend.hibernate_until.assert_not_called()
        self.assertIsNone(self.core.sleep_plan)

    def test_preflight_failure_does_not_schedule_sleep(self):
        self.backend.preflight.side_effect = RuntimeError("Timers disabled")
        self.sleep_command()
        self.assertIsNone(self.core.sleep_plan)
        self.assertEqual(self.messages[-1], "Timers disabled")

    def test_foreign_user_cannot_schedule_sleep(self):
        self.core.telegram_update({"message":{"from":{"id":13},"chat":{"id":42,"type":"private"},
                                              "text":"/sleep 08:30"}})
        self.backend.preflight.assert_not_called()

    def test_queue_is_bound_to_session(self):
        self.core.command("/queue task")
        self.core.command("/release")
        self.assertEqual(self.core.selected, 's1')
        self.core.command("/clearqueue")
        self.core.command("/release")
        self.assertIsNone(self.core.selected)


class PowerTests(unittest.TestCase):
    def test_clock_time_rolls_to_tomorrow(self):
        now = datetime(2026, 10, 20, 23, 0, tzinfo=timezone(timedelta(hours=3)))
        self.assertEqual(parse_wake('08:30', now).day, 21)

    def test_past_date_and_short_interval_rejected(self):
        now = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
        for value in ('2026-10-19 12:00', '12:01', '25:00', 'nonsense'):
            with self.assertRaises(ValueError):
                parse_wake(value, now)

    def test_filetime_is_absolute_utc(self):
        utc = datetime(2026, 10, 20, 8, tzinfo=timezone.utc)
        msk = utc.astimezone(timezone(timedelta(hours=3)))
        self.assertEqual(filetime(utc), filetime(msk))
        self.assertGreater(filetime(utc), 0)
        self.assertEqual(filetime(datetime(1970,1,1,tzinfo=timezone.utc)), 116444736000000000)

    @unittest.skipUnless(__import__('os').name == 'nt', 'Windows ctypes last-error API')
    def test_timer_precedes_hibernation_and_is_closed_after_resume(self):
        backend = WindowsPower.__new__(WindowsPower)
        backend.kernel, backend.powr = Mock(), Mock()
        backend.preflight, backend.enable_shutdown_privilege = Mock(), Mock()
        backend.kernel.CreateWaitableTimerW.return_value = 123
        backend.kernel.SetWaitableTimer.return_value = True
        backend.powr.SetSuspendState.return_value = True
        calls = []
        backend.kernel.SetWaitableTimer.side_effect = lambda *args: calls.append('timer') or True
        backend.powr.SetSuspendState.side_effect = lambda *args: calls.append('sleep') or True
        backend.hibernate_until(datetime.now().astimezone() + timedelta(hours=1))
        self.assertEqual(calls, ['timer', 'sleep'])
        self.assertTrue(backend.kernel.SetWaitableTimer.call_args.args[-1])
        backend.powr.SetSuspendState.assert_called_once_with(True, False, False)
        backend.kernel.CloseHandle.assert_called_once_with(123)

    @unittest.skipUnless(__import__('os').name == 'nt', 'Windows ctypes last-error API')
    def test_failed_wake_timer_prevents_hibernation(self):
        backend = WindowsPower.__new__(WindowsPower)
        backend.kernel, backend.powr = Mock(), Mock()
        backend.preflight, backend.enable_shutdown_privilege = Mock(), Mock()
        backend.kernel.CreateWaitableTimerW.return_value = 123
        backend.kernel.SetWaitableTimer.return_value = False
        with self.assertRaises(RuntimeError):
            backend.hibernate_until(datetime.now().astimezone() + timedelta(hours=1))
        backend.powr.SetSuspendState.assert_not_called()
        backend.kernel.CloseHandle.assert_called_once_with(123)


if __name__ == '__main__':
    unittest.main()
