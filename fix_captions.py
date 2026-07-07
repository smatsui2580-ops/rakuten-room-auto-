"""
楽天ROOMの既存投稿にキャプションを遡って追加するスクリプト

使い方:
  python fix_captions.py            # キャプションなし投稿を検出して追加
  python fix_captions.py --dry-run  # 確認のみ（実際には書き込まない）
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, BrowserContext

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
MY_ROOM_URL = "https://room.rakuten.co.jp/room_1988b87b48/items"


def load_config():
    with open("config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_cookies():
    if not COOKIES_FILE.exists():
        return []
    try:
        return json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


async def scrape_items_without_caption(page: Page) -> list[dict]:
    """ROOMプロフィールページをスクロールして、キャプションなし投稿を収集"""
    logger.info(f"ROOMページをスキャン中: {MY_ROOM_URL}")
    await page.goto(MY_ROOM_URL, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(3000)

    no_caption_items = []
    page_num = 0
    prev_count = 0

    while True:
        page_num += 1

        # スクロールして全アイテムを読み込む
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(2000)
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(2000)

        # アイテムカードを取得
        items = await page.evaluate("""() => {
            const results = [];
            // ROOMのアイテムカードを探す（様々なセレクタを試す）
            const cards = document.querySelectorAll(
                '[class*="item-card"], [class*="itemCard"], [class*="product-item"], li[class*="item"]'
            );
            cards.forEach(card => {
                // 商品リンク（楽天市場またはROOM内リンク）
                const link = card.querySelector('a[href*="item.rakuten.co.jp"], a[href*="room.rakuten.co.jp"]');
                if (!link) return;

                // キャプションテキスト（コメント部分）
                // 価格・ショップ名・いいね数以外のテキスト
                const allText = card.innerText || '';
                const priceMatch = allText.match(/¥[\d,]+/);
                const price = priceMatch ? priceMatch[0] : '';

                // キャプション候補テキスト
                const captionEl = card.querySelector(
                    '[class*="comment"], [class*="caption"], [class*="description"], [class*="text"], p'
                );
                const captionText = captionEl ? captionEl.innerText.trim() : '';

                // 価格行・ショップ名・ハート数を除いたテキストがキャプション
                // 非常に短い（20文字以下）または空ならキャプションなし
                const hasCaption = captionText.length > 20;

                results.push({
                    href: link.href,
                    captionText: captionText,
                    hasCaption: hasCaption,
                    price: price,
                });
            });
            return results;
        }""")

        # スクロールして取得できなかった場合は別セレクタで試す
        if len(items) == 0:
            items = await page.evaluate("""() => {
                const results = [];
                // 全リンクから楽天市場商品URLを拾う
                document.querySelectorAll('a[href*="item.rakuten.co.jp"]').forEach(a => {
                    const m = a.href.match(/item\\.rakuten\\.co\\.jp\\/([^/]+)\\/([^/?#]+)/);
                    if (!m) return;
                    const card = a.closest('li, article, [class*="item"], [class*="product"]') || a.parentElement?.parentElement;
                    const text = card ? card.innerText : '';
                    const priceMatch = text.match(/¥[\d,]+/);
                    results.push({
                        href: a.href,
                        item_code: m[1] + ':' + m[2],
                        captionText: '',
                        hasCaption: text.length > 100,
                        price: priceMatch ? priceMatch[0] : '',
                    });
                });
                return results;
            }""")

        current_count = len(items)
        logger.info(f"スキャン {page_num}回目: {current_count}件検出")

        for item in items:
            href = item.get("href", "")
            if "item.rakuten.co.jp" in href:
                import re
                m = re.search(r"item\.rakuten\.co\.jp/([^/]+)/([^/?#]+)", href)
                if m:
                    item["item_code"] = m.group(1) + ":" + m.group(2)
                    item["rakuten_url"] = href
                else:
                    continue
            elif "room.rakuten.co.jp" in href:
                item["room_url"] = href
            else:
                continue

            if not item.get("hasCaption"):
                no_caption_items.append(item)

        # これ以上スクロールしても増えなければ終了
        if current_count == prev_count or current_count == 0:
            break
        prev_count = current_count

        # 「もっと見る」ボタンがあればクリック
        try:
            more_btn = page.locator('button:has-text("もっと見る"), a:has-text("もっと見る"), [class*="more"]').first
            if await more_btn.is_visible(timeout=2000):
                await more_btn.click()
                await page.wait_for_timeout(2000)
                continue
        except Exception:
            pass

        if page_num >= 5:
            break

    # 重複除去（同じitem_codeが複数あれば1つに）
    seen = set()
    unique = []
    for item in no_caption_items:
        code = item.get("item_code", item.get("href", ""))
        if code not in seen:
            seen.add(code)
            unique.append(item)

    logger.info(f"キャプションなし投稿: {len(unique)}件")
    return unique


async def get_item_info_from_rakuten(page: Page, item_url: str) -> dict:
    """楽天市場の商品ページから商品情報を取得"""
    try:
        await page.goto(item_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)

        info = await page.evaluate("""() => {
            const title = document.querySelector('h1, [class*="item-name"], [class*="itemName"]');
            const price = document.querySelector('[class*="price"], .important, [class*="Price"]');
            const desc = document.querySelector('[class*="item-desc"], [class*="itemDesc"], [class*="description"]');
            return {
                item_name: title ? title.innerText.trim().slice(0, 100) : document.title.slice(0, 80),
                item_price: price ? (price.innerText.match(/[\\d,]+/)?.[0] || '').replace(/,/g, '') : '0',
                item_caption: desc ? desc.innerText.trim().slice(0, 200) : '',
            };
        }""")
        return info
    except Exception as e:
        logger.warning(f"商品情報取得失敗 {item_url}: {e}")
        return {"item_name": "", "item_price": "0", "item_caption": ""}


def generate_caption(item_info: dict, caption_cfg: dict) -> str:
    """Claude APIでキャプション生成"""
    import anthropic
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    name = item_info.get("item_name", "")
    price = item_info.get("item_price", "0")
    desc = item_info.get("item_caption", "")

    try:
        price_int = int(str(price).replace(",", ""))
        price_str = f"¥{price_int:,}"
    except Exception:
        price_str = f"¥{price}"

    hashtag_instruction = (
        f"\n- 最後に関連するハッシュタグを{caption_cfg['hashtag_count']}個追加してください（例: #コスメ #おすすめ）"
        if caption_cfg.get("add_hashtags") else ""
    )

    prompt = f"""楽天ROOMの投稿キャプションを作成してください。

【商品情報】
- 商品名: {name}
- 価格: {price_str}
- 商品説明（抜粋）: {desc}

【文章の構成（この順番で書く）】
1. 絵本のような世界観・情景が浮かぶ書き出し（1〜2行）
2. 商品の見た目・素材・使い心地の描写（2〜3行）
3. 🌟おすすめポイント を箇条書き（3〜4個、「・」で始める）
4. 締めの一言（自分へのご褒美・贈り物・日常を豊かに、など）

【ルール】
- トーン: {caption_cfg['tone']}
- 文字数: {caption_cfg['max_length']}文字以内（ハッシュタグ含む）
- 絵文字を自然に使う（1行に1〜2個程度）{hashtag_instruction}

キャプションのみ出力してください（前置きや説明文は不要）。"""

    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text.strip()
    except Exception as e:
        logger.error(f"キャプション生成失敗: {e}")
        return f"{name}\n{price_str}\n\nおすすめの商品です♡"


async def add_caption_to_room(page: Page, context: BrowserContext, item_code: str, caption: str, action_delay: int = 2) -> bool:
    """ROOMの既存投稿にキャプションを追加・更新"""
    from urllib.parse import quote
    room_url = f"https://room.rakuten.co.jp/mix/collect?itemcode={quote(item_code, safe='')}&scid=we_room_upc60"

    logger.info(f"ROOM編集ページに移動: {item_code}")
    await page.goto(room_url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(action_delay * 1000)

    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    await page.wait_for_timeout(3000)

    # ログインページに飛んだ場合
    if "id.rakuten" in page.url or "login" in page.url:
        logger.error("セッション切れ。クッキーを更新してください。")
        return False

    # /mix/out は投稿不可商品
    if "/mix/out" in page.url:
        logger.warning(f"投稿不可商品: {item_code}")
        return False

    # テキストエリアを探す
    textarea_selectors = [
        'textarea[placeholder*="オススメポイント"]',
        'textarea[placeholder*="好きな所"]',
        'textarea[placeholder*="コメント"]',
        'textarea',
    ]

    textarea = None
    for selector in textarea_selectors:
        try:
            t = page.locator(selector).first
            if await t.is_visible(timeout=5000):
                textarea = t
                break
        except Exception:
            continue

    if textarea is None:
        logger.warning(f"テキストエリアが見つかりません: {item_code}")
        return False

    # 既存のキャプションを確認（すでに入力済みならスキップ）
    existing = await textarea.input_value()
    if len(existing) > 20:
        logger.info(f"既にキャプションあり ({len(existing)}文字) → スキップ: {item_code}")
        return True

    # キャプション入力
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
        logger.warning(f"キャプション入力失敗: {item_code}")
        return False

    logger.info(f"キャプション入力: {len(actual)}文字")
    await page.wait_for_timeout(action_delay * 1000)

    # 完了ボタンをクリック
    try:
        done_btn = page.locator('button.collect-btn, button:has-text("完了")').first
        if await done_btn.is_visible(timeout=3000):
            await done_btn.scroll_into_view_if_needed()
            await done_btn.click(force=True)
            logger.info("完了ボタンクリック")

            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                await page.wait_for_timeout(5000)

            # 成功確認
            for indicator in ['text="コレ完了"', 'text="my ROOMを見る"', 'a:has-text("my ROOM")', 'text="コレ！しました"']:
                try:
                    if await page.locator(indicator).first.is_visible(timeout=2000):
                        logger.info(f"キャプション追加成功: {item_code}")
                        return True
                except Exception:
                    continue

            # テキストエリアが消えていれば成功
            try:
                if not await page.locator('textarea').first.is_visible(timeout=1000):
                    logger.info(f"キャプション追加成功（フォーム消滅）: {item_code}")
                    return True
            except Exception:
                pass

            logger.warning(f"成功確認できず: {item_code} / URL: {page.url}")
            return False
    except Exception as e:
        logger.error(f"完了ボタンクリック失敗: {e}")
        return False


async def main_async(dry_run: bool = False):
    config = load_config()
    caption_cfg = config["caption"]
    browser_cfg = config["browser"]
    action_delay = browser_cfg["action_delay"]

    cookies = load_cookies()
    if not cookies:
        logger.error("クッキーが見つかりません。先に login.py を実行してください。")
        sys.exit(1)

    headless = True if os.getenv("CI") == "true" else True  # 常にheadless
    launch_args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"] if os.getenv("CI") else []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, args=launch_args)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        await context.add_cookies(cookies)
        page = await context.new_page()

        # キャプションなし投稿をスキャン
        no_caption_items = await scrape_items_without_caption(page)

        if not no_caption_items:
            logger.info("キャプションなし投稿は見つかりませんでした")
            await browser.close()
            return

        logger.info(f"\n{'='*50}")
        logger.info(f"キャプションなし投稿: {len(no_caption_items)}件")
        for item in no_caption_items:
            logger.info(f"  {item.get('item_code', '?')} {item.get('price', '')}")
        logger.info(f"{'='*50}\n")

        if dry_run:
            logger.info("[dry-run] 確認のみ。実際の書き込みはスキップします。")
            await browser.close()
            return

        # 各投稿にキャプションを追加
        fixed = 0
        failed = 0
        for i, item in enumerate(no_caption_items):
            item_code = item.get("item_code")
            if not item_code:
                logger.warning(f"item_code不明: {item.get('href', '?')}")
                continue

            rakuten_url = item.get("rakuten_url") or item.get("href", "")
            shop, code = item_code.split(":", 1) if ":" in item_code else ("", item_code)
            if not rakuten_url:
                rakuten_url = f"https://item.rakuten.co.jp/{shop}/{code}/"

            logger.info(f"\n[{i+1}/{len(no_caption_items)}] {item_code}")

            # 商品情報を取得
            item_info = await get_item_info_from_rakuten(page, rakuten_url)
            if not item_info.get("item_name"):
                item_info["item_name"] = f"{shop} {code}"

            # キャプション生成
            caption = generate_caption(item_info, caption_cfg)
            logger.info(f"生成キャプション ({len(caption)}文字): {caption[:50]}...")

            # ROOMに追加
            ok = await add_caption_to_room(page, context, item_code, caption, action_delay)
            if ok:
                fixed += 1
            else:
                failed += 1

            # 連続リクエストを避けるため少し待つ
            await page.wait_for_timeout(3000)

        logger.info(f"\n{'='*50}")
        logger.info(f"完了: {fixed}件追加成功 / {failed}件失敗")
        logger.info(f"{'='*50}")

        await browser.close()


def main():
    dry_run = "--dry-run" in sys.argv
    asyncio.run(main_async(dry_run=dry_run))


if __name__ == "__main__":
    Path("logs").mkdir(exist_ok=True)
    main()
