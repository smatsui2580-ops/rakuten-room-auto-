"""
楽天市場API経由で商品を検索・取得するモジュール

- 通常: 楽天市場商品検索API (app.rakuten.co.jp)
- API失敗時: 楽天検索RSSフィードにフォールバック
"""

import random
import re
import time
import xml.etree.ElementTree as ET
import requests
import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

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
    BASE_URL = "https://app.rakuten.co.jp/services/api/IchibaItem/Search/20170706"

    def __init__(self, app_id: str, access_key: str):
        self.app_id = app_id
        self.access_key = access_key

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
        """楽天市場APIで商品を検索。失敗時はRSSにフォールバック"""

        result = self._search_via_api(keyword, hits, min_price, max_price, min_review_count, min_review_average, sort)
        if result:
            return result

        logger.info(f"[API失敗] RSSフォールバック: {keyword}")
        return self._search_via_rss(keyword, hits, min_price, max_price)

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
            "applicationId": self.app_id,
            "hits": 30,
            "page": page,
            "sort": sort,
        }
        if min_price:
            params["minPrice"] = min_price
        if max_price:
            params["maxPrice"] = max_price

        for attempt in range(3):
            try:
                time.sleep(random.uniform(1.5, 2.5))
                response = requests.get(self.BASE_URL, params=params, timeout=10)
                if response.status_code in (503, 429):
                    wait = 10 * (2 ** attempt)
                    logger.warning(f"楽天API {response.status_code} → {wait}秒後にリトライ ({attempt+1}/3): {keyword}")
                    time.sleep(wait)
                    continue
                if not response.ok:
                    logger.error(f"楽天API {response.status_code}: {response.text[:200]}")
                    return []
                data = response.json()
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

    def _search_via_rss(
        self,
        keyword: str,
        hits: int,
        min_price: Optional[int] = None,
        max_price: Optional[int] = None,
    ) -> list[RakutenItem]:
        """楽天検索RSSフィードから商品を取得"""
        rss_url = f"https://search.rakuten.co.jp/search/mall/{quote(keyword)}/?f=1&s=6&v=3"
        try:
            time.sleep(random.uniform(1.5, 2.5))
            response = requests.get(rss_url, timeout=15, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/rss+xml, application/xml, text/xml, */*",
            })
            response.raise_for_status()
            logger.debug(f"RSS レスポンス先頭: {response.text[:300]}")

            if not response.text.strip().startswith("<"):
                logger.error(f"RSS: XMLでないレスポンス: {response.text[:300]}")
                return []

            root = ET.fromstring(response.content)
            items = []

            for entry in root.findall(".//item"):
                title = entry.findtext("title", "").strip()
                link = entry.findtext("link", "").strip()
                description = entry.findtext("description", "")

                if not link or not title:
                    continue

                # item.rakuten.co.jp/{shop}/{code}/ からコードを抽出
                m = re.search(r"item\.rakuten\.co\.jp/([^/]+)/([^/?#]+)", link)
                if not m:
                    continue
                shop_code = m.group(1)
                item_code_part = m.group(2)
                item_code = f"{shop_code}:{item_code_part}"

                # 価格を説明HTMLから抽出
                price = 0
                price_m = re.search(r"([\d,]+)\s*円", description)
                if price_m:
                    try:
                        price = int(price_m.group(1).replace(",", ""))
                    except ValueError:
                        pass

                if min_price and price and price < min_price:
                    continue
                if max_price and price and price > max_price:
                    continue

                # 画像URLを説明HTMLから抽出
                img_m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', description)
                image_url = img_m.group(1) if img_m else ""

                items.append(RakutenItem(
                    item_code=item_code,
                    item_name=title,
                    item_price=price,
                    item_url=link,
                    image_url=image_url,
                    shop_name=shop_code,
                    review_count=0,
                    review_average=0.0,
                    catch_copy="",
                    item_caption="",
                ))

                if len(items) >= hits:
                    break

            logger.info(f"[RSS] キーワード「{keyword}」→ {len(items)}件取得")
            return items

        except ET.ParseError as e:
            logger.error(f"RSS XML解析失敗 {keyword}: {e} | 先頭: {response.text[:200] if 'response' in dir() else 'N/A'}")
            return []
        except Exception as e:
            logger.error(f"RSS取得失敗 {keyword}: {e}")
            return []
