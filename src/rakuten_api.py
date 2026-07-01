"""
楽天市場API経由で商品を検索・取得するモジュール
"""

import random
import time
import requests
import logging
from dataclasses import dataclass
from typing import Optional

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
    BASE_URL = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260701"

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
        """楽天市場APIで商品を検索して条件でフィルタリングして返す"""

        # hits=30 × page ≤ 100 の制限があるため最大ページは3
        page = random.randint(1, 3)

        params = {
            "format": "json",
            "keyword": keyword,
            "applicationId": self.app_id,
            "accessKey": self.access_key,
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
                    logger.error(f"楽天API {response.status_code}: {response.text[:300]}")
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
            logger.error(f"楽天API リトライ上限: {keyword}")
            return []

        items = []
        for entry in data.get("Items", []):
            item_data = entry.get("Item", {})

            review_count = item_data.get("reviewCount", 0)
            review_average = float(item_data.get("reviewAverage", 0))

            # フィルタリング
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
