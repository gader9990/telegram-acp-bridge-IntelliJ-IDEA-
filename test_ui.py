import json
from pathlib import Path
import struct
import threading
import unittest
from unittest.mock import Mock, patch
import zlib

from bridge import Bridge, Session, Telegram, TelegramWorker, MAIN_KEYBOARD, multipart_photo
from screenshot import png_from_bgra


class UiTests(unittest.TestCase):
    def setUp(self):
        self.sent, self.messages = [], []
        self.core = Bridge(self.sent.append, lambda _: None,
                           lambda text, buttons=None: self.messages.append((text, buttons)), 42, 42)
        self.core.ready = True
        self.core.sessions['one'] = Session()
        self.core.selected = 'one'

    def test_compact_and_busy_guard(self):
        self.core.handle_text('🧹 Сжать контекст')
        self.assertEqual(self.sent[-1]['params']['prompt'][0]['text'], '/compact')
        count = len(self.sent)
        self.core.handle_text('🧹 Сжать контекст')
        self.assertEqual(len(self.sent), count)

    def test_guided_queue(self):
        self.core.handle_text('➕ В очередь')
        self.core.handle_text('исправь мод')
        self.assertEqual(self.core.jobs, [('one', 'исправь мод')])

    def test_menu_cancels_input(self):
        self.core.handle_text('🌙 Уснуть до…')
        self.core.handle_text('↩ Отмена ввода')
        self.assertIsNone(self.core.input_mode)
        self.assertEqual(self.messages[-1][1], MAIN_KEYBOARD)

    def test_screenshot_rate_limit(self):
        captured = threading.Event()
        self.core.screenshot = Mock(return_value=b'png')
        self.core.send_photo = lambda data, caption: captured.set()
        self.core.command('/screenshot')
        self.assertTrue(captured.wait(2))
        self.core.command('/screenshot')
        self.core.screenshot.assert_called_once()

    def test_untrusted_user_cannot_capture(self):
        self.core.screenshot = Mock()
        self.core.telegram_update({'message': {'from': {'id': 7}, 'chat': {'id': 42},
                                               'text': '/screenshot'}})
        self.core.screenshot.assert_not_called()

    def test_worker_markup_and_photo(self):
        worker = TelegramWorker(None, None, 42, threading.Event())
        worker.notify('menu', MAIN_KEYBOARD)
        self.assertEqual(worker.outbox.get()[1]['reply_markup'], MAIN_KEYBOARD)
        buttons = [[{'text': 'OK', 'callback_data': 'ok'}]]
        worker.notify('pick', buttons)
        self.assertEqual(worker.outbox.get()[1]['reply_markup'], {'inline_keyboard': buttons})
        worker.photo(b'png', 'screen')
        method, params, _ = worker.outbox.get()
        self.assertEqual((method, params['_photo']), ('sendPhoto', b'png'))

    def test_png_pixel_order(self):
        png = png_from_bgra(1, 1, b'\x01\x02\x03\xff')
        self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
        size = struct.unpack('!I', png[33:37])[0]
        self.assertEqual(zlib.decompress(png[41:41 + size]), b'\x00\x03\x02\x01')

    def test_multipart_and_curl_cleanup(self):
        params = {'chat_id': 42, '_photo': b'\x00\xffPNG', 'reply_markup': MAIN_KEYBOARD}
        body, kind = multipart_photo(params)
        self.assertIn(params['_photo'], body)
        self.assertIn(b'name="photo"; filename="screenshot.png"', body)
        self.assertIn(json.dumps(MAIN_KEYBOARD, ensure_ascii=False).encode(), body)
        api = Telegram('FAKE_SECRET')
        paths = []
        def run(config):
            self.assertIn('Content-Type: multipart/form-data;', config)
            line = next(line for line in config.splitlines() if line.startswith('data-binary = '))
            path = Path(json.loads(line.split(' = ', 1)[1])[1:])
            paths.append(path)
            self.assertIn(params['_photo'], path.read_bytes())
            self.assertNotIn(b'FAKE_SECRET', path.read_bytes())
            return True
        with patch.object(api, 'run_curl', side_effect=run):
            self.assertTrue(api.call_curl('sendPhoto', params))
        self.assertFalse(paths[0].exists())


if __name__ == '__main__':
    unittest.main()
