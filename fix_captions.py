"""
楽天ROOMの既存投稿のうち、キャプション（本文）が空のものに遡ってキャプションを付けるスクリプト

仕組み:
  1. ROOM の公開API /api/<user_id>/collects を全ページたどり、content が空の投稿を洗い出す（ログイン不要）
  2. 各投稿の編集ページ https://room.rakuten.co.jp/<username>/<collect_id>/edit を開き、
     投稿時と同じ AngularJS フォームにキャプションを入力して「完了」を押す（要クッキー）
  3. 結果は data/fixed_captions.json に記録し、次回以降はスキップする

使い方:
  python fix_captions.py --scan             # 本文なし投稿を数えるだけ（ログイン不要）
  python fix_captions.py --fix 20           # 本文なし投稿を新しい順に20件修復
  python fix_captions.py --fix 1 --dry-run  # 入力して画面を保存するだけ。「完了」は押さない
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page

from src.room_poster import _wait_for_form_loaded, _caption_counter, _save_cookies

load_dotenv()
Path("logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/fix_captions.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

ROOM = "https://room.rakuten.co.jp"
COOKIES_FILE = Path("data/rakuten_cookies.json")
RESULT_FILE = Path("data/fixed_captions.json")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
MAX_ATTEMPTS = 2  # 失敗した投稿を再試行する回数


def load_config() -> dict:
    with open("config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_results() -> dict:
    if not RESULT_FILE.exists():
        return {}
    try:
        return json.loads(RESULT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_results(results: dict):
    RESULT_FILE.parent.mkdir(parents=True, exist_ok=True)
    RESULT_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. 公開APIで本文なし投稿を洗い出す
# ---------------------------------------------------------------------------

def resolve_user_id(username: str) -> str:
    """ROOM のプロフィールページから数値のユーザーIDを取り出す"""
    r = requests.get(f"{ROOM}/{username}/items", headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    m = re.search(r"user_id=(\d{10,})", r.text)
    if not m:
        raise RuntimeError("ユーザーIDがページから取れませんでした")
    return m.group(1)


def iter_collects(user_id: str):
    """/api/<user_id>/collects を after_id で新しい順にページ送りしながら1件ずつ返す"""
    after_id, page = None, 0
    while True:
        url = f"{ROOM}/api/{user_id}/collects?limit=20&api_version=1"
        if after_id:
            url += f"&after_id={after_id}"
        data = None
        for attempt in range(4):
            try:
                r = requests.get(url, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=30)
                r.raise_for_status()
                data = r.json().get("data", [])
                break
            except Exception as e:
                logger.warning(f"collects取得リトライ {attempt + 1}/4: {e}")
                time.sleep(3 * (attempt + 1))
        if data is None:
            raise RuntimeError("collects API の取得に失敗しました")
        if not data:
            return
        page += 1
        for c in data:
            c.pop("recent_responsers", None)
            yield c
        after_id = data[-1]["id"]
        if page % 10 == 0:
            logger.info(f"  取得中… {page * 20}件（{data[-1]['created_at']} まで）")
        if len(data) < 20:
            return
        time.sleep(0.4)


def find_empty_captions(username: str, need: int | None = None, skip_ids: set | None = None) -> tuple[list[dict], int]:
    """本文なし投稿を新しい順に集める。need を指定すると（skip_ids を除いて）その件数が集まった時点で止める。
    戻り値: (本文なし投稿のリスト, 調べた投稿数)"""
    user_id = resolve_user_id(username)
    skip_ids = skip_ids or set()
    empty, scanned, usable = [], 0, 0
    for c in iter_collects(user_id):
        scanned += 1
        if (c.get("content") or "").strip():
            continue
        empty.append(c)
        if c["id"] not in skip_ids:
            usable += 1
        if need is not None and usable >= need:
            break
    logger.info(f"調べた投稿 {scanned}件 / 本文なし {len(empty)}件" + ("（必要数に達したので打ち切り）" if need is not None and usable >= need else ""))
    return empty, scanned


# ---------------------------------------------------------------------------
# 2. キャプション生成（投稿時と同じトーン。レビュー情報は無いので使わない）
# ---------------------------------------------------------------------------

def generate_caption(collect: dict, caption_cfg: dict) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    item = collect.get("item") or {}
    name = item.get("name") or collect.get("name") or ""
    price = item.get("price") or 0
    url = item.get("url") or ""
    shop = ""
    m = re.search(r"item\.rakuten\.co\.jp/([^/]+)/", url)
    if m:
        shop = m.group(1)

    hashtag_instruction = (
        f"\n- 最後に関連するハッシュタグを{caption_cfg['hashtag_count']}個追加してください（例: #コスメ #おすすめ）"
        if caption_cfg.get("add_hashtags") else ""
    )
    price_line = f"- 価格: ¥{price:,}\n" if price else ""
    prompt = f"""楽天ROOMの投稿キャプションを作成してください。

【商品情報】
- 商品名: {name}
{price_line}- ショップ: {shop}

