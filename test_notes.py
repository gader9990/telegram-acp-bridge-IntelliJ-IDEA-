import tempfile
import unittest
from pathlib import Path
from bridge import Bridge
from notes import Notes


class NotesTests(unittest.TestCase):
    def test_notes_persist_without_agent_session(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'notes.local.json'
            sent, messages = [], []
            core = Bridge(sent.append, sent.append, lambda text, buttons=None: messages.append(text),
                          42, 42, notes=Notes(path))
            core.handle_text('✍ Добавить заметку')
            core.handle_text('Проверить торговлю NPC 🐈')
            core.handle_text('📒 Заметки')
            self.assertIn('Проверить торговлю NPC 🐈', messages[-1])
            self.assertEqual(Notes(path).read()[0]['text'], 'Проверить торговлю NPC 🐈')
            self.assertEqual(sent, [])

    def test_corrupt_file_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'notes.local.json'
            path.write_text('broken', encoding='utf-8')
            with self.assertRaises(ValueError):
                Notes(path).add('test')
            self.assertEqual(path.read_text(), 'broken')
