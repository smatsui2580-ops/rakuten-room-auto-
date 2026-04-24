"""
楽天ROOMログイン専用スクリプト
初回だけ実行してクッキーを保存する

実行方法:
  python3 login.py
"""

import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

COOKIES_FILE = Path(__file__).parent / "data" / "rakuten_cookies.json"
# ROOMボタンの動作確認用テスト商品
TEST_ITEM_URL = "https://item.rakuten.co.jp/k-s-kitchen/0052/"


async def is_logged_in(page) -> bool:
    try:
        # ポイント表示・ユーザー名・マイページリンクのどれかが見えたらログイン済み
        selectors = [
            ':has-text("保有ポイント")',
            ':has-text("ログアウト")',
            'a[href*="logout"]',
            'a[href*="mypage"]',
            '.logout',
        ]
        for sel in selectors:
            if await page.locator(sel).first.is_visible(timeout=1000):
                return True
        return False
    except Exception:
        return False


async def wait_for_login(page, label="楽天", timeout=300) -> bool:
    print(f"\n{label}にログインしてください...")
    for i in range(timeout):
        await asyncio.sleep(1)
        if await is_logged_in(page):
            return True
        url = page.url
        if "room.rakuten.co.jp" in url and "login" not in url and "id.rakuten" not in url:
            return True
        if i % 15 == 0 and i > 0:
            print(f"  {i}秒経過...")
    return False


async def main():
    print("="*50)
    print("楽天ROOM 初回ログイン設定")
    print("="*50)
    print("\nブラウザが開きます。以下の手順でログインしてください：")
    print("  ステップ1: 楽天市場にログイン（右上のログインボタンから）")
    print("  ステップ2: 商品ページのROOMボタンでROOMにログイン")
    print("\nすべて完了すると自動でブラウザが閉じます。\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await context.new_page()

        # ステップ1: 楽天市場にログイン
        print("[ステップ1] 楽天市場を開きます...")
        await page.goto("https://www.rakuten.co.jp/", wait_until="domcontentloaded", timeout=60000)

        logged_in = await wait_for_login(page, "楽天市場")
        if not logged_in:
            print("タイムアウト")
            await browser.close()
            return

        print("楽天市場ログイン確認！")

        # ステップ2: 商品ページのROOMボタンをクリックしてROOMログイン
        print("\n[ステップ2] 商品ページに移動してROOMボタンをクリックします...")
        await page.goto(TEST_ITEM_URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)

        # ROOMボタンを探してクリック
        room_links = page.locator('a[href*="room.rakuten.co.jp"]')
        count = await room_links.count()
        print(f"ROOMリンク: {count}個発見")

        if count > 0:
            href = await room_links.first.get_attribute("href")
            print(f"ROOMボタンURL: {href}")
            await room_links.first.click()
            await asyncio.sleep(2)

            # 新しいタブが開いた場合
            all_pages = context.pages
            if len(all_pages) > 1:
                new_page = all_pages[-1]
                print(f"新タブURL: {new_page.url}")

                # ログインが必要なら対応
                if "id.rakuten" in new_page.url or "login" in new_page.url:
                    print("\nROOMへのログインが必要です。新しいタブでログインしてください。")
                    await wait_for_login(new_page, "ROOM（新タブ）")

                await new_page.screenshot(path="logs/room_step2.png")
                print("スクリーンショット: logs/room_step2.png")

        # 全クッキー保存
        print("\nクッキーを保存中...")
        all_cookies = await context.cookies()
        COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
        COOKIES_FILE.write_text(
            json.dumps(all_cookies, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        print(f"保存完了: {len(all_cookies)}件")
        print("\n設定完了！ブラウザを閉じます。")
        await asyncio.sleep(2)
        await browser.close()


asyncio.run(main())
