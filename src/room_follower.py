"""
楽天ROOMの自動フォローモジュール

戦略：自分の投稿にいいねしてくれたユーザーを自動フォロー
- 1日の上限：500件（ROOM規約内）
- フォロー済みユーザーはスキップ（重複フォロー防止）
"""

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from playwright.async_api import async_playwright, Page, BrowserContext

logger = logging.getLogger(__name__)

COOKIES_FILE = Path(__file__).parent.parent / "data" / "rakuten_cookies.json"
FOLLOW_HISTORY_FILE = Path(__file__).parent.parent / "data" / "followed_users.json"


def _load_cookies() -> list:
    if not COOKIES_FILE.exists():
        return []
    try:
        return json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _load_follow_history() -> dict:
    if not FOLLOW_HISTORY_FILE.exists():
        return {}
    try:
        return json.loads(FOLLOW_HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_follow_history(history: dict):
    FOLLOW_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    FOLLOW_HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def is_already_followed(user_id: str, refollow_days: int = 30) -> bool:
    history = _load_follow_history()
    if user_id not in history:
        return False
    followed_at = datetime.fromisoformat(history[user_id])
    return datetime.now() - followed_at < timedelta(days=refollow_days)


def mark_as_followed(user_id: str):
    history = _load_follow_history()
    history[user_id] = datetime.now().isoformat()
    _save_follow_history(history)


async def _get_users_from_profile_followers(page: Page, user_id: str) -> list[str]:
    """指定ユーザーのフォロワーページからユーザーIDを取得"""
    for url in [
        f"https://room.rakuten.co.jp/{user_id}/follower",
        f"https://room.rakuten.co.jp/{user_id}/followers",
    ]:
        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)
            for _ in range(4):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1500)
            found = await page.evaluate("""() => {
                const users = [];
                document.querySelectorAll('a[href]').forEach(a => {
                    const match = a.href.match(/room\\.rakuten\\.co\\.jp\\/(room_[^/?#]+)/);
                    if (match && match[1] && !users.includes(match[1])) {
                        users.push(match[1]);
                    }
                });
                return users;
            }""")
            if found:
                logger.info(f"フォロワー探索 {user_id} → {len(found)} 人")
                return found
        except Exception:
            continue
    return []


async def _search_one_keyword(page: Page, keyword: str) -> list[str]:
    """1キーワード分のユーザーIDを取得"""
    from urllib.parse import quote
    search_url = f"https://room.rakuten.co.jp/search/item?keyword={quote(keyword)}&user_tab=1"
    logger.info(f"ユーザー検索: {keyword} → {search_url}")
    try:
        await page.goto(search_url, wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(5000)
        for _ in range(6):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2000)
        found = await page.evaluate("""() => {
            const users = [];
            document.querySelectorAll('a[href]').forEach(a => {
                const href = a.href || '';
                const match = href.match(/room\\.rakuten\\.co\\.jp\\/(room_[^/?#]+)/);
                if (match && match[1] && !users.includes(match[1])) {
                    users.push(match[1]);
                }
            });
            return users;
        }""")
        logger.info(f"キーワード「{keyword}」→ {len(found)} 人")
        return found
    except Exception as e:
        logger.debug(f"ユーザー検索失敗 {keyword}: {e}")
        return []


async def _get_users_from_search(page: Page, keywords: list[str], max_users: int = 50) -> list[str]:
    """ROOMのユーザー検索から同ジャンルのユーザーIDを取得"""
    user_ids = []

    for keyword in keywords:
        if len(user_ids) >= max_users:
            break

        from urllib.parse import quote
        search_url = f"https://room.rakuten.co.jp/search/item?keyword={quote(keyword)}&user_tab=1"
        logger.info(f"ユーザー検索: {keyword} → {search_url}")
        try:
            await page.goto(search_url, wait_until="networkidle", timeout=60000)
            await page.wait_for_timeout(5000)

            # スクロールして追加ロード
            for _ in range(6):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(2000)

            # ページ上の全リンクからユーザーIDを取得
            found = await page.evaluate("""() => {
                const users = [];
                document.querySelectorAll('a[href]').forEach(a => {
                    const href = a.href || '';
                    // room_XXXXXXXX 形式のユーザーID
                    const match = href.match(/room\\.rakuten\\.co\\.jp\\/(room_[^/?#]+)/);
                    if (match && match[1] && !users.includes(match[1])) {
                        users.push(match[1]);
                    }
                });
                return users;
            }""")

            logger.info(f"キーワード「{keyword}」→ {len(found)} 人")
            for uid in found:
                if uid not in user_ids:
                    user_ids.append(uid)

        except Exception as e:
            logger.debug(f"ユーザー検索失敗 {keyword}: {e}")
            continue

    logger.info(f"フォロー候補ユーザー {len(user_ids)} 人を取得")
    return user_ids


