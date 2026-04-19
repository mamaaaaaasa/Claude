"""
上井草グリーンハイツ 新規物件監視スクリプト
SUUMO・アットホーム・HOMES を巡回し、新規掲載があれば Telegram に通知する。

セットアップ:
  1. Telegram で @BotFather に話しかけ /newbot でボットを作成 → BOT_TOKEN を取得
  2. 作成したボットに話しかける → Chat ID を取得
  3. GitHub Secrets に TELEGRAM_BOT_TOKEN と TELEGRAM_CHAT_ID を登録
  4. GitHub Actions が2時間ごとに自動実行
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

BUILDING_NAME = "上井草グリーンハイツ"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_listings.json")
JST = timezone(timedelta(hours=9))


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


# ── Telegram 通知 ────────────────────────────────────────────────────────────

def notify_telegram(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Telegram] トークン/Chat ID 未設定。メッセージ:\n{message}")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=15,
        )
        return resp.status_code == 200
    except Exception as e:
        print(f"[Telegram] 通知エラー: {e}")
        return False


def block_unauthorized_users() -> None:
    """自分以外からのメッセージに「利用できません」と返信してから削除する"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
            timeout=10,
        )
        if resp.status_code != 200:
            return
        updates = resp.json().get("result", [])
        for update in updates:
            msg = update.get("message", {})
            chat_id = str(msg.get("chat", {}).get("id", ""))
            update_id = update.get("update_id")
            if chat_id and chat_id != str(TELEGRAM_CHAT_ID):
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": chat_id, "text": "このbotは非公開です。"},
                    timeout=10,
                )
            # 処理済みとしてマーク
            requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                params={"offset": update_id + 1},
                timeout=10,
            )
    except Exception as e:
        print(f"[Telegram] unauthorized check エラー: {e}")


# ── Playwright ブラウザ取得 ───────────────────────────────────────────────────

def _fetch_html(browser, url: str, debug_name: str = "") -> BeautifulSoup | None:
    """ヘッドレスChromiumでページを取得してBeautifulSoupを返す"""
    try:
        page = browser.new_page()
        page.set_extra_http_headers({"Accept-Language": "ja,en-US;q=0.9,en;q=0.8"})
        response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
        print(f"[Browser] HTTP {response.status}: {url}")
        page.wait_for_timeout(3000)
        html = page.content()
        page.close()

        # デバッグ用にHTMLを保存（GitHub Actions Artifactsで確認できる）
        if debug_name:
            debug_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"debug_{debug_name}.html")
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write(html)
            print(f"[Debug] HTML保存: debug_{debug_name}.html ({len(html)} bytes)")

        return BeautifulSoup(html, "lxml")
    except PlaywrightTimeout:
        print(f"[Browser] タイムアウト: {url}")
        return None
    except Exception as e:
        print(f"[Browser] エラー ({url}): {e}")
        return None


# ── スクレイパー ──────────────────────────────────────────────────────────────

def scrape_suumo(browser) -> list[dict]:
    """SUUMO: 物件名フリーワード検索（東京都）"""
    results = []
    url = (
        "https://suumo.jp/jj/chintai/ichiran/FR301FC001/"
        f"?ar=030&bs=040&ta=13&fw2={quote(BUILDING_NAME)}"
    )
    soup = _fetch_html(browser, url, debug_name="suumo")
    if not soup:
        return results

    if BUILDING_NAME not in soup.get_text():
        print(f"[SUUMO] ページ内に '{BUILDING_NAME}' が見つかりません")
        return results

    for building in soup.select(".cassetteitem"):
        title_el = building.select_one(".cassetteitem_content-title")
        if not title_el or BUILDING_NAME not in title_el.get_text():
            continue

        building_name = title_el.get_text(strip=True)
        address_el = building.select_one(".cassetteitem_detail-col1")
        address = address_el.get_text(strip=True) if address_el else ""

        for row in building.select("tbody tr"):
            link_el = row.select_one("a[href*='/chintai/'], a[href*='jnc_']")
            if not link_el:
                continue
            href = link_el.get("href", "")
            if not href:
                continue

            price_el = row.select_one(".cassetteitem_price--rent")
            layout_el = row.select_one(".cassetteitem_madori")
            area_el   = row.select_one(".cassetteitem_menseki")
            floor_el  = row.select_one(".cassetteitem_floor")

            listing_id = f"suumo:{href}"
            results.append({
                "id":       listing_id,
                "source":   "SUUMO",
                "building": building_name,
                "address":  address,
                "price":    price_el.get_text(strip=True) if price_el else "不明",
                "layout":   layout_el.get_text(strip=True) if layout_el else "",
                "area":     area_el.get_text(strip=True)   if area_el   else "",
                "floor":    floor_el.get_text(strip=True)  if floor_el  else "",
                "url":      f"https://suumo.jp{href}" if href.startswith("/") else href,
            })

    print(f"[SUUMO] {len(results)} 件取得")
    return results


