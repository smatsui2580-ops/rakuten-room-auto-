"""ROOMの画面を確認してROOMボタンのURLを調べるデバッグスクリプト"""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

COOKIES_FILE = Path(__file__).parent / "data" / "rakuten_cookies.json"
# テスト商品URL（以前に取得したもの）
TEST_ITEM_URL = "https://item.rakuten.co.jp/eemon/001-681/?rafcid=wsc_i_is_7f..."


async def main():
    cookies = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        await context.add_cookies(cookies)
        page = await context.new_page()

        # ROOMに移動してスクリーンショット
        print("ROOMに移動中...")
        await page.goto("https://room.rakuten.co.jp/", wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)
        await page.screenshot(path="logs/room_main.png")
        print("ROOMメイン画面: logs/room_main.png")

        # ROOMボタンのhrefを調べる
        print("\n商品ページに移動中...")
        await page.goto("https://item.rakuten.co.jp/eemon/001-681/", wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)
        await page.screenshot(path="logs/product_page.png")
        print("商品ページ: logs/product_page.png")

        # ROOMリンクのhrefを取得
        links = await page.locator('a[href*="room.rakuten.co.jp"]').all()
        print(f"\nROOM関連リンク: {len(links)}個")
        for link in links[:5]:
            href = await link.get_attribute("href")
            text = await link.inner_text()
            print(f"  テキスト: {text[:30]} | href: {href}")

        input("確認したらEnterを押してください...")
        await browser.close()


asyncio.run(main())
