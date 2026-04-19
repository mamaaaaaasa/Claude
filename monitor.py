"""
上井草グリーンハイツ 新規物件監視スクリプト
SUUMO・アットホーム・HOMES を巡回し、新規掲載があればLINEに通知する。

セットアップ:
  1. LINE Notify でトークンを取得: https://notify-bot.line.me/
  2. GitHub Secrets に LINE_NOTIFY_TOKEN を登録
  3. GitHub Actions が2時間ごとに自動実行
"""

import json
import os
import sys
import time
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

BUILDING_NAME = "上井草グリーンハイツ"
LINE_NOTIFY_TOKEN = os.environ.get("LINE_NOTIFY_TOKEN", "")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_listings.json")
JST = timezone(timedelta(hours=9))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ── 状態管理 ──────────────────────────────────────────────────────────────────

def load_seen() -> set:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return set(json.load(f).get("ids", []))
    return set()


def save_seen(ids: set) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {"ids": sorted(ids), "updated": datetime.now(JST).isoformat()},
            f,
            ensure_ascii=False,
            indent=2,
        )


# ── LINE 通知 ─────────────────────────────────────────────────────────────────

def notify_line(message: str) -> bool:
    if not LINE_NOTIFY_TOKEN:
        print(f"[LINE] トークン未設定。メッセージ:\n{message}")
        return False
    try:
        resp = requests.post(
            "https://notify-api.line.me/api/notify",
            headers={"Authorization": f"Bearer {LINE_NOTIFY_TOKEN}"},
            data={"message": f"\n{message}"},
            timeout=15,
        )
        return resp.status_code == 200
    except Exception as e:
        print(f"[LINE] 通知エラー: {e}")
        return False


# ── スクレイパー ──────────────────────────────────────────────────────────────

def _get(url: str, timeout: int = 30) -> BeautifulSoup | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.encoding = resp.apparent_encoding or "utf-8"
        if resp.status_code != 200:
            print(f"[HTTP] {resp.status_code}: {url}")
            return None
        return BeautifulSoup(resp.text, "lxml")
    except Exception as e:
        print(f"[HTTP] エラー ({url}): {e}")
        return None


def scrape_suumo() -> list[dict]:
    """SUUMO: 物件名で検索（東京都）"""
    results = []
    url = (
        "https://suumo.jp/jj/chintai/ichiran/FR301FC001/"
        f"?ar=030&bs=040&ta=13&fw2={quote(BUILDING_NAME)}"
    )
    soup = _get(url)
    if not soup:
        return results

    for building in soup.select(".cassetteitem"):
        title_el = building.select_one(".cassetteitem_content-title")
        if not title_el or BUILDING_NAME not in title_el.get_text():
            continue

        building_name = title_el.get_text(strip=True)
        address_el = building.select_one(".cassetteitem_detail-col1")
        address = address_el.get_text(strip=True) if address_el else ""

        for row in building.select("tbody tr.js-cassette_link_href"):
            link_el = row.select_one("td.ui-text--midium a, a[href*='/chintai/']")
            if not link_el:
                link_el = row.select_one("a[href]")
            if not link_el:
                continue

            href = link_el.get("href", "")
            if not href:
                continue

            price_el = row.select_one(".cassetteitem_price--rent")
            layout_el = row.select_one(".cassetteitem_madori")
            area_el = row.select_one(".cassetteitem_menseki")
            floor_el = row.select_one(".cassetteitem_floor")

            listing_id = f"suumo:{href}"
            results.append({
                "id": listing_id,
                "source": "SUUMO",
                "building": building_name,
                "address": address,
                "price": price_el.get_text(strip=True) if price_el else "不明",
                "layout": layout_el.get_text(strip=True) if layout_el else "",
                "area": area_el.get_text(strip=True) if area_el else "",
                "floor": floor_el.get_text(strip=True) if floor_el else "",
                "url": f"https://suumo.jp{href}" if href.startswith("/") else href,
            })

    print(f"[SUUMO] {len(results)} 件取得")
    return results


