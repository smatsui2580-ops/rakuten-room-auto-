"""
商品選びと投稿の出し方を決めるモジュール

- 売れている実績のある商品を優先する（検索上位・レビュー件数・評価・ポイント倍率）
- セール期間（5と0のつく日／お買い物マラソン等）を判定して投稿を寄せる
- 同じキーワード・同じショップ・同じ書き出しが続かないようにする
"""

from __future__ import annotations

import json
import logging
import math
import random
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

STATE_FILE = Path(__file__).parent.parent / "data" / "post_state.json"
JST = ZoneInfo("Asia/Tokyo")

# 商品名にこの語が入っている商品が多ければ、セール開催中とみなす
SALE_WORDS = ["マラソン", "スーパーSALE", "スーパーセール", "ブラックフライデー", "BLACK FRIDAY", "大感謝祭", "買い回り", "買いまわり"]

# キャプションの書き出しの型（毎回変えて、同じ文が並ばないようにする）
OPENING_STYLES = [
    ("情景", "その商品がある暮らしの情景がふっと浮かぶ一文で始める"),
    ("問いかけ", "読む人への短い問いかけで始める（例:「〜って思うこと、ありませんか？」）"),
    ("悩み解決", "ありがちな小さな悩みを一言で挙げ、この商品で解決できると続ける"),
    ("贈り物", "誰かに贈る場面（友人・家族・自分へのご褒美）を思い浮かべる一文で始める"),
    ("季節", "今の季節や行事にふれる一文で始める"),
    ("実績", "レビュー件数や評価の高さなど、選ばれている事実から始める"),
    ("第一印象", "見た瞬間の素直な感想（「ひとめぼれ」など）で始める"),
]


# ---------- 状態ファイル ----------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def record_post(state: dict, keyword: str, shop: str, style: str, caption: str):
    """投稿した内容を記録する（次回以降のかぶり防止に使う）"""
    first_line = next((ln.strip() for ln in caption.splitlines() if ln.strip()), "")[:40]
    for key, value, keep in [
        ("recent_keywords", keyword, 24),
        ("recent_shops", shop, 8),
        ("recent_styles", style, 3),
        ("recent_openings", first_line, 12),
    ]:
        if not value:
            continue
        lst = state.setdefault(key, [])
        lst.append(value)
        del lst[:-keep]
    save_state(state)


# ---------- セール判定 ----------

def today_jst() -> date:
    return datetime.now(JST).date()


def is_point_day(d: date | None = None) -> bool:
    """5と0のつく日かどうか"""
    d = d or today_jst()
    return d.day % 5 == 0


def in_sale_period(sale_periods: list | None, d: date | None = None) -> bool:
    """config の sale_periods（[開始日, 終了日] の並び）に今日が入っているか"""
    d = d or today_jst()
    for period in sale_periods or []:
        try:
            start = date.fromisoformat(str(period[0]))
            end = date.fromisoformat(str(period[1]))
        except Exception:
            continue
        if start <= d <= end:
            return True
    return False


def has_sale_word(name: str) -> bool:
    upper = (name or "").upper()
    return any(w.upper() in upper for w in SALE_WORDS)


def sale_word_ratio(items: list) -> float:
    """検索結果のうち、商品名にセールの語が入っている割合"""
    if not items:
        return 0.0
    return sum(1 for it in items if has_sale_word(it.item_name)) / len(items)


# ---------- キーワード選び ----------

def order_keywords(base: list[str], seasonal: list[str], state: dict) -> list[str]:
    """キーワードを並べる。最近使ったものは後ろへ回し、季節のものを前のほうに混ぜる"""
    recent = state.get("recent_keywords", [])
    # 直近で使ったキーワードほど後ろへ（全キーワードの半分までを「最近」とみなす）
    window = recent[-max(1, (len(base) + len(seasonal)) // 2):]

    def split(words):
        words = list(dict.fromkeys(words))
        random.shuffle(words)
        fresh = [w for w in words if w not in window]
        used = sorted((w for w in words if w in window), key=lambda w: window.index(w))
        return fresh, used

    base_fresh, base_used = split(base)
    sea_fresh, sea_used = split(seasonal)

    # 新しいキーワードを先に。季節ものと通常ものを交互に並べる
    ordered = []
    a, b = (sea_fresh, base_fresh) if random.random() < 0.5 else (base_fresh, sea_fresh)
    while a or b:
        if a:
            ordered.append(a.pop(0))
        if b:
            ordered.append(b.pop(0))
    return ordered + sea_used + base_used


# ---------- 商品の点数づけ ----------

def score_item(item, sale_mode: bool) -> float:
    """買われやすさの目安。大きいほど優先する"""
    score = 0.0
    # レビュー件数＝実際に売れてきた量の目安（10件→2点、100件→4点、1000件→6点）
    score += 2.0 * math.log10(item.review_count + 1)
    # 評価（4.0を基準に、4.5で+1.5点）
    score += 3.0 * (item.review_average - 4.0)
    # ポイント倍率（2倍以上は買う理由になる）
    score += 0.5 * min(max(item.point_rate - 1, 0), 9)
    # 送料込みは買いやすい
    if item.postage_free:
        score += 1.0
    # 手を出しやすい価格帯
    if 1000 <= item.item_price <= 5000:
        score += 1.0
    # セール中は、セール対象とうたっている商品を優先
    if sale_mode and has_sale_word(item.item_name):
        score += 2.5
    return score


def pick_best(items: list, sale_mode: bool, is_posted, recent_shops: list[str]):
    """未投稿の候補から1つ選ぶ。上位3つの中からランダム（毎回同じ商品にならないように）"""
    fresh = [it for it in items if not is_posted(it.item_code)]
    if not fresh:
        return None
    # 直近と同じショップは避ける（ほかに候補がある場合）
    other_shop = [it for it in fresh if it.shop_name not in recent_shops]
    pool = other_shop or fresh
    pool.sort(key=lambda it: score_item(it, sale_mode), reverse=True)
    return random.choice(pool[:3])


# ---------- キャプションの型 ----------

def pick_opening_style(state: dict) -> tuple[str, str]:
    recent = state.get("recent_styles", [])
    choices = [s for s in OPENING_STYLES if s[0] not in recent] or OPENING_STYLES
    return random.choice(choices)