REALESTATE_DOMAINS = [
    "athome.co.jp",
    "homes.co.jp",
    "chintai.com",
    "apamanshop.com",
    "able.co.jp",
    "minimini.jp",
    "chintai.net",
    "realestate.co.jp",
    "relo-guide.jp",
    "e-chintai.com",
    "century21.jp",
    "leopalace21.com",
    "heyaagent.com",
    "irent.jp",
    "o-uccino.com",
]


def scrape_via_duckduckgo(browser) -> list[dict]:
    """DuckDuckGo で物件名を検索し、不動産サイトの結果URLを収集する。
    仲介サイトは物件名URL検索に対応していないため、ユーザーと同じく検索エンジン経由でアクセスする。
    """
    results = []
    query = f'"{BUILDING_NAME}" 賃貸'
    url = f"https://duckduckgo.com/html/?q={quote(query)}&kl=jp-jp"
    soup = _fetch_html(browser, url, debug_name="duckduckgo")
    if not soup:
        return results

    seen_urls: set[str] = set()

    for result in soup.select("div.result, .results_links, .web-result"):
        title_el = result.select_one("a.result__a, h2 a, .result__title a")
        snippet_el = result.select_one(".result__snippet, .result__body")

        if not title_el:
            continue

        title_text = title_el.get_text(strip=True)
        snippet_text = snippet_el.get_text(strip=True) if snippet_el else ""
        combined_text = title_text + " " + snippet_text

        if BUILDING_NAME not in combined_text:
            continue

        href = title_el.get("href", "")
        # DuckDuckGo sometimes wraps URLs in redirect links
        if "uddg=" in href:
            parsed = urlparse(href)
            uddg = parse_qs(parsed.query).get("uddg", [""])
            href = uddg[0] if uddg[0] else href

        if not href or not href.startswith("http"):
            continue

        # Only keep links from known real estate domains
        if not any(domain in href for domain in REALESTATE_DOMAINS):
            continue

        # Deduplicate by URL
        if href in seen_urls:
            continue
        seen_urls.add(href)

        # Extract domain as source label
        domain = urlparse(href).netloc.replace("www.", "")

        listing_id = f"search:{href}"
        results.append({
            "id":       listing_id,
            "source":   domain,
            "building": BUILDING_NAME,
            "price":    "",
            "layout":   "",
            "area":     "",
            "url":      href,
        })

    print(f"[DuckDuckGo] {len(results)} 件取得")
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
    block_unauthorized_users()

    seen_ids = load_seen()
    all_listings: list[dict] = []

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

        for scraper in [scrape_suumo, scrape_via_duckduckgo]:
            all_listings.extend(scraper(context))
            time.sleep(2)

        browser.close()

    new_listings = [l for l in all_listings if l["id"] not in seen_ids]
    print(f"合計取得: {len(all_listings)} 件 / 新規: {len(new_listings)} 件")

    if new_listings:
        for listing in new_listings:
            msg = (
                f"🏠 {BUILDING_NAME} に新着物件！\n\n"
                f"{format_listing(listing)}\n\n"
                f"チェック時刻: {now_jst}"
            )
            success = notify_telegram(msg)
            status = "OK" if success else "失敗"
            print(f"[Telegram通知 {status}] {listing['id']}")
            time.sleep(1)

        seen_ids.update(l["id"] for l in new_listings)
        save_seen(seen_ids)

    elif not all_listings:
        print("⚠️  全サイトで物件情報を取得できませんでした")
        notify_telegram(
            f"⚠️ {BUILDING_NAME} 監視\n"
            "全サイトの物件情報を取得できませんでした。\n"
            f"チェック時刻: {now_jst}"
        )
    else:
        print("新規物件なし")

    print("=== 監視完了 ===")
    return len(new_listings)


if __name__ == "__main__":
    main()
    sys.exit(0)
