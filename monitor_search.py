"""
条件指定 賃貸物件 監視スクリプト
Bot1: 敷金礼金なし → 「敷金礼金なし新着」トピックに通知
Bot2: 全条件一致  → 「全条件一致新着」トピックに通知

対象サイト: SUUMO / アットホーム / goodroom
条件:
- 家賃17万以下（管理費別）
- 1LDK以上 / 45m²以上 / 駅徒歩15分以内
- 2階以上（テラスハウス・メゾネット除く）
- 軽量鉄骨造以外
- 南向き・ガスコンロ・エアコンは通知に注記
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_GROUP_ID  = os.environ.get("TELEGRAM_GROUP_ID", "")
JST = timezone(timedelta(hours=9))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

ALLOWED_STATIONS = {
    "御茶ノ水", "水道橋", "飯田橋", "市ヶ谷", "四ツ谷", "信濃町", "千駄ヶ谷",
    "代々木", "東中野", "中野", "高円寺", "阿佐ヶ谷", "荻窪", "西荻窪", "吉祥寺",
    "三鷹", "武蔵境", "東小金井", "武蔵小金井", "国分寺",
    "東長崎", "江古田", "桜台", "練馬", "中村橋", "富士見台", "練馬高野台",
    "石神井公園", "大泉学園",
    "高田馬場", "下落合", "中井", "新井薬師前", "沼袋", "野方", "都立家政",
    "鷺ノ宮", "下井草", "井荻", "上井草", "上石神井",
    "門前仲町", "代々木公園", "代々木上原", "戸越", "五反田", "高輪台", "泉岳寺",
    "三田", "大門", "新橋", "東銀座", "宝町", "日本橋", "人形町", "東日本橋",
    "浅草橋", "蔵前", "曙橋", "九段下", "神保町", "小川町", "岩本町",
    "馬喰横山", "浜町", "森下", "両国", "清澄白河", "月島",
    "初台", "幡ヶ谷", "笹塚", "代田橋", "明大前", "下高井戸", "桜上水", "上北沢",
    "八幡山", "芦花公園", "千歳烏山", "仙川",
    "下北沢", "新代田", "永福町", "西永福", "南新宿", "参宮橋", "代々木八幡",
    "東北沢", "世田谷代田", "梅ヶ丘", "豪徳寺", "経堂",
    "代官山", "中目黒", "祐天寺", "学芸大学", "都立大学", "自由が丘", "田園調布",
    "池尻大橋", "三軒茶屋", "駒沢大学", "桜新町", "用賀", "二子玉川", "戸越銀座",
}

LIGHT_STEEL_KEYWORDS = ["軽量鉄骨", "軽鉄造"]
SMALL_LAYOUT = re.compile(r"^(ワンルーム|1R|1K|1DK)$")


def _find_station_in_text(text: str) -> str:
    """テキスト中に含まれる許可駅名を返す。見つからなければ空文字。"""
    for s in ALLOWED_STATIONS:
        if s in text:
            return s
    return ""

# 対象駅を含む東京都区
WARD_CODES = [
    "13101",  # 千代田区
    "13102",  # 中央区
    "13103",  # 港区
    "13104",  # 新宿区
    "13106",  # 台東区
    "13107",  # 墨田区
    "13108",  # 江東区
    "13109",  # 品川区
    "13110",  # 目黒区
    "13112",  # 世田谷区
    "13113",  # 渋谷区
    "13114",  # 中野区
    "13115",  # 杉並区
    "13116",  # 豊島区
    "13120",  # 練馬区
]

SEARCH_CONFIGS = [
    {
        "name":       "bot1",
        "label":      "🔑【敷金礼金なし】",
        "state_file": os.path.join(SCRIPT_DIR, "seen_bot1.json"),
        "thread_id":  int(os.environ.get("TELEGRAM_THREAD_BOT1", "0")),
        "no_deposit": True,
    },
    {
        "name":       "bot2",
        "label":      "🏠【全条件一致】",
        "state_file": os.path.join(SCRIPT_DIR, "seen_bot2.json"),
        "thread_id":  int(os.environ.get("TELEGRAM_THREAD_BOT2", "0")),
        "no_deposit": False,
    },
]


# ── 状態管理 ──────────────────────────────────────────────────────────────────

def load_seen(state_file: str) -> set:
    if os.path.exists(state_file):
        with open(state_file, encoding="utf-8") as f:
            return set(json.load(f).get("ids", []))
    return set()


def save_seen(state_file: str, ids: set) -> None:
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(
            {"ids": sorted(ids), "updated": datetime.now(JST).isoformat()},
            f, ensure_ascii=False, indent=2,
        )


# ── Telegram 通知 ────────────────────────────────────────────────────────────

def notify_telegram(message: str, thread_id: int) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_GROUP_ID:
        print(f"[Telegram] 未設定。\n{message}")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id":           TELEGRAM_GROUP_ID,
                "text":              message,
                "message_thread_id": thread_id,
            },
            timeout=15,
        )
        return resp.status_code == 200
    except Exception as e:
        print(f"[Telegram] エラー: {e}")
        return False


# ── Playwright ───────────────────────────────────────────────────────────────

def _fetch_html(
    browser,
    url: str,
    debug_name: str = "",
    wait_selector: str = "",
    wait_ms: int = 3000,
) -> BeautifulSoup | None:
    try:
        page = browser.new_page()
        page.set_extra_http_headers({
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
            "Referer": "https://www.google.co.jp/",
        })
        response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
        print(f"[Browser] HTTP {response.status}: {url[:100]}")
        if wait_selector:
            try:
                page.wait_for_selector(wait_selector, timeout=10000)
            except PlaywrightTimeout:
                pass
        else:
            page.wait_for_timeout(wait_ms)
        html = page.content()
        page.close()
        if debug_name:
            with open(os.path.join(SCRIPT_DIR, f"debug_{debug_name}.html"), "w", encoding="utf-8") as f:
                f.write(html)
        return BeautifulSoup(html, "lxml")
    except PlaywrightTimeout:
        print(f"[Browser] タイムアウト: {url}")
        return None
    except Exception as e:
        print(f"[Browser] エラー: {e}")
        return None


# ── フィルタ ─────────────────────────────────────────────────────────────────

def _parse_floor(text: str) -> int | None:
    m = re.search(r"(\d+)階", text or "")
    return int(m.group(1)) if m else None


def _parse_area(text: str) -> float | None:
    m = re.search(r"([\d.]+)\s*m", text or "")
    return float(m.group(1)) if m else None


def _passes(listing: dict) -> bool:
    station = listing.get("station", "") or ""
    if station and not any(s in station for s in ALLOWED_STATIONS):
        return False
    layout = listing.get("layout", "") or ""
    if layout and SMALL_LAYOUT.match(layout):
        return False
    structure = listing.get("structure", "") or ""
    if any(kw in structure for kw in LIGHT_STEEL_KEYWORDS):
        return False
    area_val = _parse_area(listing.get("area", ""))
    if area_val is not None and area_val < 45:
        return False
    floor_num = _parse_floor(listing.get("floor", ""))
    if floor_num is not None and floor_num < 2:
        building = listing.get("building", "") or ""
        if not any(kw in building for kw in ["テラスハウス", "メゾネット"]):
            return False
    return True


# ── SUUMO スクレイパー ────────────────────────────────────────────────────────

def _build_suumo_url(no_deposit: bool, page: int) -> str:
    params = ["ar=030", "bs=040", "ta=13"]
    params += [f"ku={w}" for w in WARD_CODES]
    # mb=45 を除去 → Python側の _passes() で面積フィルタ
    params += ["cb=0", "ct=17.0", "et=15"]
    if no_deposit:
        params.append("shkn2=0101")
    if page > 1:
        params.append(f"pn={page}")
    return "https://suumo.jp/jj/chintai/ichiran/FR301FC001/?" + "&".join(params)


def scrape_suumo(browser, no_deposit: bool, max_pages: int = 5) -> list[dict]:
    tag = "suumo_bot1" if no_deposit else "suumo_bot2"
    results = []

    for page_num in range(1, max_pages + 1):
        url = _build_suumo_url(no_deposit, page_num)
        soup = _fetch_html(
            browser, url,
            debug_name=f"{tag}_p{page_num}" if page_num <= 2 else "",
            wait_selector=".cassetteitem",
            wait_ms=5000,
        )
        if not soup:
            break

        items = soup.select(".cassetteitem")
        if not items:
            print(f"[SUUMO] p{page_num}: 物件なし → 終了")
            print(f"[SUUMO] ページテキスト抜粋: {soup.get_text()[:200]}")
            break

        print(f"[SUUMO] p{page_num}: {len(items)} 棟")

        for building in items:
            title_el = building.select_one(".cassetteitem_content-title")
            addr_el  = building.select_one(".cassetteitem_detail-col1")
            col3_el  = building.select_one(".cassetteitem_detail-col3")
            st_els   = building.select(".cassetteitem_detail-col2 .cassetteitem_detail-text")

            building_name = title_el.get_text(strip=True) if title_el else ""
            address       = addr_el.get_text(strip=True)  if addr_el  else ""
            structure     = col3_el.get_text(strip=True)  if col3_el  else ""
            station_text  = "　".join(el.get_text(strip=True) for el in st_els)

            for row in building.select("tbody tr"):
                link_el = row.select_one("a[href*='/chintai/'], a[href*='jnc_']")
                if not link_el:
                    continue
                href = link_el.get("href", "")
                if not href:
                    continue

                price_el  = row.select_one(".cassetteitem_price--rent")
                layout_el = row.select_one(".cassetteitem_madori")
                area_el   = row.select_one(".cassetteitem_menseki")
                floor_el  = row.select_one(".cassetteitem_floor")

                listing = {
                    "id":        f"suumo:{href.split('?')[0]}",
                    "source":    "SUUMO",
                    "building":  building_name,
                    "address":   address,
                    "station":   station_text,
                    "structure": structure,
                    "price":     price_el.get_text(strip=True)  if price_el  else "",
                    "layout":    layout_el.get_text(strip=True) if layout_el else "",
                    "area":      area_el.get_text(strip=True)   if area_el   else "",
                    "floor":     floor_el.get_text(strip=True)  if floor_el  else "",
                    "url":       f"https://suumo.jp{href}" if href.startswith("/") else href,
                }

                if _passes(listing):
                    results.append(listing)

        time.sleep(2)

    print(f"[SUUMO] 条件一致: {len(results)} 件")
    return results


# ── アットホーム スクレイパー ─────────────────────────────────────────────────

def _build_athome_url(no_deposit: bool, page: int) -> str:
    params = [
        "MAXRENT=170000",   # 家賃上限（円）
        "MENSEKI=45,",      # 面積下限
        "WALK=15",          # 徒歩分数上限
    ]
    if no_deposit:
        params += ["SHIKIKIN=0", "REIKIN=0"]  # 敷金0・礼金0
    if page > 1:
        params.append(f"page={page}")
    return "https://www.athome.co.jp/chintai/tokyo/list/?" + "&".join(params)


def scrape_athome(browser, no_deposit: bool, max_pages: int = 5) -> list[dict]:
    tag = "athome_bot1" if no_deposit else "athome_bot2"
    results = []

    for page_num in range(1, max_pages + 1):
        url = _build_athome_url(no_deposit, page_num)
        soup = _fetch_html(browser, url, debug_name=f"{tag}_p{page_num}" if page_num <= 2 else "")
        if not soup:
            break

        items = soup.select(
            ".itemCassette, .m-cassette__item, [class*='property-list__item'], "
            "li[class*='property'], article[class*='property']"
        )
        if not items:
            print(f"[athome] p{page_num}: 物件なし → 終了")
            break

        print(f"[athome] p{page_num}: {len(items)} 件")

        for item in items:
            link_el = item.select_one("a[href*='athome.co.jp'], a[href^='/']")
            if not link_el:
                continue
            href = link_el.get("href", "")
            if not href:
                continue
            if href.startswith("/"):
                href = "https://www.athome.co.jp" + href

            full_text    = item.get_text(" ", strip=True)
            name_el      = item.select_one("[class*='buildingName'], [class*='building-name'], h2, h3")
            price_el     = item.select_one("[class*='price'], [class*='rent'], [class*='Price']")
            layout_el    = item.select_one("[class*='madori'], [class*='layout'], [class*='type']")
            area_el      = item.select_one("[class*='menseki'], [class*='area'], [class*='size']")
            floor_el     = item.select_one("[class*='floor'], [class*='kai']")
            structure_el = item.select_one("[class*='structure'], [class*='kozo']")

            listing = {
                "id":        f"athome:{href}",
                "source":    "アットホーム",
                "building":  name_el.get_text(strip=True)      if name_el      else "",
                "address":   "",
                "station":   _find_station_in_text(full_text),
                "structure": structure_el.get_text(strip=True) if structure_el else "",
                "price":     price_el.get_text(strip=True)     if price_el     else "",
                "layout":    layout_el.get_text(strip=True)    if layout_el    else "",
                "area":      area_el.get_text(strip=True)      if area_el      else "",
                "floor":     floor_el.get_text(strip=True)     if floor_el     else "",
                "url":       href,
            }

            if _passes(listing):
                results.append(listing)

        time.sleep(2)

    print(f"[athome] 条件一致: {len(results)} 件")
    return results


# ── goodroom スクレイパー ─────────────────────────────────────────────────────

def _build_goodroom_url(no_deposit: bool, page: int) -> str:
    params = [
        "rent_max=170000",  # 家賃上限（円）
        "size_from=45",     # 面積下限
        "walk_max=15",      # 徒歩分数上限
        "layout[]=1ldk", "layout[]=2k", "layout[]=2dk", "layout[]=2ldk",
        "layout[]=3k", "layout[]=3dk", "layout[]=3ldk",
    ]
    if no_deposit:
        params.append("zero_shiki_rei=1")
    if page > 1:
        params.append(f"page={page}")
    return "https://goodrooms.jp/tokyo/rooms/?" + "&".join(params)


def scrape_goodroom(browser, no_deposit: bool, max_pages: int = 5) -> list[dict]:
    tag = "goodroom_bot1" if no_deposit else "goodroom_bot2"
    results = []

    for page_num in range(1, max_pages + 1):
        url = _build_goodroom_url(no_deposit, page_num)
        soup = _fetch_html(browser, url, debug_name=f"{tag}_p{page_num}" if page_num <= 2 else "")
        if not soup:
            break

        items = soup.select(
            "[class*='room-list__item'], [class*='property-card'], "
            "[class*='roomCard'], li[class*='room'], article[class*='room']"
        )
        if not items:
            print(f"[goodroom] p{page_num}: 物件なし → 終了")
            break

        print(f"[goodroom] p{page_num}: {len(items)} 件")

        for item in items:
            link_el = item.select_one("a[href*='goodrooms.jp'], a[href^='/']")
            if not link_el:
                continue
            href = link_el.get("href", "")
            if not href:
                continue
            if href.startswith("/"):
                href = "https://goodrooms.jp" + href

            full_text = item.get_text(" ", strip=True)
            name_el   = item.select_one("[class*='name'], [class*='title'], h2, h3")
            price_el  = item.select_one("[class*='price'], [class*='rent']")
            layout_el = item.select_one("[class*='layout'], [class*='madori'], [class*='type']")
            area_el   = item.select_one("[class*='area'], [class*='size'], [class*='menseki']")
            floor_el  = item.select_one("[class*='floor'], [class*='kai']")

            listing = {
                "id":        f"goodroom:{href}",
                "source":    "goodroom",
                "building":  name_el.get_text(strip=True)  if name_el  else "",
                "address":   "",
                "station":   _find_station_in_text(full_text),
                "structure": "",
                "price":     price_el.get_text(strip=True)  if price_el  else "",
                "layout":    layout_el.get_text(strip=True) if layout_el else "",
                "area":      area_el.get_text(strip=True)   if area_el   else "",
                "floor":     floor_el.get_text(strip=True)  if floor_el  else "",
                "url":       href,
            }

            if _passes(listing):
                results.append(listing)

        time.sleep(2)

    print(f"[goodroom] 条件一致: {len(results)} 件")
    return results


# ── フォーマット ──────────────────────────────────────────────────────────────

def format_listing(listing: dict, label: str, now_jst: str) -> str:
    station_short = listing["station"].split("　")[0] if listing["station"] else ""
    return "\n".join([
        f"{label} 新着物件",
        "",
        f"物件名: {listing['building']}",
        f"サイト: {listing['source']}",
        f"家賃: {listing['price']}",
        f"間取り: {listing['layout']}　面積: {listing['area']}",
        f"階数: {listing['floor']}",
        f"最寄駅: {station_short}",
        f"住所: {listing['address']}",
        f"URL: {listing['url']}",
        "",
        "⚠️ 南向き・ガスコンロ・エアコンは詳細ページで確認してください",
        f"チェック時刻: {now_jst}",
    ])


# ── メイン ────────────────────────────────────────────────────────────────────

def main() -> None:
    now_jst = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    print(f"=== 条件物件監視開始: {now_jst} ===")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
        )

        for config in SEARCH_CONFIGS:
            name      = config["name"]
            label     = config["label"]
            thread_id = config["thread_id"]
            no_dep    = config["no_deposit"]
            print(f"\n=== {name} ===")

            seen_ids   = load_seen(config["state_file"])
            is_initial = len(seen_ids) == 0

            listings: list[dict] = []
            for scraper in [scrape_suumo, scrape_athome, scrape_goodroom]:
                listings.extend(scraper(context, no_deposit=no_dep))
                time.sleep(2)

            # 同一URLの重複を除去
            seen_in_run: set[str] = set()
            unique = []
            for l in listings:
                if l["id"] not in seen_in_run:
                    seen_in_run.add(l["id"])
                    unique.append(l)
            listings = unique

            print(f"[{name}] 合計条件一致: {len(listings)} 件")

            if is_initial:
                seen_ids.update(l["id"] for l in listings)
                save_seen(config["state_file"], seen_ids)
                notify_telegram(
                    f"{label} 監視開始\n"
                    f"現在 {len(listings)} 件の対象物件を確認しました。\n"
                    f"今後、新着物件が見つかった場合にお知らせします。\n"
                    f"開始時刻: {now_jst}",
                    thread_id,
                )
                print(f"[{name}] 初回: {len(listings)} 件を既読に設定")
            else:
                new_listings = [l for l in listings if l["id"] not in seen_ids]
                print(f"[{name}] 新規: {len(new_listings)} 件")

                for listing in new_listings:
                    success = notify_telegram(format_listing(listing, label, now_jst), thread_id)
                    print(f"[Telegram {'OK' if success else '失敗'}] {listing['id']}")
                    time.sleep(1)

                if new_listings:
                    seen_ids.update(l["id"] for l in new_listings)
                    save_seen(config["state_file"], seen_ids)
                elif listings:
                    print(f"[{name}] 新規物件なし")
                else:
                    print(f"[{name}] ⚠️ 物件情報を取得できませんでした")

            time.sleep(3)

        browser.close()

    print("\n=== 監視完了 ===")


if __name__ == "__main__":
    main()
    sys.exit(0)
