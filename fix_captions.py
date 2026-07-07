"""
楽天ROOMの既存投稿にキャプションを遡って追加するスクリプト

使い方:
  python3 fix_captions.py              # 直近7日分を処理
  python3 fix_captions.py --days=14   # 直近14日分を処理
  python3 fix_captions.py --dry-run   # 確認のみ（書き込みしない）
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/fix_captions.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

COOKIES_FILE = Path("data/rakuten_cookies.json")


def load_config():
    with open("config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_cookies():
    if not COOKIES_FILE.exists():
        return []
    return json.loads(COOKIES_FILE.read_text(encoding="utf-8"))


def get_recent_item_codes(days: int) -> list[str]:
    """posted_items.json から直近N日以内のアイテムコードを新しい順に返す"""
    path = Path("data/posted_items.json")
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    cutoff = datetime.now() - timedelta(days=days)
    recent = [
        code for code, ts in data.items()
        if datetime.fromisoformat(ts) > cutoff
    ]
    recent.sort(key=lambda c: data[c], reverse=True)
    return recent


def generate_caption(item_info: dict, caption_cfg: dict) -> str:
    """Claude APIでキャプション生成"""
    import anthropic
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    name = item_info.get("item_name", "")
    try:
        price_int = int(item_info.get("item_price", "0").replace(",", "") or "0")
        price_str = f"¥{price_int:,}" if price_int else ""
    except Exception:
        price_str = ""

    hashtag_instr = (
        f"\n- 最後に関連するハッシュタグを{caption_cfg['hashtag_count']}個追加（例: #インテリア #おすすめ）"
        if caption_cfg.get("add_hashtags") else ""
    )

    prompt = f"""楽天ROOMの投稿キャプションを作成してください。

【商品情報】
- 商品名: {name}
- 価格: {price_str}
- 商品説明: {item_info.get('item_caption', '')}

【構成】
1. 絵本のような情景が浮かぶ書き出し（1〜2行）
2. 見た目・素材・使い心地の描写（2〜3行）
3. 🌟おすすめポイント を箇条書き（3〜4個）
4. 締めの一言

【ルール】
- トーン: {caption_cfg['tone']}
- {caption_cfg['max_length']}文字以内{hashtag_instr}

