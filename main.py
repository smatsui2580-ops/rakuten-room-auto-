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
from src.room_follower import run_auto_follow
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


def _ci_auto_login() -> bool:
    """CI環境での自動ログイン（Cookieはリポジトリに保存しない）"""
    if os.getenv("CI") != "true":
        return True
    email = os.getenv("RAKUTEN_EMAIL")
    password = os.getenv("RAKUTEN_PASSWORD")
    if not email or not password:
        logger.error("CI環境: RAKUTEN_EMAIL / RAKUTEN_PASSWORD が未設定")
        return False
    from src.auto_login import auto_login_rakuten
    logger.info("CI環境: 自動ログイン実行...")
    ok = asyncio.run(auto_login_rakuten(email, password))
    if not ok:
        logger.error("自動ログイン失敗 → 処理を中断します")
    return ok


def run_auto_post(config: dict, test_mode: bool = False, skip_follow: bool = False):
    """商品検索 → キャプション生成 → ROOM投稿 を実行"""

    logger.info("=" * 50)
    logger.info(f"自動投稿開始: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if not _ci_auto_login():
        return

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

            if success is True:
                history.mark_as_posted(item.item_code)
                posted_count += 1
                logger.info(f"投稿成功 ({posted_count}/{max_posts})")
            elif success == "duplicate":
                history.mark_as_posted(item.item_code)
                logger.info(f"重複のため履歴に記録してスキップ: {item.item_name[:30]}...")
            else:
                logger.warning(f"投稿失敗: {item.item_name[:30]}...")

    logger.info(f"自動投稿完了: {posted_count}件投稿")

    # 自動フォロー
    follow_cfg = config.get("follow", {})
    if not skip_follow and follow_cfg.get("enabled", False):
        logger.info("--- 自動フォロー開始 ---")
        headless = True if os.getenv("CI") == "true" else browser_cfg["headless"]
        followed = asyncio.run(run_auto_follow(
            my_room_user_id=follow_cfg["my_room_user_id"],
            search_keywords=follow_cfg.get("search_keywords", search_cfg["keywords"]),
            max_follows=follow_cfg.get("max_follows", 30),
            action_delay=browser_cfg["action_delay"],
            headless=headless,
            refollow_days=follow_cfg.get("refollow_days", 30),
        ))
        logger.info(f"自動フォロー完了: {followed}件")

    logger.info("=" * 50)

    # スケジューラー実行時（CI以外）は投稿後にスリープ
    if not test_mode and os.getenv("CI") != "true" and "--now" not in sys.argv:
        logger.info("投稿完了 → 5分後にスリープします")
        import time
        time.sleep(300)
        os.system("pmset sleepnow")


def run_follow_only(config: dict):
    """フォローのみ実行"""
    load_dotenv()
    logger.info("=" * 50)
    logger.info(f"自動フォロー開始: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if not _ci_auto_login():
        return
    follow_cfg = config.get("follow", {})
    browser_cfg = config["browser"]
    search_cfg = config["search"]
    headless = True if os.getenv("CI") == "true" else browser_cfg["headless"]
    followed = asyncio.run(run_auto_follow(
        my_room_user_id=follow_cfg["my_room_user_id"],
        search_keywords=follow_cfg.get("search_keywords", search_cfg["keywords"]),
        max_follows=follow_cfg.get("max_follows", 30),
        action_delay=browser_cfg["action_delay"],
        headless=headless,
        refollow_days=follow_cfg.get("refollow_days", 30),
    ))
    logger.info(f"自動フォロー完了: {followed}件")
    logger.info("=" * 50)


def main():
    load_dotenv()
    config = load_config()

    args = sys.argv[1:]
    test_mode = "--test" in args
    post_only = "--post" in args
    follow_only = "--follow" in args
    run_now = "--now" in args or test_mode or post_only or follow_only

    if run_now:
        if follow_only:
            logger.info("フォローのみモード")
            run_follow_only(config)
        elif post_only:
            logger.info("投稿のみモード" + (" [テスト]" if test_mode else ""))
            run_auto_post(config, test_mode=test_mode, skip_follow=True)
        else:
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
