"""ROOMプロフィールページの編集フローを調査するスクリプト"""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

COOKIES_FILE = Path(__file__).parent / "data" / "rakuten_cookies.json"
PROFILE_URL = "https://room.rakuten.co.jp/room_1988b87b48/items"

async def main():
    cookies = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        await context.add_cookies(cookies)
        page = await context.new_page()

        # ネットワークリクエストをキャプチャ
        requests_log = []
        def on_request(request):
            if "api" in request.url or "collect" in request.url or "room" in request.url.lower():
                requests_log.append(f"{request.method} {request.url}")

        page.on("request", on_request)

        print("ROOMプロフィールに移動中...")
        await page.goto(PROFILE_URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(3)
        await page.screenshot(path="logs/debug_profile.png")
        print("スクリーンショット: logs/debug_profile.png")

        # キャプションなし（価格だけ表示）の最初のアイテムをクリックしてみる
        print("\n最初のアイテムをクリックします...")
        first_item = page.locator('li.item, [class*="item"], a[href*="room"]').first
        try:
            await first_item.click(timeout=5000)
            await asyncio.sleep(2)
            await page.screenshot(path="logs/debug_item_click.png")
            print(f"クリック後URL: {page.url}")
            print("スクリーンショット: logs/debug_item_click.png")
        except Exception as e:
            print(f"クリック失敗: {e}")

        print("\n=== キャプチャしたリクエスト ===")
        for r in requests_log[:20]:
            print(r)

        input("\nブラウザを確認してEnterを押してください...")
        await browser.close()

asyncio.run(main())
