"""
楽天市場商品を検索・取得するモジュール

- 通常: Cloudflare Worker経由で楽天市場商品検索API (openapi.rakuten.co.jp)
- API失敗時: Playwrightブラウザで楽天検索ページを直接スクレイピング（IP制限回避）
"""

import asyncio
import os
import random
import re
import time
import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)


@dataclass
class RakutenItem:
    item_code: str
    item_name: str
    item_price: int
    item_url: str
    image_url: str
    shop_name: str
    review_count: int
    review_average: float
    catch_copy: str
    item_caption: str


class RakutenAPI:
    CF_WORKER_URL = "https://rakuten-proxy.matsuisho.workers.dev"

    def __init__(self, app_id: str, access_key: str):
        self.app_id = app_id
        self.access_key = access_key
        self.auth_token = os.getenv("CF_AUTH_TOKEN", "")
        self.worker_url = os.getenv("CF_WORKER_URL", self.CF_WORKER_URL)

    def search_items(
        self,
        keyword: str,
        hits: int = 5,
        min_price: Optional[int] = None,
        max_price: Optional[int] = None,
        min_review_count: int = 0,
        min_review_average: float = 0.0,
        sort: str = "-reviewCount",
    ) -> list[RakutenItem]:
        """楽天市場で商品を検索。API失敗時はPlaywrightブラウザにフォールバック"""

        result = self._search_via_api(keyword, hits, min_price, max_price, min_review_count, min_review_average, sort)
        if result:
            return result

        logger.info(f"[API失敗] ブラウザ検索フォールバック: {keyword}")
        return self._search_via_browser(keyword, hits, min_price, max_price)

    def _search_via_api(
        self,
        keyword: str,
        hits: int,
        min_price: Optional[int],
        max_price: Optional[int],
        min_review_count: int,
        min_review_average: float,
        sort: str,
    ) -> list[RakutenItem]:
        page = random.randint(1, 3)
        params = {
            "format": "json",
            "keyword": keyword,
            "hits": 30,
            "page": page,
            "sort": sort,
        }
        if min_price:
            params["minPrice"] = min_price
        if max_price:
            params["maxPrice"] = max_price

        headers = {"X-Auth-Token": self.auth_token} if self.auth_token else {}

        for attempt in range(3):
            try:
                time.sleep(random.uniform(1.5, 2.5))
                response = requests.get(self.worker_url, params=params, headers=headers, timeout=15)
                if response.status_code in (503, 429):
                    wait = 10 * (2 ** attempt)
                    logger.warning(f"楽天API {response.status_code} → {wait}秒後にリトライ ({attempt+1}/3): {keyword}")
                    time.sleep(wait)
                    continue
                if not response.ok:
                    logger.error(f"楽天API {response.status_code}: {response.text[:200]}")
                    return []
                data = response.json()
                # エラーレスポンス検知（HTTP 200でもエラーbodyを返す場合）
                if "errors" in data:
                    logger.error(f"楽天API エラー: {data['errors']}")
                    return []
                break
            except requests.RequestException as e:
                logger.error(f"楽天API呼び出し失敗: {e}")
                if attempt < 2:
                    time.sleep(10)
                    continue
                return []
        else:
            logger.warning(f"楽天API リトライ上限: {keyword}")
            return []

        items = []
        for entry in data.get("Items", []):
            item_data = entry.get("Item", {})

            review_count = item_data.get("reviewCount", 0)
            review_average = float(item_data.get("reviewAverage", 0))

            if review_count < min_review_count:
                continue
            if review_average < min_review_average:
                continue

            images = item_data.get("mediumImageUrls") or item_data.get("smallImageUrls") or []
            image_url = images[0].get("imageUrl", "") if images else ""

            items.append(RakutenItem(
                item_code=item_data.get("itemCode", ""),
                item_name=item_data.get("itemName", ""),
                item_price=item_data.get("itemPrice", 0),
                item_url=item_data.get("itemUrl", ""),
                image_url=image_url,
                shop_name=item_data.get("shopName", ""),
                review_count=review_count,
                review_average=review_average,
                catch_copy=item_data.get("catchcopy", ""),
                item_caption=item_data.get("itemCaption", "")[:300],
            ))

            if len(items) >= hits:
                break

        logger.info(f"[楽天API] キーワード「{keyword}」→ {len(items)}件取得")
        return items

    def _search_via_browser(
        self,
        keyword: str,
        hits: int,
        min_price: Optional[int] = None,
        max_price: Optional[int] = None,
    ) -> list[RakutenItem]:
        """同期ラッパー"""
        return asyncio.run(self._search_via_browser_async(keyword, hits, min_price, max_price))

    async def _search_via_browser_async(
        self,
        keyword: str,
        hits: int,
        min_price: Optional[int] = None,
        max_price: Optional[int] = None,
    ) -> list[RakutenItem]:
        """Playwrightで楽天市場検索ページを直接スクレイピング（IP制限なし）"""
        from playwright.async_api import async_playwright

        launch_args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"] if os.getenv("CI") else []

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=launch_args)
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )
            page = await context.new_page()

            try:
                search_url = f"https://search.rakuten.co.jp/search/mall/{quote(keyword)}/?s=6"
                await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3000)

                for _ in range(3):
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                    await page.wait_for_timeout(1000)

                raw_items = await page.evaluate("""() => {
                    const seen = new Set();
                    const items = [];
                    document.querySelectorAll('a[href*="item.rakuten.co.jp"]').forEach(a => {
                        const m = a.href.match(/item\\.rakuten\\.co\\.jp\\/([^/]+)\\/([^/?#]+)/);
                        if (!m) return;
                        const code = m[1] + ':' + m[2];
                        if (seen.has(code)) return;
                        seen.add(code);
                        const card = a.closest('li, [class*="item"], [class*="product"]') || a.parentElement;
                        const nameEl = card?.querySelector('[class*="title"], [class*="name"], h2, h3') || a;
                        const name = (nameEl?.innerText || a.innerText || '').trim();
                        const priceEl = card?.querySelector('[class*="price"], .important, [class*="yen"]');
                        const priceText = priceEl?.innerText || '';
                        const priceMatch = priceText.match(/[\\d,]+/);
                        const price = priceMatch ? parseInt(priceMatch[0].replace(/,/g, '')) : 0;
                        const imgEl = card?.querySelector('img');
                        const imgUrl = imgEl?.src || '';
                        if (name.length > 3) {
                            items.push({ item_code: code, item_name: name, item_price: price,
                                         item_url: a.href, image_url: imgUrl, shop_name: m[1] });
                        }
                    });
                    return items;
                }""")

                items = []
                for r in raw_items:
                    price = r.get("item_price", 0)
                    if min_price and price and price < min_price:
                        continue
                    if max_price and price and price > max_price:
                        continue
                    items.append(RakutenItem(
                        item_code=r["item_code"],
                        item_name=r["item_name"],
                        item_price=price,
                        item_url=r["item_url"],
                        image_url=r.get("image_url", ""),
                        shop_name=r["shop_name"],
                        review_count=0,
                        review_average=0.0,
                        catch_copy="",
                        item_caption="",
                    ))
                    if len(items) >= hits:
                        break

                logger.info(f"[ブラウザ検索] キーワード「{keyword}」→ {len(items)}件取得")
                return items

            except Exception as e:
                logger.error(f"ブラウザ検索失敗 {keyword}: {e}")
                return []
            finally:
                await browser.close()
