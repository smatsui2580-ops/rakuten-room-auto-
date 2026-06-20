"""
楽天への自動ヘッドレスログイン

クッキーが切れた場合に、保存済みのメール/パスワードで自動再ログインする。
"""

import asyncio
import json
import logging
import os
from pathlib import Path

from playwright.async_api import async_playwright, Page

logger = logging.getLogger(__name__)

COOKIES_FILE = Path(__file__).parent.parent / "data" / "rakuten_cookies.json"


async def _is_logged_in(page: Page) -> bool:
    selectors = [
        ':has-text("ログアウト")',
        'a[href*="logout"]',
        ':has-text("保有ポイント")',
    ]
    for sel in selectors:
        try:
            if await page.locator(sel).first.is_visible(timeout=2000):
                return True
        except Exception:
            continue
    return False


async def auto_login_rakuten(email: str, password: str) -> bool:
    """楽天市場 + ROOMにヘッドレスで自動ログインしてクッキーを保存する"""

    logger.info("自動ログイン開始...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        page = await context.new_page()

        try:
            # 楽天トップからログインボタン経由でSSOページへ遷移
            await page.goto(
                "https://www.rakuten.co.jp/",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            await page.wait_for_timeout(2000)

            # ログインリンクをクリックして実際のSSOログインページへ
            login_link = page.locator('a[href*="login"]:has-text("ログイン"), a.loginUser').first
            try:
                if await login_link.is_visible(timeout=5000):
                    await login_link.click()
                    await page.wait_for_load_state("domcontentloaded")
                    await page.wait_for_timeout(2000)
            except Exception:
                # フォールバック: grp02サブドメインのログインURL
                await page.goto(
                    "https://grp02.id.rakuten.co.jp/rms/nid/login?scid=wi_ich_rack_header_login",
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                await page.wait_for_timeout(2000)

            logger.info(f"ログインページURL: {page.url}")

            # メールアドレス入力
            email_selectors = [
                'input[name="u"]',
                'input[type="email"]',
                'input[name="email"]',
                'input[placeholder*="メールアドレス"]',
                'input[placeholder*="ユーザーID"]',
            ]
            email_filled = False
            for sel in email_selectors:
                try:
                    inp = page.locator(sel).first
                    if await inp.is_visible(timeout=2000):
                        await inp.fill(email)
                        email_filled = True
                        logger.info(f"メールアドレス入力: {sel}")
                        break
                except Exception:
                    continue

            if not email_filled:
                logger.error("メールアドレス入力欄が見つかりません")
                await page.screenshot(path="logs/auto_login_failed.png")
                await browser.close()
                return False

            await page.wait_for_timeout(800)

            # パスワード入力
            pass_selectors = [
                'input[name="p"]',
                'input[type="password"]',
                'input[placeholder*="パスワード"]',
            ]
            pass_filled = False
            for sel in pass_selectors:
                try:
                    inp = page.locator(sel).first
                    if await inp.is_visible(timeout=2000):
                        await inp.fill(password)
                        pass_filled = True
                        logger.info("パスワード入力完了")
                        break
                except Exception:
                    continue

            if not pass_filled:
                logger.error("パスワード入力欄が見つかりません")
                await page.screenshot(path="logs/auto_login_failed.png")
                await browser.close()
                return False

            await page.wait_for_timeout(800)

            # ログインボタンクリック
            submit_selectors = [
                'button[type="submit"]',
                'input[type="submit"]',
                'button:has-text("ログイン")',
            ]
            submitted = False
            for sel in submit_selectors:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=2000):
                        await btn.click()
                        submitted = True
                        logger.info("ログインボタンクリック")
                        break
                except Exception:
                    continue

            if not submitted:
                await page.keyboard.press("Enter")
                logger.info("Enterキーでフォーム送信")

            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(4000)

            # ログイン確認
            if not await _is_logged_in(page):
                logger.error(f"ログイン失敗 / URL: {page.url}")
                await page.screenshot(path="logs/auto_login_failed.png")
                await browser.close()
                return False

            logger.info("楽天市場ログイン成功")

            # ROOMにもアクセスしてクッキーを取得
            await page.goto("https://room.rakuten.co.jp/", wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3000)

            # クッキーを保存
            all_cookies = await context.cookies()
            COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
            COOKIES_FILE.write_text(
                json.dumps(all_cookies, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info(f"クッキー保存完了: {len(all_cookies)}件 → {COOKIES_FILE}")
            await browser.close()
            return True

        except Exception as e:
            logger.error(f"自動ログイン中にエラー: {e}")
            try:
                await page.screenshot(path="logs/auto_login_error.png")
            except Exception:
                pass
            await browser.close()
            return False


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()

    email = os.getenv("RAKUTEN_EMAIL")
    password = os.getenv("RAKUTEN_PASSWORD")
    if not email or not password:
        print("RAKUTEN_EMAIL と RAKUTEN_PASSWORD を .env に設定してください")
    else:
        result = asyncio.run(auto_login_rakuten(email, password))
        print("ログイン成功" if result else "ログイン失敗")
