"""Живой прогон: cd services && uv run python -m gov [год]"""
import json
import sys
from datetime import date

from .service import snapshot

year = int(sys.argv[1]) if len(sys.argv) > 1 else date.today().year
print(json.dumps(snapshot(year), ensure_ascii=False, indent=2))