キャプションのみ出力してください。"""

    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        logger.error(f"キャプション生成失敗: {e}")
        return f"{name}\n{price_str}\n\nおすすめの商品です♡ #楽天ROOM"


async def process_item(page: Page, shop: str, code: str, caption_cfg: dict, action_delay: int, dry_run: bool) -> str:
    """
    楽天商品ページ → ROOMに追加リンク取得 → ROOM編集フォームでキャプション追加
    戻り値: "skipped" / "fixed" / "failed" / "no_room_btn"
    """
    item_url = f"https://item.rakuten.co.jp/{shop}/{code}/"
    logger.info(f"  楽天商品ページ: {item_url}")

    try:
        await page.goto(item_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
    except Exception as e:
        logger.warning(f"  商品ページ取得失敗: {e}")
        return "failed"

    # 商品情報を取得
    item_info = await page.evaluate("""() => {
        const titleEl = document.querySelector('h1');
        const priceEl = document.querySelector('.price2 .price, [class*="price"] .price');
        const descEl = document.querySelector('#item-description, [class*="item-desc"]');
        return {
            item_name: titleEl ? titleEl.innerText.trim().slice(0, 100) : document.title.slice(0, 80),
            item_price: priceEl ? priceEl.innerText.replace(/[^0-9]/g, '') : '0',
            item_caption: descEl ? descEl.innerText.trim().slice(0, 200) : '',
        };
    }""")

    # ROOMに追加リンクのhrefを取得（数値itemcodeが含まれている）
    room_href = None
    for selector in [
        'a[href*="room.rakuten.co.jp/mix"]',
        'a[href*="room.rakuten.co.jp"]',
    ]:
        try:
            link = page.locator(selector).first
            if await link.is_visible(timeout=3000):
                href = await link.get_attribute("href")
                if href and "room.rakuten.co.jp" in href:
                    room_href = href
                    logger.info(f"  ROOMリンク発見: {href[:80]}")
                    break
        except Exception:
            continue

    if not room_href:
        logger.info(f"  ROOMに追加ボタンが見つかりません: {shop}/{code}")
        return "no_room_btn"

    # ROOMフォームページに移動
    try:
        await page.goto(room_href, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(action_delay * 1000)
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        await page.wait_for_timeout(3000)
    except Exception as e:
        logger.warning(f"  ROOMページ移動失敗: {e}")
        return "failed"

    current_url = page.url
    logger.info(f"  ROOM URL: {current_url}")

    if "id.rakuten" in current_url or "login" in current_url:
        logger.error("  セッション切れ")
        return "session_expired"

    if "/mix/out" in current_url:
        logger.info(f"  投稿不可商品")
        return "no_room_btn"

    # テキストエリアを探す
    textarea = None
    for selector in [
        'textarea[placeholder*="オススメポイント"]',
        'textarea[placeholder*="好きな所"]',
        'textarea[placeholder*="コメント"]',
        'textarea',
    ]:
        try:
            t = page.locator(selector).first
            if await t.is_visible(timeout=4000):
                textarea = t
                break
        except Exception:
            continue

    if textarea is None:
        logger.info(f"  フォームなし（投稿ページに遷移しなかった）")
        return "failed"

    # 既存キャプション確認
    existing = (await textarea.input_value()).strip()
    if len(existing) > 20:
        logger.info(f"  キャプションあり ({len(existing)}文字) → スキップ")
        return "skipped"

    logger.info(f"  キャプションなし → 追加します")

    if dry_run:
        logger.info("  [dry-run] スキップ")
        return "fixed"

    # キャプション生成
    caption = generate_caption(item_info, caption_cfg)
    logger.info(f"  キャプション生成: {caption[:50]}... ({len(caption)}文字)")

    # 入力
    await textarea.scroll_into_view_if_needed()
    await textarea.click()
    await page.wait_for_timeout(500)

    await textarea.fill(caption)
    await page.wait_for_timeout(500)
    actual = await textarea.input_value()

    if len(actual) == 0:
        await textarea.press_sequentially(caption, delay=10)
        await textarea.press("Tab")
        await page.wait_for_timeout(2000)
        actual = await textarea.input_value()

    if len(actual) == 0:
        logger.warning(f"  キャプション入力失敗")
        return "failed"

    await page.wait_for_timeout(action_delay * 1000)

    # 完了ボタンをクリック
    try:
        done_btn = page.locator('button.collect-btn, button:has-text("完了")').first
        if not await done_btn.is_visible(timeout=3000):
            logger.warning(f"  完了ボタンが見つかりません")
            return "failed"

        await done_btn.scroll_into_view_if_needed()
        await done_btn.click(force=True)
        logger.info("  完了ボタンクリック")

        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            await page.wait_for_timeout(5000)

        for indicator in ['a:has-text("my ROOM")', 'text="コレ完了"', 'text="コレ！しました"']:
            try:
                if await page.locator(indicator).first.is_visible(timeout=2000):
                    logger.info(f"  追加成功")
                    return "fixed"
            except Exception:
                continue

        # テキストエリアが消えていれば成功
        try:
            if not await page.locator('textarea').first.is_visible(timeout=1000):
                logger.info(f"  追加成功（フォーム消滅）")
                return "fixed"
        except Exception:
            pass

        logger.warning(f"  成功確認できず: URL={page.url}")
        return "failed"

    except Exception as e:
        logger.error(f"  完了ボタンクリック失敗: {e}")
        return "failed"


async def main_async(days: int, dry_run: bool):
    config = load_config()
    caption_cfg = config["caption"]
    action_delay = config["browser"]["action_delay"]

    cookies = load_cookies()
    if not cookies:
        logger.error("クッキーが見つかりません。先に login.py を実行してください。")
        sys.exit(1)

    item_codes = get_recent_item_codes(days)
    logger.info(f"直近{days}日の投稿: {len(item_codes)}件を確認します")

    if not item_codes:
        logger.info("対象アイテムがありません")
        return

    launch_args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"] if os.getenv("CI") else []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=launch_args)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        await context.add_cookies(cookies)
        page = await context.new_page()

        stats: dict[str, int] = {"skipped": 0, "fixed": 0, "failed": 0, "no_room_btn": 0}

        for i, item_code in enumerate(item_codes):
            logger.info(f"[{i+1}/{len(item_codes)}] {item_code}")
            if ":" not in item_code:
                logger.warning(f"  item_code形式不明: {item_code}")
                continue

            shop, code = item_code.split(":", 1)
            result = await process_item(page, shop, code, caption_cfg, action_delay, dry_run)

            if result == "session_expired":
                logger.error("セッション切れ → 処理終了")
                break

            stats[result] = stats.get(result, 0) + 1
            await page.wait_for_timeout(2000)

        await browser.close()

    logger.info(f"\n{'='*50}")
    logger.info(f"完了: 追加={stats.get('fixed',0)} / スキップ(既存)={stats.get('skipped',0)} / 失敗={stats.get('failed',0)} / ROOMボタンなし={stats.get('no_room_btn',0)}")
    logger.info(f"{'='*50}")


def main():
    days = 7
    dry_run = False
    for arg in sys.argv[1:]:
        if arg.startswith("--days="):
            days = int(arg.split("=")[1])
        elif arg == "--dry-run":
            dry_run = True

    Path("logs").mkdir(exist_ok=True)
    asyncio.run(main_async(days, dry_run))


if __name__ == "__main__":
    main()
