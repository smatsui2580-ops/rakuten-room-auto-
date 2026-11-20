"""
楽天ROOMのコレクションを自動で作る・更新する

使い方:
  python make_collections.py                 # 振り分けの確認だけ（何も変更しない・ログイン不要）
  python make_collections.py --create 1      # 未作成のコレクションを最大1個作る
  python make_collections.py --create 10 --update   # 未作成を作り、作成済みには新しい投稿を追加する

仕組み:
  1. 公開APIから自分の投稿を全部取り、collections.yaml の条件で商品名・価格から振り分ける
  2. ログイン済みのブラウザで作成ページを開き、画面と同じ保存処理（ROOM自身のAPI呼び出し）で登録する
  3. 作ったコレクションと入れた投稿は data/collections.json に記録する
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import logging
import os
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv
from playwright.async_api import async_playwright

BASE = Path(__file__).parent
COOKIES_FILE = BASE / "data" / "rakuten_cookies.json"
STATE_FILE = BASE / "data" / "collections.json"
THEMES_FILE = BASE / "collections.yaml"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
ROOM = "https://room.rakuten.co.jp"

(BASE / "logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(BASE / "logs" / "collections.log", encoding="utf-8")],
)
logger = logging.getLogger(__name__)


# ---------- 公開APIから自分の投稿を取る ----------

def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    body = urllib.request.urlopen(req, timeout=30).read()
    try:
        body = gzip.decompress(body)
    except Exception:
        pass
    return json.loads(body)


def find_user_id(username: str) -> str:
    import re
    req = urllib.request.Request(f"{ROOM}/{username}/items", headers={"User-Agent": UA})
    html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
    m = re.search(r"user_id=(\d+)", html) or re.search(r'"user_id":"?(\d+)', html)
    if not m:
        raise RuntimeError("ユーザーIDが取得できません")
    return m.group(1)


def fetch_all_collects(user_id: str, max_pages: int = 60) -> list[dict]:
    rows, after = [], None
    for _ in range(max_pages):
        url = f"{ROOM}/api/{user_id}/collects?limit=100&api_version=1" + (f"&after_id={after}" if after else "")
        data = _get_json(url).get("data") or []
        if not data:
            break
        rows += data
        after = data[-1]["id"]
        time.sleep(0.5)
    return rows


def fetch_my_collections(user_id: str) -> list[dict]:
    try:
        return _get_json(f"{ROOM}/api/{user_id}/collections").get("data") or []
    except Exception as e:
        logger.warning(f"コレクション一覧の取得に失敗: {e}")
        return []


# ---------- 振り分け ----------

def _price(row: dict) -> int:
    try:
        return int(float((row.get("item") or {}).get("price") or 0))
    except (TypeError, ValueError):
        return 0


def _available(row: dict) -> bool:
    item = row.get("item") or {}
    return str(item.get("flg_soldout", "0")) != "1" and str(item.get("flg_deleted", "0")) != "1"


def match_theme(row: dict, theme: dict) -> bool:
    name = (row.get("item") or {}).get("name") or row.get("name") or ""
    if not any(w in name for w in theme.get("any", [])):
        return False
    if any(w in name for w in theme.get("exclude", [])):
        return False
    price = _price(row)
    if theme.get("max_price") and not (0 < price <= theme["max_price"]):
        return False
    if theme.get("min_price") and price < theme["min_price"]:
        return False
    return True


JUDGE_FILE = BASE / "data" / "collection_judgements.json"
JUDGE_MODEL = "claude-haiku-4-5-20251001"


def judge_with_ai(theme: dict, rows: list[dict], cache: dict) -> list[dict]:
    """単語で拾った候補が本当にテーマに合うかをAIに判定させる（商品名は検索用の単語が詰め込まれていて誤って拾うため）

    判定結果は cache（"テーマ:投稿ID" → true/false）に残し、同じ投稿を二度判定しない。
    """
    import anthropic

    todo = [r for r in rows if f"{theme['key']}:{r['id']}" not in cache]
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY")) if todo else None
    for start in range(0, len(todo), 25):
        batch = todo[start:start + 25]
        listing = "\n".join(
            f"{i + 1}. ¥{_price(r):,} {((r.get('item') or {}).get('name') or '')[:90]}" for i, r in enumerate(batch)
        )
        prompt = f"""楽天ROOMのコレクション「{theme['name']}」（{theme.get('about', '')}）に入れる商品を選んでいます。
次の商品それぞれについて、その商品そのものがこのコレクションの主題に合うかを判定してください。
商品名には検索用の無関係な単語が多く含まれます。単語の一致ではなく「実際に何の商品か」で判断し、迷うものは false にしてください。

{listing}