【文章の構成（この順番で書く）】
1. 絵本のような世界観・情景が浮かぶ書き出し（1〜2行）
2. 商品の見た目・素材・使い心地の描写（2〜3行）
3. 🌟おすすめポイント を箇条書き（3〜4個、「・」で始める）
4. 締めの一言（自分へのご褒美・贈り物・日常を豊かに、など）

【ルール】
- トーン: {caption_cfg['tone']}
- 文字数: {caption_cfg['max_length']}文字以内（ハッシュタグ含む）
- 絵文字を自然に使う（1行に1〜2個程度）
- 「♡」「✨」「☕」「🌿」などほっこり系の絵文字が合う
- 商品名から分かること以外は断定しない。レビューや実績の数字は書かない{hashtag_instruction}

キャプションのみ出力してください（前置きや説明文は不要）。"""

    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        caption = message.content[0].text.strip()
        logger.info(f"[キャプション生成] {name[:30]}... → {len(caption)}文字")
        return caption
    except Exception as e:
        logger.error(f"キャプション生成失敗: {e}")
        return ""


# ---------------------------------------------------------------------------
# 3. 編集ページでキャプションを入力して保存
# ---------------------------------------------------------------------------

async def edit_caption(page: Page, username: str, collect: dict, caption: str, action_delay: int, dry_run: bool) -> str:
    """戻り値: fixed / skipped_has_caption / login_required / failed"""
    cid = collect["id"]
    item_name = (collect.get("item") or {}).get("name") or collect.get("name") or ""
    edit_url = f"{ROOM}/{username}/{cid}/edit"
    logger.info(f"編集ページへ移動: {edit_url}")
    await page.goto(edit_url, wait_until="domcontentloaded", timeout=60000)
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    await page.wait_for_timeout(3000)

    if "login" in page.url and "room.rakuten.co.jp/" + username not in page.url:
        logger.error(f"ログインページに飛ばされました（クッキー切れ）: {page.url}")
        await page.screenshot(path="logs/fix_login_required.png")
        return "login_required"

    await _wait_for_form_loaded(page, item_name)
    await page.screenshot(path="logs/fix_before_fill.png")

    # エラーダイアログ（削除済み商品など）
    try:
        body = await page.evaluate("() => document.body.innerText")
    except Exception:
        body = ""
    for ng in ["削除されています", "編集できません", "ご利用いただけません"]:
        if ng in body:
            logger.warning(f"編集不可の表示あり（{ng}）→ スキップ")
            return "failed"

    textarea = None
    for selector in ['textarea[placeholder*="オススメポイント"]', 'textarea[ng-model="content"]', 'textarea']:
        try:
            t = page.locator(selector).first
            if await t.is_visible(timeout=3000):
                textarea = t
                break
        except Exception:
            continue
    if textarea is None:
        logger.warning("テキストエリアが見つかりません")
        await page.screenshot(path="logs/fix_failed.png")
        return "failed"

    existing = await textarea.input_value()
    if existing.strip():
        logger.info(f"すでにキャプションあり（{len(existing)}文字）→ スキップ")
        return "skipped_has_caption"

    await textarea.scroll_into_view_if_needed()
    await textarea.click()
    await page.wait_for_timeout(300)
    await textarea.fill(caption)
    await page.wait_for_timeout(500)
    if len(await textarea.input_value()) == 0:
        await textarea.press_sequentially(caption, delay=5)
        await textarea.press("Tab")

    for attempt in range(1, 4):
        await page.wait_for_timeout(2500)
        value = await textarea.input_value()
        counter = await _caption_counter(page)
        if len(value) > 0 and (counter is None or counter > 0):
            logger.info(f"キャプション反映確認: value {len(value)}文字 / カウンター {counter}")
            break
        logger.warning(f"キャプションが消えている（value {len(value)} / カウンター {counter}）→ 再入力 {attempt}/3")
        await textarea.click()
        await textarea.fill("")
        await textarea.press_sequentially(caption, delay=5)
        await textarea.press("Tab")
    else:
        logger.warning("3回入れ直しても反映されない → 失敗")
        await page.screenshot(path="logs/fix_failed.png")
        return "failed"

    await page.screenshot(path="logs/fix_after_fill.png")
    if dry_run:
        logger.info("[dry-run] 「完了」は押しません")
        return "dry_run"

    await page.wait_for_timeout(action_delay * 1000)
    done_btn = page.locator('button.collect-btn, button:has-text("完了")').first
    if not await done_btn.is_visible(timeout=3000):
        logger.warning("「完了」ボタンが見つかりません")
        await page.screenshot(path="logs/fix_failed.png")
        return "failed"
    await done_btn.scroll_into_view_if_needed()
    try:
        await done_btn.click(force=True)
    except Exception:
        await page.evaluate("document.querySelector('button.collect-btn').click()")
    logger.info("「完了」をクリック")

    indicators = ['text=編集完了', 'text=コレ！完了', 'text=my ROOM を見る', 'a:has-text("my ROOM")']
    matched = ""
    for waited in range(1, 41):
        await page.wait_for_timeout(1000)
        try:
            for ind in indicators:
                if await page.locator(ind).first.is_visible():
                    matched = ind
                    break
            if matched:
                break
            # 編集ページから投稿ページに戻った場合も成功
            if page.url.rstrip("/").endswith(f"/{cid}"):
                matched = "url"
                break
        except Exception:
            continue
    await page.screenshot(path="logs/fix_after_post.png")
    if not matched:
        try:
            text = await page.evaluate("() => document.body.innerText")
            logger.warning(f"完了後の画面テキスト: {text[:200]!r}")
        except Exception:
            pass
        logger.warning("保存の確認が取れませんでした → 失敗扱い")
        return "failed"
    logger.info(f"保存確認: {matched}（{waited}秒）")
    return "fixed"


def verify_via_page(username: str, cid: str, caption: str) -> bool:
    """公開の投稿ページにキャプションが載ったかを確認する"""
    try:
        r = requests.get(f"{ROOM}/{username}/{cid}", headers={"User-Agent": UA}, timeout=30)
        text = html.unescape(r.text)
        head = re.sub(r"\s+", "", caption)[:12]
        return head in re.sub(r"\s+", "", text)
    except Exception as e:
        logger.warning(f"公開ページ確認エラー: {e}")
        return False


async def run_fix(count: int, dry_run: bool):
    config = load_config()
    username = config["follow"]["my_room_user_id"]
    caption_cfg = config["caption"]
    action_delay = config["browser"]["action_delay"]
    headless = True if os.getenv("CI") == "true" else config["browser"]["headless"]

    if not os.getenv("ANTHROPIC_API_KEY"):
        logger.error("ANTHROPIC_API_KEY が未設定です")
        return
    if not COOKIES_FILE.exists():
        logger.error("data/rakuten_cookies.json がありません")
        return

    results = load_results()
    skip_ids = {cid for cid, rec in results.items()
                if rec.get("status") == "fixed" or rec.get("attempts", 0) >= MAX_ATTEMPTS}
    empty, _ = find_empty_captions(username, need=count, skip_ids=skip_ids)
    targets = [c for c in empty if c["id"] not in skip_ids][:count]
    logger.info(f"今回の対象: {len(targets)}件")
    if not targets:
        return

    cookies = json.loads(COOKIES_FILE.read_text(encoding="utf-8"))
    launch_args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"] if os.getenv("CI") else []
    fixed = failed = 0
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, args=launch_args)
        context = await browser.new_context(viewport={"width": 1280, "height": 900}, user_agent=UA)
        await context.add_cookies(cookies)
        page = await context.new_page()
        try:
            for i, c in enumerate(targets, 1):
                cid = c["id"]
                name = (c.get("item") or {}).get("name") or c.get("name") or ""
                logger.info(f"[{i}/{len(targets)}] {cid} {c.get('created_at')} {name[:40]}")
                caption = generate_caption(c, caption_cfg)
                if not caption:
                    failed += 1
                    continue
                try:
                    status = await edit_caption(page, username, c, caption, action_delay, dry_run)
                except Exception as e:
                    logger.error(f"編集中にエラー: {e}")
                    try:
                        await page.screenshot(path="logs/fix_error.png")
                    except Exception:
                        pass
                    status = "failed"

                if status == "login_required":
                    break
                if status == "dry_run":
                    continue

                rec = results.get(cid, {"attempts": 0})
                rec["attempts"] = rec.get("attempts", 0) + 1
                rec["status"] = status
                rec["at"] = datetime.now().isoformat(timespec="seconds")
                if status == "fixed":
                    rec["verified"] = verify_via_page(username, cid, caption)
                    logger.info(f"公開ページで確認: {'OK' if rec['verified'] else '未反映（反映待ちの可能性）'}")
                    fixed += 1
                elif status == "failed":
                    failed += 1
                results[cid] = rec
                save_results(results)

                await page.wait_for_timeout(int((action_delay * 2 + random.uniform(1, 3)) * 1000))
            if fixed:
                await _save_cookies(context)
        finally:
            await browser.close()
    logger.info(f"修復完了: 成功 {fixed}件 / 失敗 {failed}件")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scan", action="store_true", help="本文なし投稿を数えるだけ")
    parser.add_argument("--fix", type=int, default=0, help="修復する件数")
    parser.add_argument("--dry-run", action="store_true", help="「完了」を押さない")
    args = parser.parse_args()

    if args.scan:
        config = load_config()
        empty, scanned = find_empty_captions(config["follow"]["my_room_user_id"])
        print(f"投稿 {scanned}件 / 本文なし {len(empty)}件")
        for c in empty[:10]:
            print(c["id"], c.get("created_at"), ((c.get("item") or {}).get("name") or "")[:50])
        return
    if args.fix > 0:
        asyncio.run(run_fix(args.fix, args.dry_run))
        return
    parser.print_help()


if __name__ == "__main__":
    main()
