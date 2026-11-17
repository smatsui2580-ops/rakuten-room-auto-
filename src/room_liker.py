"""
楽天ROOMの自動いいねモジュール

目的: ROOMランク（C〜E）の条件「検索機能を使って商品検索」「投稿へのいいね」を
      毎回の実行で満たし続ける。件数は少量（既定5件）にとどめる。

流れ:
  1. 画面上部の検索ボックスにキーワードを入れて Enter（＝条件の「検索機能を使った検索」）
  2. 検索結果の投稿カードの「いいね」を、未いいね・他人の投稿に限って押す
  3. 押した投稿は data/liked_items.json に記録し、二度と押さない（押し直すと「いいね」が外れる）
  4. 最後に my ROOM のプロフィールを撮影してランク表示を記録する
"""

import json
import logging
import os
import random
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from playwright.async_api import async_playwright, Page

logger = logging.getLogger(__name__)

COOKIES_FILE = Path(__file__).parent.parent / "data" / "rakuten_cookies.json"
LIKE_HISTORY_FILE = Path(__file__).parent.parent / "data" / "liked_items.json"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def _load_json(path: Path) -> dict | list:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_like_history(history: dict):
    LIKE_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    LIKE_HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


async def _search_via_box(page: Page, keyword: str) -> bool:
    """画面上部の検索ボックスで検索する（ランク条件の「検索機能を使って商品検索」に当たる操作）"""
    try:
        await page.goto("https://room.rakuten.co.jp/items", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)
        box = page.locator('input#search-box, input[type="search"]').first
        if not await box.is_visible(timeout=5000):
            logger.warning("検索ボックスが見つからない → 検索URLに直接移動")
            return False
        await box.click()
        await box.fill(keyword)
        await page.wait_for_timeout(500)
        await box.press("Enter")
        await page.wait_for_url(re.compile(r"/search/item"), timeout=20000)
        await page.wait_for_load_state("networkidle", timeout=20000)
        logger.info(f"検索ボックスから検索: 「{keyword}」 → {page.url[:80]}")
        return True
    except Exception as e:
        logger.warning(f"検索ボックス操作に失敗: {e}")
        return False


async def _like_cards_on_page(page: Page, my_room_user_id: str, history: dict, remaining: int, action_delay: int) -> int:
    """表示中の検索結果でいいねを押す。押した件数を返す"""
    await page.wait_for_timeout(3000)
    for _ in range(2):
        await page.evaluate("window.scrollBy(0, 1200)")
        await page.wait_for_timeout(1500)

    cards = await page.evaluate("""(myId) => {
        const out = [];
        document.querySelectorAll('a.icon-like.right').forEach((btn, i) => {
            const card = btn.closest('.item-new-holder, .item, li, .slot') || btn.parentElement;
            const owner = card ? card.querySelector('a[href*="/items"]') : null;
            const ownerId = owner ? (owner.getAttribute('href') || '').replace(/^\\//, '').split('/')[0] : '';
            const img = card ? card.querySelector('img.image, img') : null;
            const name = (img && img.alt) ? img.alt.slice(0, 60) : '';
            const src = img ? (img.getAttribute('src') || '') : '';
            const cls = btn.className || '';
            out.push({ i, ownerId, name, key: src.split('?')[0].slice(-80), liked: /isLiked/.test(cls), disabled: /isDisabled/.test(cls), visible: btn.offsetParent !== null });
        });
        return out;
    }""", my_room_user_id)
    logger.info(f"いいねボタン {len(cards)} 個（いいね済み {sum(1 for c in cards if c['liked'])}・自分の投稿 {sum(1 for c in cards if c['ownerId'] == my_room_user_id)}）")

    candidates = [c for c in cards if c["visible"] and not c["liked"] and not c["disabled"]
                  and c["ownerId"] and c["ownerId"] != my_room_user_id and c["key"] not in history]
    random.shuffle(candidates)
    liked = 0
    for c in candidates:
        if liked >= remaining:
            break
        btn = page.locator('a.icon-like.right').nth(c["i"])
        try:
            await btn.scroll_into_view_if_needed()
            await page.wait_for_timeout(500)
            before = await btn.get_attribute("class") or ""
            if "isLiked" in before:
                continue
            await btn.click()
            await page.wait_for_timeout(2000)
            if "login" in page.url and "room.rakuten.co.jp/search" not in page.url:
                logger.error(f"ログインページに飛ばされた（クッキー切れ）: {page.url}")
                return liked
            after = await btn.get_attribute("class") or ""
            if "isLiked" in after:
                liked += 1
                history[c["key"]] = {"at": datetime.now().isoformat(timespec="seconds"), "owner": c["ownerId"], "name": c["name"]}
                _save_like_history(history)
                logger.info(f"いいね {liked}: {c['ownerId']} / {c['name'][:30]}")
            else:
                logger.warning(f"いいねが反映されなかった: {c['ownerId']} / {c['name'][:30]}（class: {after[:60]}）")
                try:
                    await page.screenshot(path="logs/like_failed.png")
                except Exception:
                    pass
            await page.wait_for_timeout(int((action_delay + random.uniform(1, 3)) * 1000))
        except Exception as e:
            logger.warning(f"いいね操作エラー: {e}")
            continue
    return liked