出力はJSON配列のみ（例: [true,false,...]）。要素数は{len(batch)}個。"""
        try:
            msg = client.messages.create(model=JUDGE_MODEL, max_tokens=400, messages=[{"role": "user", "content": prompt}])
            text = msg.content[0].text
            verdicts = json.loads(text[text.index("["): text.rindex("]") + 1])
            if len(verdicts) != len(batch):
                raise ValueError(f"件数が合いません {len(verdicts)}/{len(batch)}")
        except Exception as e:
            logger.warning(f"AI判定に失敗（この分は見送り）「{theme['name']}」: {e}")
            continue
        for r, v in zip(batch, verdicts):
            cache[f"{theme['key']}:{r['id']}"] = bool(v)
    return [r for r in rows if cache.get(f"{theme['key']}:{r['id']}") is True]


def assign(rows: list[dict], cfg: dict, cache: dict | None = None) -> dict[str, list[dict]]:
    """テーマごとに入れる投稿を決める（単語で候補を拾う→AIで判定→いいねが多い順に max_items 件まで）"""
    result = {}
    usable = [r for r in rows if _available(r)]
    for theme in cfg["themes"]:
        matched = [r for r in usable if match_theme(r, theme)]
        # 同じ商品が複数回投稿されている場合は1つにまとめる
        seen, uniq = set(), []
        for r in matched:
            item_id = (r.get("item") or {}).get("id") or r["id"]
            if item_id in seen:
                continue
            seen.add(item_id)
            uniq.append(r)
        uniq.sort(key=lambda r: (int(r.get("likes") or 0), r.get("created_at") or ""), reverse=True)
        if cache is not None:
            # 判定の量を抑えるため、上位の候補だけをAIに見せる
            uniq = judge_with_ai(theme, uniq[: cfg.get("judge_candidates", 75)], cache)
        result[theme["key"]] = uniq[: cfg.get("max_items", 30)]
    return result


# ---------- 状態 ----------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- ブラウザでの登録（画面と同じ保存処理を呼ぶ） ----------

JS_READY = """() => {
    try {
        const inj = angular.element(document.documentElement).injector() || angular.element(document.body).injector();
        const user = inj.get('roomUser');
        return { ok: !!(user && user.csrfToken), loggedIn: !!(user && user.user && user.user.id) };
    } catch (e) { return { ok: false, error: String(e) }; }
}"""

JS_CREATE = """async (payload) => {
    const inj = angular.element(document.documentElement).injector() || angular.element(document.body).injector();
    const api = inj.get('RoomApi');
    try {
        const res = await api.freecollection.post({}, jQuery.param(payload)).$promise;
        return { ok: true, status: res.status, data: res.data || null };
    } catch (e) {
        const d = (e && e.data) || {};
        return { ok: false, http: e && e.status, msg_code: d.msg_code || null, message: d.message || String(e) };
    }
}"""

JS_UPDATE = """async ([collectionId, payload]) => {
    const inj = angular.element(document.documentElement).injector() || angular.element(document.body).injector();
    const api = inj.get('RoomApi');
    try {
        const res = await api.editFreeCollection.update({ collectionID: collectionId }, jQuery.param(payload)).$promise;
        return { ok: true, status: res.status };
    } catch (e) {
        const d = (e && e.data) || {};
        return { ok: false, http: e && e.status, msg_code: d.msg_code || null, message: d.message || String(e) };
    }
}"""


async def open_editor(context, username: str):
    """作成ページを開き、ログイン済みで保存処理が呼べる状態か確かめる"""
    page = await context.new_page()
    await page.goto(f"{ROOM}/{username}/collection/new", wait_until="domcontentloaded", timeout=60000)
    for _ in range(20):
        await page.wait_for_timeout(1500)
        if "login" in page.url and "room.rakuten.co.jp" not in page.url.split("?")[0]:
            logger.error(f"ログインページに飛ばされた（クッキー切れ）: {page.url[:80]}")
            return None
        try:
            ready = await page.evaluate(JS_READY)
        except Exception:
            continue
        if ready.get("ok"):
            return page
    logger.error(f"作成ページの準備ができませんでした: {page.url[:80]}")
    await page.screenshot(path=str(BASE / "logs" / "collection_not_ready.png"))
    return None


async def run_browser(username: str, user_id: str, to_create: list, to_update: list, state: dict):
    cookies = json.loads(COOKIES_FILE.read_text(encoding="utf-8")) if COOKIES_FILE.exists() else None
    if not cookies:
        logger.error("クッキーがありません")
        return
    async with async_playwright() as p:
        launch_args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"] if os.getenv("CI") else []
        browser = await p.chromium.launch(headless=True, args=launch_args)
        context = await browser.new_context(viewport={"width": 1280, "height": 900}, user_agent=UA)
        await context.add_cookies(cookies)
        try:
            page = await open_editor(context, username)
            if not page:
                return

            for theme, rows in to_create:
                ids = [r["id"] for r in rows]
                payload = {"name": theme["name"], "img_icon": "", "about": theme.get("about", ""), "collects": ",".join(ids)}
                res = await page.evaluate(JS_CREATE, payload)
                if not res.get("ok"):
                    logger.error(f"作成失敗「{theme['name']}」: {res}")
                    if res.get("msg_code") == "R122":
                        logger.error("コレクション数の上限に達しました。以降の作成を止めます")
                        break
                    continue
                await page.wait_for_timeout(3000)
                # 公開の一覧で、実際にできたかを確かめる
                found = next((c for c in fetch_my_collections(user_id) if c.get("name") == theme["name"]), None)
                data = res.get("data") or {}
                cid = (found or {}).get("id") or (data.get("id") if isinstance(data, dict) else None)
                if not found:
                    logger.warning(f"作成の応答は成功でしたが、公開一覧にまだ出ていません「{theme['name']}」")
                state[theme["key"]] = {
                    "id": cid, "name": theme["name"], "collects": ids,
                    "confirmed": bool(found), "updated": datetime.now().isoformat(timespec="seconds"),
                }
                save_state(state)
                logger.info(f"作成成功「{theme['name']}」{len(ids)}件 / id={cid} / 公開一覧で確認={bool(found)}")
                await page.wait_for_timeout(4000)

            for theme, new_rows in to_update:
                entry = state[theme["key"]]
                ids = [r["id"] for r in new_rows]
                payload = {
                    "name": theme["name"], "img_icon": "", "about": theme.get("about", ""),
                    "collection_id": entry["id"], "omitted_collects": "", "added_collects": ",".join(ids),
                }
                res = await page.evaluate(JS_UPDATE, [entry["id"], payload])
                if not res.get("ok"):
                    logger.error(f"追加失敗「{theme['name']}」: {res}")
                    continue
                entry["collects"] += ids
                entry["updated"] = datetime.now().isoformat(timespec="seconds")
                save_state(state)
                logger.info(f"追加成功「{theme['name']}」+{len(ids)}件（計{len(entry['collects'])}件）")
                await page.wait_for_timeout(4000)
        except Exception as e:
            logger.error(f"コレクション処理中にエラー: {e}")
            try:
                await page.screenshot(path=str(BASE / "logs" / "collection_error.png"))
            except Exception:
                pass
        finally:
            await browser.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--create", type=int, default=0, help="未作成のコレクションを最大N個作る")
    ap.add_argument("--update", action="store_true", help="作成済みのコレクションに新しい投稿を追加する")
    args = ap.parse_args()

    load_dotenv(BASE / ".env")
    cfg = yaml.safe_load(THEMES_FILE.read_text(encoding="utf-8"))
    config = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8"))
    username = config["follow"]["my_room_user_id"]
    user_id = find_user_id(username)

    # 開発用: ROWS_CACHE を指定すると、取得した投稿をファイルに置いて使い回す
    rows_cache = os.getenv("ROWS_CACHE")
    if rows_cache and Path(rows_cache).exists():
        rows = json.loads(Path(rows_cache).read_text(encoding="utf-8"))
    else:
        rows = fetch_all_collects(user_id)
        if rows_cache:
            Path(rows_cache).write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    logger.info(f"自分の投稿: {len(rows)}件")

    judgements = {}
    if JUDGE_FILE.exists():
        try:
            judgements = json.loads(JUDGE_FILE.read_text(encoding="utf-8"))
        except Exception:
            judgements = {}
    if not os.getenv("ANTHROPIC_API_KEY"):
        logger.error("ANTHROPIC_API_KEY が未設定のため、AI判定ができません")
        return
    assigned = assign(rows, cfg, judgements)
    JUDGE_FILE.write_text(json.dumps(judgements, ensure_ascii=False), encoding="utf-8")
    state = load_state()
    themes = {t["key"]: t for t in cfg["themes"]}
    max_items = cfg.get("max_items", 30)

    to_create, to_update = [], []
    for key, picked in assigned.items():
        theme = themes[key]
        if key not in state:
            enough = len(picked) >= cfg.get("min_items", 6)
            logger.info(f"[未作成] {theme['name']}: 候補{len(picked)}件" + ("" if enough else "（少ないので作らない）"))
            for r in picked[:8]:
                logger.info(f"    ¥{_price(r):,} いいね{r.get('likes')} {((r.get('item') or {}).get('name') or '')[:40]}")
            if enough:
                to_create.append((theme, picked))
        else:
            entry = state[key]
            have = set(entry.get("collects", []))
            room_left = max(0, 100 - len(have))
            new_rows = [r for r in picked if r["id"] not in have][: min(room_left, max_items)]
            logger.info(f"[作成済] {theme['name']}: 現在{len(have)}件 / 追加候補{len(new_rows)}件")
            if new_rows and entry.get("id"):
                to_update.append((theme, new_rows))

    to_create = to_create[: args.create]
    if not args.update:
        to_update = []
    if not to_create and not to_update:
        logger.info("変更なし（確認のみ）")
        return
    asyncio.run(run_browser(username, user_id, to_create, to_update, state))


if __name__ == "__main__":
    main()