async def _follow_user(page: Page, user_id: str, action_delay: int = 2) -> bool:
    """指定ユーザーをフォローする"""
    profile_url = f"https://room.rakuten.co.jp/{user_id}"
    try:
        try:
            await page.goto(profile_url, wait_until="networkidle", timeout=60000)
        except Exception:
            await page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(action_delay * 1000)

        # クッキー期限切れ検知
        if "id.rakuten" in page.url or ("login" in page.url and "room" not in page.url):
            logger.error(f"クッキー期限切れ検知（フォロー）: {page.url}")
            return False

        # フォローボタンを探す
        follow_selectors = [
            'button:has-text("フォローする")',
            'button:has-text("フォロー")',
            '[class*="follow"]:not([class*="following"]):not([class*="unfollow"])',
        ]

        for selector in follow_selectors:
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=2000):
                    btn_text = await btn.inner_text()
                    logger.info(f"ボタン発見: {selector} | テキスト: '{btn_text.strip()}' | ユーザー: {user_id}")

                    if "フォロー中" in btn_text or "フォロー済" in btn_text:
                        logger.info(f"すでにフォロー済み: {user_id}")
                        return False

                    await btn.click(force=True)
                    await page.wait_for_timeout(1000)

                    # フォロー成功確認：ボタンが「フォロー中」に変わるか確認
                    try:
                        await page.wait_for_function(
                            """() => {
                                const btns = Array.from(document.querySelectorAll('button, [class*="follow"]'));
                                return btns.some(b => (b.innerText || b.textContent || '').includes('フォロー中'));
                            }""",
                            timeout=6000,
                        )
                        logger.info(f"フォロー完了（確認済み）: {user_id}")
                        return True
                    except Exception:
                        # ボタン状態で再確認
                        try:
                            after_text = await btn.inner_text()
                            if "フォロー中" in after_text:
                                logger.info(f"フォロー完了（ボタン変化確認）: {user_id}")
                                return True
                            logger.warning(f"フォロー後ボタン未変化: '{after_text.strip()}' → 失敗扱い: {user_id}")
                        except Exception:
                            logger.warning(f"フォロー後確認不能 → 失敗扱い: {user_id}")
                        return False
            except Exception:
                continue

        logger.debug(f"フォローボタンが見つからない: {user_id}")
        return False

    except Exception as e:
        logger.error(f"フォロー失敗 {user_id}: {e}")
        return False


async def run_auto_follow(
    my_room_user_id: str,
    search_keywords: list[str] = None,
    max_follows: int = 50,
    action_delay: int = 2,
    headless: bool = False,
    refollow_days: int = 30,
) -> int:
    """自動フォローを実行。フォローした件数を返す"""

    if search_keywords is None:
        search_keywords = ["おうちカフェ", "ナチュラル雑貨", "キッチン雑貨", "北欧インテリア"]

    async with async_playwright() as p:
        launch_args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"] if os.getenv("CI") else []
        browser = await p.chromium.launch(headless=headless, args=launch_args)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )

        cookies = _load_cookies()
        if not cookies:
            logger.error("クッキーがありません。login.py を先に実行してください。")
            await browser.close()
            return 0

        await context.add_cookies(cookies)
        page = await context.new_page()

        # クッキー有効性確認
        await page.goto("https://room.rakuten.co.jp/", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2000)
        if "id.rakuten" in page.url or ("login" in page.url and "room" not in page.url):
            logger.error(f"クッキー期限切れ（フォロー開始時）: {page.url}")
            # 自動ログイン試行
            email = os.getenv("RAKUTEN_EMAIL")
            password = os.getenv("RAKUTEN_PASSWORD")
            if email and password:
                from src.auto_login import auto_login_rakuten
                logger.info("自動ログインを試みます...")
                login_ok = await auto_login_rakuten(email, password)
                if not login_ok:
                    await browser.close()
                    return 0
                # 新しいクッキーを読み込んで続行
                new_cookies = _load_cookies()
                if new_cookies:
                    await context.add_cookies(new_cookies)
                    logger.info(f"自動ログイン後クッキー再読み込み: {len(new_cookies)}件")
                else:
                    await browser.close()
                    return 0
            else:
                logger.error("RAKUTEN_EMAIL / RAKUTEN_PASSWORD 未設定のため自動ログイン不可")
                await browser.close()
                return 0

        follow_count = 0
        try:
            # キーワードを1つずつ処理して新規候補が揃い次第停止
            user_ids = []
            for keyword in search_keywords:
                new_found = await _search_one_keyword(page, keyword)
                for uid in new_found:
                    if uid not in user_ids:
                        user_ids.append(uid)
                new_candidates = [u for u in user_ids if not is_already_followed(u, refollow_days) and u != my_room_user_id]
                if len(new_candidates) >= max_follows:
                    logger.info(f"新規候補 {len(new_candidates)} 人確保 → キーワード検索終了")
                    break

            # それでも足りなければフォロワーチェーンで補充
            new_candidates = [u for u in user_ids if not is_already_followed(u, refollow_days) and u != my_room_user_id]
            if len(new_candidates) < max_follows:
                logger.info(f"新規候補 {len(new_candidates)} 人 → フォロワーチェーンで補充します")
                seed_users = user_ids[:8]
                for seed in seed_users:
                    follower_ids = await _get_users_from_profile_followers(page, seed)
                    for uid in follower_ids:
                        if uid not in user_ids:
                            user_ids.append(uid)
                    new_candidates = [u for u in user_ids if not is_already_followed(u, refollow_days) and u != my_room_user_id]
                    if len(new_candidates) >= max_follows * 2:
                        break
                logger.info(f"フォロワーチェーン後の新規候補: {len(new_candidates)} 人")

            for user_id in user_ids:
                if follow_count >= max_follows:
                    logger.info(f"フォロー上限 {max_follows} 件に達しました")
                    break

                if user_id == my_room_user_id:
                    continue

                if is_already_followed(user_id, refollow_days):
                    logger.debug(f"スキップ（フォロー済み）: {user_id}")
                    continue

                success = await _follow_user(page, user_id, action_delay)
                if success:
                    mark_as_followed(user_id)
                    follow_count += 1

        except Exception as e:
            logger.error(f"自動フォロー中にエラー: {e}")
        finally:
            await browser.close()

        logger.info(f"自動フォロー完了: {follow_count} 件")
        return follow_count
