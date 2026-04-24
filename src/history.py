"""
投稿済み商品の履歴管理（重複投稿防止）
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

HISTORY_FILE = Path(__file__).parent.parent / "data" / "posted_items.json"


def load_history() -> dict:
    if not HISTORY_FILE.exists():
        return {}
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_history(history: dict):
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def is_already_posted(item_code: str, repost_interval_days: int) -> bool:
    history = load_history()
    if item_code not in history:
        return False
    posted_at = datetime.fromisoformat(history[item_code])
    return datetime.now() - posted_at < timedelta(days=repost_interval_days)


def mark_as_posted(item_code: str):
    history = load_history()
    history[item_code] = datetime.now().isoformat()
    save_history(history)
    logger.info(f"[履歴] {item_code} を投稿済みとして記録")
