"""
Claude APIを使って商品キャプションを自動生成するモジュール
"""

import anthropic
import logging
from .rakuten_api import RakutenItem

logger = logging.getLogger(__name__)


class CaptionGenerator:
    def __init__(self, api_key: str, tone: str, max_length: int, add_hashtags: bool, hashtag_count: int):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.tone = tone
        self.max_length = max_length
        self.add_hashtags = add_hashtags
        self.hashtag_count = hashtag_count

    def generate(
        self,
        item: RakutenItem,
        opening_style: str = "",
        avoid_openings: list = None,
        sale_note: str = "",
    ) -> str:
        """商品情報からROOM投稿用キャプションを生成する

        opening_style: 書き出しの型の説明（毎回変えて同じ文が並ばないようにする）
        avoid_openings: 最近使った書き出し（似た文を避けさせる）
        sale_note: セール時に添える事実（例: 5と0のつく日）
        """

        hashtag_instruction = (
            f"\n- 最後に関連するハッシュタグを{self.hashtag_count}個追加してください（例: #コスメ #おすすめ）"
            if self.add_hashtags else ""
        )

        # 買う理由になる事実（APIで取れたものだけ）
        facts = []
        if item.point_rate and item.point_rate > 1:
            facts.append(f"ポイント{item.point_rate}倍")
        if item.postage_free:
            facts.append("送料込み")
        facts_line = "・".join(facts) if facts else "なし"

        opening_instruction = opening_style or "絵本のような世界観・情景が浮かぶ書き出し"
        avoid_block = ""
        if avoid_openings:
            listed = "\n".join(f"  ・{o}" for o in avoid_openings[-8:])
            avoid_block = f"\n- 次の書き出しは最近使ったので、似た言い回し・同じ単語での始まりは避ける:\n{listed}"
        sale_block = f"\n- 締めの近くで「{sale_note}」に一言だけ自然にふれる" if sale_note else ""

        prompt = f"""楽天ROOMの投稿キャプションを作成してください。

【商品情報】
- 商品名: {item.item_name}
- 価格: ¥{item.item_price:,}
- ショップ: {item.shop_name}
- レビュー: {item.review_average}点（{item.review_count}件）
- キャッチコピー: {item.catch_copy}
- 商品説明（抜粋）: {item.item_caption}
- お得情報: {facts_line}

【文章の構成（この順番で書く）】
1. 書き出し（1〜2行）: {opening_instruction}
2. 商品の見た目・素材・使い心地の描写（2〜3行）
3. 🌟おすすめポイント を箇条書き（3〜4個、「・」で始める）。サイズ・容量・素材・使える場面など、買う判断に役立つ具体的な情報を優先する
4. 締めの一言（自分へのご褒美・贈り物・日常を豊かに、など）

【ルール】
- トーン: {self.tone}
- 文字数: {self.max_length}文字以内（ハッシュタグ含む）
- 絵文字を自然に使う（1行に1〜2個程度）
- 「♡」「✨」「☕」「🌿」などほっこり系の絵文字が合う
- 過剰な誇張や嘘の情報は書かない。商品情報にない数字・効果・受賞歴は書かない
- 「お得情報」が「なし」以外なら、その内容を1か所だけ入れる（書かれていない特典は書かない）{avoid_block}{sale_block}{hashtag_instruction}

キャプションのみ出力してください（前置きや説明文は不要）。"""

        try:
            message = self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            caption = message.content[0].text.strip()
            logger.info(f"[キャプション生成] {item.item_name[:30]}... → {len(caption)}文字")
            return caption
        except Exception as e:
            logger.error(f"キャプション生成失敗: {e}")
            # フォールバック：シンプルなキャプション
            return f"{item.item_name}\n¥{item.item_price:,}\n\nレビュー{item.review_average}点・{item.review_count}件の人気商品です！"
