"""
楽天ROOM自動投稿 メインスクリプト

使い方:
  python main.py          # スケジューラーを起動（常時起動モード）
  python main.py --now    # 今すぐ1回だけ実行
  python main.py --test   # テストモード（ブラウザ投稿はスキップ）
"""

import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import yaml
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

from src.rakuten_api import RakutenAPI
from src.caption_generator import CaptionGenerator
from src.room_poster import post_to_room
from src import history

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/auto_post.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def load_config() -> dict:
    config_path = Path(__file__).parent / "config.yaml"
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def run_auto_post(config: dict, test_mode: bool = False):
    """商品検索 → キャプション生成 → ROOM投稿 を実行"""

    logger.info("=" * 50)
    logger.info(f"自動投稿開始: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # 環境変数ロード
    rakuten_app_id = os.getenv("RAKUTEN_APP_ID")
    rakuten_access_key = os.getenv("RAKUTEN_ACCESS_KEY")
    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY")
    if not all([rakuten_app_id, rakuten_access_key, anthropic_api_key]):
        logger.error(".env ファイルの設定が不足しています。.env.example を参考に設定してください。")
        return

    # モジュール初期化
    rakuten_api = RakutenAPI(app_id=rakuten_app_id, access_key=rakuten_access_key)
    caption_gen = CaptionGenerator(
        api_key=anthropic_api_key,
        tone=config["caption"]["tone"],
        max_length=config["caption"]["max_length"],
        add_hashtags=config["caption"]["add_hashtags"],
        hashtag_count=config["caption"]["hashtag_count"],
    )

    search_cfg = config["search"]
    posting_cfg = config["posting"]
    browser_cfg = config["browser"]

    posted_count = 0
    max_posts = posting_cfg["max_posts_per_day"]

    for keyword in search_cfg["keywords"]:
        if posted_count >= max_posts:
            logger.info(f"本日の投稿上限 {max_posts}件 に達しました")
            break

        items = rakuten_api.search_items(
            keyword=keyword,
            hits=search_cfg["items_per_search"],
            min_price=search_cfg.get("min_price"),
            max_price=search_cfg.get("max_price"),
            min_review_count=search_cfg.get("min_review_count", 0),
            min_review_average=search_cfg.get("min_review_average", 0.0),
            sort=search_cfg.get("sort", "-reviewCount"),
        )

        for item in items:
            if posted_count >= max_posts:
                break

            # 重複チェック
            if history.is_already_posted(item.item_code, posting_cfg["repost_interval_days"]):
                logger.info(f"スキップ（投稿済み）: {item.item_name[:30]}...")
                continue

            # キャプション生成
            caption = caption_gen.generate(item)
            logger.info(f"\n【投稿予定】\n商品: {item.item_name[:50]}\n価格: ¥{item.item_price:,}\nキャプション:\n{caption}\n")

            if test_mode:
                logger.info("[テストモード] ブラウザ投稿はスキップします")
                history.mark_as_posted(item.item_code)
                posted_count += 1
                continue

            # ROOM投稿
            # CI環境（GitHub Actions）では強制headless
            headless = True if os.getenv("CI") == "true" else browser_cfg["headless"]
            success = asyncio.run(post_to_room(
                item_url=item.item_url,
                caption=caption,
                item_code=item.item_code,
                action_delay=browser_cfg["action_delay"],
                headless=headless,
            ))

            if success:
                history.mark_as_posted(item.item_code)
                posted_count += 1
                logger.info(f"投稿成功 ({posted_count}/{max_posts})")
            else:
                logger.warning(f"投稿失敗: {item.item_name[:30]}...")

    logger.info(f"自動投稿完了: {posted_count}件投稿")
    logger.info("=" * 50)


def main():
    load_dotenv()
    config = load_config()

    args = sys.argv[1:]
    test_mode = "--test" in args
    run_now = "--now" in args or test_mode

    if run_now:
        logger.info("手動実行モード" + (" [テスト]" if test_mode else ""))
        run_auto_post(config, test_mode=test_mode)
        return

    # スケジューラー起動
    scheduler = BlockingScheduler(timezone="Asia/Tokyo")
    schedule_times = config["posting"]["schedule_times"]

    for time_str in schedule_times:
        hour, minute = map(int, time_str.split(":"))
        scheduler.add_job(
            run_auto_post,
            trigger="cron",
            hour=hour,
            minute=minute,
            args=[config, False],
            id=f"post_{time_str}",
        )
        logger.info(f"スケジュール登録: {time_str}")

    logger.info("スケジューラー起動中... Ctrl+C で停止")
    try:
        scheduler.start()
    except KeyboardInterrupt:
        logger.info("スケジューラーを停止しました")


if __name__ == "__main__":
    main()
