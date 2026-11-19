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
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

from src.rakuten_api import RakutenAPI
from src.caption_generator import CaptionGenerator
from src.room_poster import post_to_room
from src.room_follower import run_auto_follow
from src.room_liker import run_auto_like
from src import history
from src import selector

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


def run_auto_post(config: dict, test_mode: bool = False, skip_follow: bool = False):
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

    state = selector.load_state()
    headless = True if os.getenv("CI") == "true" else browser_cfg["headless"]

    # 季節のキーワード（今月の分）を通常のキーワードに混ぜ、最近使ったものは後ろへ回す
    month = selector.today_jst().month
    seasonal = (search_cfg.get("seasonal_keywords") or {}).get(month, [])
    keywords = selector.order_keywords(list(search_cfg["keywords"]), list(seasonal), state)

    # セール判定その1: 5と0のつく日／config で指定した期間
    point_day = selector.is_point_day()
    sale_mode = point_day or selector.in_sale_period(posting_cfg.get("sale_periods"))
    sale_checked = False

    normal_posts = posting_cfg.get("posts_per_run", posting_cfg.get("max_posts_per_day", 1))
    sale_posts = posting_cfg.get("sale_posts_per_run", normal_posts)
    max_posts = sale_posts if sale_mode else normal_posts
    gap_minutes = posting_cfg.get("gap_minutes", [20, 30])
    max_pages = search_cfg.get("max_pages", 3)

    posted_count = 0

    def is_posted(code: str) -> bool:
        return history.is_already_posted(code, posting_cfg["repost_interval_days"])

    for keyword in keywords:
        if posted_count >= max_posts:
            break

        # 検索の上位（1ページ目）から順に見て、未投稿の候補が出るまでページを進める
        item = None
        for page in range(1, max_pages + 1):
            items = rakuten_api.search_items(
                keyword=keyword,
                hits=30,
                min_price=search_cfg.get("min_price"),
                max_price=search_cfg.get("max_price"),
                min_review_count=search_cfg.get("min_review_count", 0),
                min_review_average=search_cfg.get("min_review_average", 0.0),
                sort=search_cfg.get("sort", "standard"),
                page=page,
            )
            if not items:
                break

            # セール判定その2: 検索結果の商品名にセールの語が多ければ開催中とみなす（初回だけ判定）
            if not sale_checked:
                sale_checked = True
                ratio = selector.sale_word_ratio(items)
                logger.info(f"セール語の割合: {ratio:.0%}（5と0のつく日: {point_day}）")
                if not sale_mode and ratio >= posting_cfg.get("sale_word_threshold", 0.2):
                    sale_mode = True
                    max_posts = sale_posts
                logger.info(f"セールモード: {sale_mode} → 今回の投稿数 {max_posts}件")

            item = selector.pick_best(items, sale_mode, is_posted, state.get("recent_shops", []))
            if item:
                break
            logger.info(f"「{keyword}」{page}ページ目は全て投稿済み → 次のページへ")

        if not item:
            logger.info(f"「{keyword}」は候補なし → 次のキーワードへ")
            continue

        # 2件目以降は間隔を空ける（同じ時刻に連投するとフィードが埋まり、読まれにくい）
        if posted_count > 0 and not test_mode and os.getenv("CI") == "true":
            wait_min = random.uniform(gap_minutes[0], gap_minutes[1])
            logger.info(f"次の投稿まで {wait_min:.0f}分 待機")
            time.sleep(wait_min * 60)

        # キャプション生成（書き出しの型を毎回変える）
        style_name, style_desc = selector.pick_opening_style(state)
        sale_note = "5と0のつく日" if point_day else ""
        caption = caption_gen.generate(
            item,
            opening_style=style_desc,
            avoid_openings=state.get("recent_openings", []),
            sale_note=sale_note,
        )
        logger.info(
            f"\n【投稿予定】キーワード: {keyword} / 書き出し: {style_name}\n"
            f"商品: {item.item_name[:50]}\n価格: ¥{item.item_price:,} / レビュー {item.review_average}点 {item.review_count}件"
            f" / ポイント{item.point_rate}倍 / 点数 {selector.score_item(item, sale_mode):.1f}\n"
            f"キャプション:\n{caption}\n"
        )

        if test_mode:
            logger.info("[テストモード] ブラウザ投稿はスキップします")
            posted_count += 1
            state.setdefault("recent_keywords", []).append(keyword)
            continue

        success = asyncio.run(post_to_room(
            item_url=item.item_url,
            caption=caption,
            item_code=item.item_code,
            item_name=item.item_name,
            action_delay=browser_cfg["action_delay"],
            headless=headless,
        ))

        if success is True:
            history.mark_as_posted(item.item_code)
            selector.record_post(state, keyword, item.shop_name, style_name, caption)
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
        time.sleep(300)
        os.system("pmset sleepnow")


def run_like(config: dict):
    """自動いいね（ランク条件の「検索」「いいね」を満たすための少量いいね）"""
    like_cfg = config.get("like", {})
    if not like_cfg.get("enabled", False):
        return
    browser_cfg = config["browser"]
    headless = True if os.getenv("CI") == "true" else browser_cfg["headless"]
    logger.info("--- 自動いいね開始 ---")
    liked = asyncio.run(run_auto_like(
        my_room_user_id=config["follow"]["my_room_user_id"],
        search_keywords=like_cfg.get("search_keywords") or config["search"]["keywords"],
        max_likes=like_cfg.get("max_likes", 5),
        action_delay=browser_cfg["action_delay"],
        headless=headless,
    ))
    logger.info(f"自動いいね完了: {liked}件")


def run_follow_only(config: dict):
    """フォローのみ実行"""
    load_dotenv()
    logger.info("=" * 50)
    logger.info(f"自動フォロー開始: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
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
    run_like(config)
    logger.info("=" * 50)


def main():
    load_dotenv()
    config = load_config()

    args = sys.argv[1:]
    test_mode = "--test" in args
    post_only = "--post" in args
    follow_only = "--follow" in args
    like_only = "--like" in args
    run_now = "--now" in args or test_mode or post_only or follow_only or like_only

    if run_now:
        if like_only:
            logger.info("いいねのみモード")
            run_like(config)
        elif follow_only:
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
