"""
Playwrightで楽天ROOMに投稿するモジュール

【ログイン方式】
- クッキーが保存済み → 自動ログイン
- 未保存 → ブラウザでユーザーが手動ログイン（自動検知）
- ログインとROOM投稿を1つのブラウザセッションで完結
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from playwright.async_api import async_playwright, Page, BrowserContext

logger = logging.getLogger(__name__)

RAKUTEN_ROOM_URL = "https://room.rakuten.co.jp/"
COOKIES_FILE = Path(__file__).parent.parent / "data" / "rakuten_cookies.json"


def _load_cookies_sync() -> list:
    if not COOKIES_FILE.exists():
        return []
    try:
        return json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


async def _save_cookies(context: BrowserContext):
    cookies = await context.cookies()
    COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    COOKIES_FILE.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("クッキーを保存しました")


async def _ensure_logged_in(page: Page, context: BrowserContext) -> bool:
    """クッキーがあればそのまま使う。なければログインを促す"""

    # クッキーがある場合はログイン確認をスキップしてそのまま進む
    if _load_cookies_sync():
        logger.info("クッキーあり → ログイン確認スキップ")
        return True

    # クッキーがない場合のみ手動ログインを求める
    print("\n" + "="*50)
    print("クッキーがありません。login.py を先に実行してください。")
    print("  python3 login.py")
    print("="*50 + "\n")
    return False


def _build_room_url(item_code: str) -> str:
    from urllib.parse import quote
    return f"https://room.rakuten.co.jp/mix?itemcode={quote(item_code, safe='')}&scid=we_room_upc60"


async def post_to_room(
    item_url: str,
    caption: str,
    item_code: str = "",
    action_delay: int = 2,
    headless: bool = False,
    _auto_login_retry: bool = False,
) -> bool:
    """楽天ROOMに商品を投稿する"""

    async with async_playwright() as p:
        launch_args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"] if os.getenv("CI") else []
        browser = await p.chromium.launch(headless=headless, args=launch_args)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )

        cookies = _load_cookies_sync()
        if cookies:
            await context.add_cookies(cookies)
            logger.info(f"クッキー読み込み済み（{len(cookies)}件）")

        page = await context.new_page()

        try:
            logged_in = await _ensure_logged_in(page, context)
            if not logged_in:
                await browser.close()
                return False

            # ROOM投稿URLに直接移動（商品コードがある場合）
            if item_code:
                room_url = _build_room_url(item_code)
                logger.info(f"ROOM投稿ページに直接移動: {room_url}")
                await page.goto(room_url, wait_until="domcontentloaded", timeout=60000)
            else:
                logger.info(f"商品ページに移動: {item_url[:60]}...")
                await page.goto(item_url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(action_delay * 1000)
                room_btn = await _find_room_button(page)
                if not room_btn:
                    logger.error("ROOMに追加ボタンが見つかりません")
                    await browser.close()
                    return False
                await room_btn.click()
                logger.info("ROOMに追加ボタンをクリック")

            await page.wait_for_timeout(action_delay * 1000)

            # 新しいタブが開いた場合は切り替え
            all_pages = context.pages
            if len(all_pages) > 1:
                page = all_pages[-1]
                await page.wait_for_load_state("domcontentloaded")
                await page.wait_for_timeout(action_delay * 1000)

            # ログインページに飛んだ場合はクッキー期限切れ → 自動再ログイン
            if "id.rakuten" in page.url or ("login" in page.url and "room" not in page.url):
                logger.warning(f"クッキー期限切れを検知: {page.url}")
                await browser.close()

                if _auto_login_retry:
                    logger.error("自動ログイン後も失敗 → 処理を中断します")
                    return False

                email = os.getenv("RAKUTEN_EMAIL")
                password = os.getenv("RAKUTEN_PASSWORD")
                if not email or not password:
                    logger.error("RAKUTEN_EMAIL / RAKUTEN_PASSWORD が未設定のため自動ログインできません")
                    return False

                from src.auto_login import auto_login_rakuten
                logger.info("自動ログインを実行します...")
                login_ok = await auto_login_rakuten(email, password)
                if not login_ok:
                    logger.error("自動ログイン失敗")
                    return False

                logger.info("自動ログイン成功 → 投稿を再試行します")
                return await post_to_room(
                    item_url=item_url,
                    caption=caption,
                    item_code=item_code,
                    action_delay=action_delay,
                    headless=headless,
                    _auto_login_retry=True,
                )

            posted = await _fill_caption_and_post(page, caption, action_delay)

            if posted:
                await _save_cookies(context)
                logger.info("投稿完了")
            else:
                await page.screenshot(path="logs/post_failed.png")
                logger.info("投稿失敗スクリーンショット: logs/post_failed.png")

            await browser.close()
            return posted

        except Exception as e:
            logger.error(f"投稿中にエラー: {e}")
            try:
                await page.screenshot(path="logs/error_screenshot.png")
            except Exception:
                pass
            await browser.close()
            return False


async def _find_room_button(page: Page):
    """商品ページのROOMに追加ボタンを探す"""
    selectors = [
        'a[href*="room.rakuten.co.jp"]',
        'button:has-text("ROOMに追加")',
        'a:has-text("ROOMに追加")',
        'a:has-text("ROOM")',
        '[class*="room"]',
        'img[alt*="ROOM"]',
    ]
    for selector in selectors:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=2000):
                logger.info(f"ROOMボタン発見: {selector}")
                return btn
        except Exception:
            continue
    return None


async def _fill_caption_and_post(page: Page, caption: str, action_delay: int) -> bool:
    """キャプションを入力して投稿する"""
    await page.wait_for_timeout(action_delay * 1000)

    # スクリーンショットでページ状態を確認
    await page.screenshot(path="logs/before_fill.png")
    logger.info(f"入力前スクリーンショット: logs/before_fill.png / URL: {page.url}")

    # テキストエリアを探す前に投稿済み状態を早期検知
    try:
        # ① 「すでにコレ！している商品です」ダイアログ
        already = page.locator('text="すでにコレ！している商品です"')
        if await already.is_visible(timeout=1500):
            logger.warning("すでにコレ！済み（ダイアログ検知） → スキップ")
            try:
                await page.locator('a.button:has-text("OK"), button:has-text("OK")').first.click()
            except Exception:
                pass
            return "duplicate"
    except Exception:
        pass
    try:
        # ② 「この商品を削除」ボタンが出ている = 編集モード = すでに収集済み
        delete_btn = page.locator('a:has-text("この商品を削除"), button:has-text("この商品を削除")')
        if await delete_btn.is_visible(timeout=1500):
            logger.warning("編集モード検知（この商品を削除ボタンあり） → 重複スキップ")
            return "duplicate"
    except Exception:
        pass

    # キャプション入力欄を探す（ROOM投稿ページのテキストエリア）
    textarea_selectors = [
        'textarea[placeholder*="オススメポイント"]',
        'textarea[placeholder*="好きな所"]',
        'textarea[placeholder*="コメント"]',
        'textarea',
    ]

    filled = False
    for selector in textarea_selectors:
        try:
            t = page.locator(selector).first
            if await t.is_visible(timeout=1500):
                await t.scroll_into_view_if_needed()
                await t.click()
                await page.wait_for_timeout(500)

                # fill()でDOM値を設定したあと、必ずJSでAngularJSのスコープも更新する
                await t.fill(caption)
                await page.wait_for_timeout(300)

                await page.evaluate(
                    """([sel, text]) => {
                        const el = document.querySelector(sel);
                        if (!el) return;
                        const setter = Object.getOwnPropertyDescriptor(
                            window.HTMLTextAreaElement.prototype, 'value'
                        ).set;
                        setter.call(el, text);
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }""",
                    [selector, caption]
                )
                await page.wait_for_timeout(500)

                actual_value = await t.input_value()
                logger.info(f"キャプション入力: セレクタ={selector}, 入力文字数={len(actual_value)}")

                await page.wait_for_timeout(action_delay * 1000)
                if len(actual_value) > 0:
                    filled = True
                    break
        except Exception as e:
            logger.debug(f"テキストエリア試行失敗 {selector}: {e}")
            continue

    if not filled:
        logger.warning("テキストエリアへの入力に失敗しました")
        await page.screenshot(path="logs/fill_failed.png")
        return False

    # 「完了」ボタンをクリック → これが投稿ボタン（キャプション入力済みの場合）
    try:
        done_btn = page.locator('button.collect-btn, button:has-text("完了")').first
        if await done_btn.is_visible(timeout=3000):
            await done_btn.scroll_into_view_if_needed()
            # div.backgroundのオーバーレイを回避するためJS経由でクリック
            await page.evaluate("document.querySelector('button.collect-btn').click()")
            logger.info("投稿ボタンクリック（完了）")
            await page.wait_for_timeout(3000)
            await page.screenshot(path="logs/after_post.png")

            # 「すでにコレ！している商品です」ダイアログ → 投稿済み商品
            already_dialog = page.locator('text="すでにコレ！している商品です"')
            if await already_dialog.is_visible(timeout=2000):
                logger.warning("すでにコレ！済みの商品 → スキップ（履歴に記録）")
                try:
                    await page.locator('button:has-text("OK")').first.click()
                except Exception:
                    pass
                return "duplicate"

            # 投稿成功の確認：「my ROOMを見る」または「編集・削除」が出れば成功
            success_indicators = [
                'text="my ROOMを見る"',
                'text="編集・削除"',
                'a:has-text("my ROOM")',
            ]
            for indicator in success_indicators:
                try:
                    if await page.locator(indicator).first.is_visible(timeout=3000):
                        logger.info(f"投稿成功確認: {indicator} / URL: {page.url}")
                        return True
                except Exception:
                    continue

            # 投稿後もURLが変わっていない場合は失敗とみなす
            logger.warning(f"投稿後URL: {page.url} / 成功インジケータ未確認 → 失敗扱い")
            return False
    except Exception as e:
        logger.error(f"投稿ボタンクリック失敗: {e}")

    # どれも見つからない場合はページ上の全リンク・ボタンをログ出力
    logger.warning("投稿ボタンが見つかりませんでした。ページ上のボタン/リンクを調査します")
    try:
        elements = await page.evaluate("""() => {
            const results = [];
            document.querySelectorAll('button, a, [role="button"]').forEach(el => {
                const text = el.innerText?.trim();
                if (text) results.push({ tag: el.tagName, text: text.slice(0, 50), class: el.className.slice(0, 50) });
            });
            return results.slice(0, 20);
        }""")
        for el in elements:
            logger.info(f"  要素: {el['tag']} | テキスト: {el['text']} | class: {el['class']}")
    except Exception:
        pass
    await page.screenshot(path="logs/no_post_button.png")
    return False