async def _capture_rank(page: Page, my_room_user_id: str):
    """my ROOM のプロフィールからランク表示を記録する（確認用）"""
    try:
        await page.goto(f"https://room.rakuten.co.jp/{my_room_user_id}/profile", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(4000)
        await page.screenshot(path="logs/rank_profile.png")
        info = await page.evaluate("""() => {
            const imgs = [...document.querySelectorAll('img[src*="rank"]')].map(i => i.getAttribute('src'));
            const links = [...document.querySelectorAll('a[href]')].filter(a => /ランク|rank/i.test((a.innerText || '') + (a.getAttribute('href') || ''))).map(a => (a.innerText || '').trim().slice(0, 20) + ' | ' + a.getAttribute('href'));
            const lines = (document.body.innerText || '').split('\\n').map(s => s.trim()).filter(s => /ランク/.test(s)).slice(0, 10);
            return { imgs, links: links.slice(0, 10), lines };
        }""")
        logger.info(f"ランク表示: 画像={info['imgs']} リンク={info['links']} 文言={info['lines']}")
        for text in ["ランクについて", "ランク"]:
            link = page.locator(f'a:has-text("{text}")').first
            try:
                if await link.is_visible(timeout=2000):
                    await link.click()
                    await page.wait_for_timeout(5000)
                    await page.screenshot(path="logs/rank_about.png", full_page=True)
                    body = await page.evaluate("() => document.body.innerText")
                    lines = [l.strip() for l in body.split("\n") if re.search(r"ランク|達成|オリジナル写真|いいね|フォロー|検索", l)]
                    logger.info(f"ランクについて（{page.url[:80]}）: " + " / ".join(lines[:25]))
                    break
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"ランク表示の取得に失敗: {e}")


async def run_auto_like(
    my_room_user_id: str,
    search_keywords: list[str],
    max_likes: int = 5,
    action_delay: int = 2,
    headless: bool = True,
) -> int:
    """自動いいねを実行。押した件数を返す"""
    cookies = _load_json(COOKIES_FILE)
    if not cookies:
        logger.error("クッキーがありません")
        return 0
    history = _load_json(LIKE_HISTORY_FILE) or {}
    keywords = list(search_keywords)
    random.shuffle(keywords)

    async with async_playwright() as p:
        launch_args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"] if os.getenv("CI") else []
        browser = await p.chromium.launch(headless=headless, args=launch_args)
        context = await browser.new_context(viewport={"width": 1280, "height": 900}, user_agent=UA)
        await context.add_cookies(cookies)
        page = await context.new_page()
        liked_total = 0
        try:
            for keyword in keywords[:3]:
                if liked_total >= max_likes:
                    break
                ok = await _search_via_box(page, keyword)
                if not ok:
                    await page.goto(f"https://room.rakuten.co.jp/search/item?keyword={quote(keyword)}", wait_until="domcontentloaded", timeout=60000)
                    await page.wait_for_timeout(4000)
                if "login" in page.url and "room.rakuten.co.jp/search" not in page.url:
                    logger.error(f"ログインページに飛ばされた（クッキー切れ）: {page.url}")
                    break
                liked_total += await _like_cards_on_page(page, my_room_user_id, history, max_likes - liked_total, action_delay)
            await _capture_rank(page, my_room_user_id)
        except Exception as e:
            logger.error(f"自動いいね中にエラー: {e}")
            try:
                await page.screenshot(path="logs/like_error.png")
            except Exception:
                pass
        finally:
            await browser.close()
    logger.info(f"自動いいね完了: {liked_total}件")
    return liked_total