def scrape_athome() -> list[dict]:
    """アットホーム: 物件名で検索"""
    results = []
    url = f"https://www.athome.co.jp/chintai/list/?BNAM={quote(BUILDING_NAME)}&PREF=13"
    soup = _get(url)
    if not soup:
        return results

    for item in soup.select("li.property-unit, div.cassette-property, article.property"):
        name_el = item.select_one(
            ".property-name-title, h2.ttl, .building-name, .casset-property__name"
        )
        if not name_el or BUILDING_NAME not in name_el.get_text():
            continue

        link_el = item.select_one("a[href]")
        if not link_el:
            continue

        href = link_el["href"]
        price_el = item.select_one(".price-num, .priceLabel, .cassette-price__num")
        layout_el = item.select_one(".detail-madori, .layout, .cassette-detail__madori")
        area_el = item.select_one(".detail-menseki, .area, .cassette-detail__menseki")

        listing_id = f"athome:{href}"
        results.append({
            "id": listing_id,
            "source": "アットホーム",
            "building": name_el.get_text(strip=True),
            "price": price_el.get_text(strip=True) if price_el else "不明",
            "layout": layout_el.get_text(strip=True) if layout_el else "",
            "area": area_el.get_text(strip=True) if area_el else "",
            "url": href if href.startswith("http") else f"https://www.athome.co.jp{href}",
        })

    print(f"[アットホーム] {len(results)} 件取得")
    return results


def scrape_homes() -> list[dict]:
    """ライフルホームズ: 物件名で検索"""
    results = []
    url = f"https://www.homes.co.jp/chintai/list/?bukken_name={quote(BUILDING_NAME)}&pref=13"
    soup = _get(url)
    if not soup:
        return results

    for item in soup.select(
        ".mod-mergeBuilding--item, .prg-cassette, li[class*='property']"
    ):
        name_el = item.select_one(
            ".mod-mergeBuilding--nameText, .prg-cassette__buildingName, h2, h3"
        )
        if not name_el or BUILDING_NAME not in name_el.get_text():
            continue

        link_el = item.select_one("a[href]")
        if not link_el:
            continue

        href = link_el["href"]
        price_el = item.select_one(
            ".mod-mergeBuilding--rent, .prg-cassette__rent, .price"
        )
        layout_el = item.select_one(
            ".mod-mergeBuilding--layout, .prg-cassette__layout, .layout"
        )
        area_el = item.select_one(
            ".mod-mergeBuilding--area, .prg-cassette__area, .area"
        )

        listing_id = f"homes:{href}"
        results.append({
            "id": listing_id,
            "source": "HOMES",
            "building": name_el.get_text(strip=True),
            "price": price_el.get_text(strip=True) if price_el else "不明",
            "layout": layout_el.get_text(strip=True) if layout_el else "",
            "area": area_el.get_text(strip=True) if area_el else "",
            "url": href if href.startswith("http") else f"https://www.homes.co.jp{href}",
        })

    print(f"[HOMES] {len(results)} 件取得")
    return results


# ── フォーマット ──────────────────────────────────────────────────────────────

def format_listing(listing: dict) -> str:
    lines = [f"【{listing['source']}】{listing['building']}"]
    if listing.get("price"):
        lines.append(f"家賃: {listing['price']}")
    if listing.get("layout"):
        lines.append(f"間取り: {listing['layout']}")
    if listing.get("area"):
        lines.append(f"面積: {listing['area']}")
    if listing.get("floor"):
        lines.append(f"階数: {listing['floor']}")
    if listing.get("address"):
        lines.append(f"住所: {listing['address']}")
    lines.append(f"URL: {listing['url']}")
    return "\n".join(lines)


# ── メイン ────────────────────────────────────────────────────────────────────

def main() -> int:
    now_jst = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    print(f"=== 物件監視開始: {now_jst} ===")

    seen_ids = load_seen()

    all_listings: list[dict] = []
    for scraper in [scrape_suumo, scrape_athome, scrape_homes]:
        all_listings.extend(scraper())
        time.sleep(2)  # サーバー負荷軽減

    new_listings = [l for l in all_listings if l["id"] not in seen_ids]

    print(f"合計取得: {len(all_listings)} 件 / 新規: {len(new_listings)} 件")

    if new_listings:
        for listing in new_listings:
            msg = (
                f"🏠 {BUILDING_NAME} に新着物件！\n\n"
                f"{format_listing(listing)}\n\n"
                f"チェック時刻: {now_jst}"
            )
            success = notify_line(msg)
            status = "OK" if success else "失敗"
            print(f"[LINE通知 {status}] {listing['id']}")
            time.sleep(1)

        seen_ids.update(l["id"] for l in new_listings)
        save_seen(seen_ids)

    elif not all_listings:
        print("⚠️  全サイトで物件情報を取得できませんでした（スクレイピング失敗の可能性）")
        # サイト構造変更の可能性をLINEで警告（1日1回程度に抑えたい場合は別途制御）
        notify_line(
            f"⚠️ {BUILDING_NAME} 監視\n"
            "全サイトの物件情報を取得できませんでした。\n"
            "サイト構造が変更された可能性があります。\n"
            f"チェック時刻: {now_jst}"
        )
    else:
        print("新規物件なし")

    print("=== 監視完了 ===")
    return len(new_listings)


if __name__ == "__main__":
    count = main()
    sys.exit(0)
