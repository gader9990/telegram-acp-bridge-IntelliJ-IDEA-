"""Local notes, independent of agent context."""
import json
from datetime import datetime
from pathlib import Path


class Notes:
    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding='utf-8'))
        if not isinstance(data, list) or not all(isinstance(x, dict) and
                isinstance(x.get('text'), str) and isinstance(x.get('date'), str) for x in data):
            raise ValueError('Invalid notes file')
        return data

    def add(self, text):
        data = self.read()
        if len(data) >= 500 or len(text) > 8000:
            raise ValueError('Notes limit reached')
        data.append({'date': datetime.now().astimezone().isoformat(timespec='minutes'), 'text': text})
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.path)

