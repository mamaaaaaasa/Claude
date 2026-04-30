import os
import json
from datetime import datetime
from apify_client import ApifyClient
from dotenv import load_dotenv

load_dotenv()
client = ApifyClient(os.getenv("APIFY_API_TOKEN"))


def analyze_account(username: str, max_videos: int = 30) -> dict:
    """指定アカウントの動画・プロフィールを分析"""
    run = client.actor("clockworks/tiktok-scraper").call(run_input={
        "profiles": [username],
        "resultsPerPage": max_videos,
        "scrapeType": "profile",
    })
    items = list(client.dataset(run["defaultDatasetId"]).iterate_items())

    videos = []
    for v in items:
        videos.append({
            "id": v.get("id"),
            "desc": v.get("desc", ""),
            "duration": v.get("video", {}).get("duration", 0),
            "plays": v.get("stats", {}).get("playCount", 0),
            "likes": v.get("stats", {}).get("diggCount", 0),
            "comments": v.get("stats", {}).get("commentCount", 0),
            "shares": v.get("stats", {}).get("shareCount", 0),
            "created_at": datetime.fromtimestamp(v.get("createTime", 0)).strftime("%Y-%m-%d"),
        })

    return {
        "username": username,
        "video_count": len(videos),
        "avg_duration": sum(v["duration"] for v in videos) / len(videos) if videos else 0,
        "avg_plays": sum(v["plays"] for v in videos) / len(videos) if videos else 0,
        "avg_likes": sum(v["likes"] for v in videos) / len(videos) if videos else 0,
        "videos": videos,
    }


def get_trending_by_hashtag(hashtag: str, max_results: int = 30) -> list:
    """ハッシュタグのトレンド動画を取得"""
    run = client.actor("clockworks/tiktok-hashtag-scraper").call(run_input={
        "hashtags": [hashtag],
        "resultsPerPage": max_results,
    })
    items = list(client.dataset(run["defaultDatasetId"]).iterate_items())

    return [{
        "id": v.get("id"),
        "author": v.get("authorMeta", {}).get("name", ""),
        "desc": v.get("desc", ""),
        "duration": v.get("videoMeta", {}).get("duration", 0),
        "plays": v.get("playCount", 0),
        "likes": v.get("diggCount", 0),
        "shares": v.get("shareCount", 0),
    } for v in items]


def save_report(data: dict, filename: str):
    """分析結果をJSONで保存"""
    path = os.path.join(os.path.dirname(__file__), "reports", filename)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"保存: {path}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("使い方: python tiktok_analyzer.py account <username>")
        print("       python tiktok_analyzer.py trend <hashtag>")
        sys.exit(1)

    mode, target = sys.argv[1], sys.argv[2]
    if mode == "account":
        result = analyze_account(target)
        save_report(result, f"{target}_{datetime.now().strftime('%Y%m%d')}.json")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif mode == "trend":
        result = get_trending_by_hashtag(target)
        save_report({"hashtag": target, "videos": result}, f"trend_{target}_{datetime.now().strftime('%Y%m%d')}.json")
        print(json.dumps(result, ensure_ascii=False, indent=2))
