import http.client
import unittest
from unittest.mock import Mock, patch

from bridge import Telegram, TelegramError, TelegramWorker


class ReconnectTests(unittest.TestCase):
    def test_interrupted_http_response_becomes_retryable_error(self):
        for error in (http.client.IncompleteRead(b'partial'),
                      http.client.BadStatusLine('private response')):
            with self.subTest(error=type(error).__name__):
                with patch('bridge.urllib.request.urlopen', side_effect=error):
                    with self.assertRaises(TelegramError) as raised:
                        Telegram('fake:secret').call('getUpdates')
                self.assertEqual(raised.exception.code, 0)
                self.assertNotIn('private', str(raised.exception))

    def test_poll_recovers_after_conflict_preserving_offset(self):
        api, core, stop = Mock(), Mock(), Mock()
        stop.is_set.side_effect = [False, False, False, True]
        stop.wait.return_value = False
        api.call.side_effect = [[{'update_id': 17}], TelegramError(409), []]
        worker = TelegramWorker(api, core, 42, stop)
        worker.poll_loop()
        self.assertEqual([c.kwargs['offset'] for c in api.call.call_args_list], [0, 18, 18])
        stop.wait.assert_called_once_with(30)

    def test_sender_retries_same_message(self):
        api, stop = Mock(), Mock()
        stop.is_set.side_effect = [False, False, False, True]
        stop.wait.return_value = False
        api.call.side_effect = [TelegramError(), True]
        worker = TelegramWorker(api, None, 42, stop)
        receipt = worker.notify('hello')
        worker.send_loop()
        self.assertTrue(receipt.is_set())
        self.assertEqual(api.call.call_args_list[0], api.call.call_args_list[1])
