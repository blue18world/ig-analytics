"""
Threads Analytics Fetcher
用 Threads Graph API 抓取帳號成效數據，寫入 Google Sheets
"""
import os
import time
import requests
from datetime import datetime, timezone, timedelta

TW_TZ = timezone(timedelta(hours=8))
THREADS_BASE = "https://graph.threads.net/v1.0"


def get_env(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(f"缺少環境變數: {key}")
    return val


# ── Threads API ───────────────────────────────────────

def get_threads_user_id(access_token: str) -> str:
    """取得 Threads User ID"""
    # 優先使用環境變數直接指定
    user_id = os.getenv("THREADS_USER_ID")
    if user_id:
        print(f"  使用指定的 Threads User ID: {user_id}")
        return user_id

    url = f"{THREADS_BASE}/me"
    resp = requests.get(url, params={
        "fields": "id,username",
        "access_token": access_token
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    print(f"  Threads 帳號: @{data.get('username')} (ID: {data.get('id')})")
    return data["id"]


def fetch_account_insights(user_id: str, access_token: str) -> dict:
    """
    抓取帳號整體成效數據
    Threads API 支援的 metrics:
    - views: 內容被看到的總次數
    - likes: 總按讚數
    - replies: 總回覆數
    - reposts: 總轉發數
    - quotes: 總引用數
    - followers_count: 粉絲數
    - follower_demographics: 粉絲人口統計（需要特殊權限）
    """
    url = f"{THREADS_BASE}/{user_id}/threads_insights"
    result = {}

    # 第一組：基本互動數據（since/until 抓過去7天）
    now = datetime.now(TW_TZ)
    since = int((now - timedelta(days=7)).timestamp())
    until = int(now.timestamp())

    metrics_list = [
        ("views", "day"),
        ("likes", "day"),
        ("replies", "day"),
        ("reposts", "day"),
        ("quotes", "day"),
        ("followers_count", "day"),
    ]

    for metric, period in metrics_list:
        resp = requests.get(url, params={
            "metric": metric,
            "period": period,
            "since": since,
            "until": until,
            "access_token": access_token,
        }, timeout=30)

        if resp.status_code == 200:
            data = resp.json().get("data", [])
            if data:
                values = data[0].get("values", [])
                # 加總過去7天的數值
                total = sum(v.get("value", 0) for v in values)
                result[metric] = total
        else:
            print(f"  Warning: {metric} 抓取失敗 ({resp.status_code})")
            result[metric] = "N/A"

        time.sleep(0.2)

    return result


def fetch_recent_posts_summary(user_id: str, access_token: str, limit: int = 10) -> list[dict]:
    """抓取最近貼文的基本資訊（Threads 不開放單篇 insights）"""
    url = f"{THREADS_BASE}/{user_id}/threads"
    resp = requests.get(url, params={
        "fields": "id,text,timestamp,media_type,like_count,replies_count",
        "limit": limit,
        "access_token": access_token,
    }, timeout=30)

    if resp.status_code != 200:
        print(f"  Warning: 貼文列表抓取失敗 ({resp.status_code})")
        return []

    return resp.json().get("data", [])


# ── Google Sheets 寫入 ────────────────────────────────

def get_gspread_client():
    import json
    import gspread
    from google.oauth2.service_account import Credentials

    SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

    creds_json_str = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if creds_json_str:
        try:
            creds_info = json.loads(creds_json_str)
        except json.JSONDecodeError:
            creds_info = json.loads(creds_json_str.replace("\\n", "\n"))
        creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    else:
        creds_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
        creds = Credentials.from_service_account_file(creds_file, scopes=SCOPES)

    return gspread.authorize(creds)


def write_account_insights(account_data: dict, posts: list[dict]):
    """將帳號週報與貼文摘要寫入 Google Sheets"""
    client = get_gspread_client()
    sheet_id = get_env("GOOGLE_SHEET_ID")
    spreadsheet = client.open_by_key(sheet_id)

    now = datetime.now(TW_TZ)
    sheet_name = "Threads Analytics"

    # 找或建立 Threads 專用分頁
    worksheet = None
    for ws in spreadsheet.worksheets():
        if ws.title == sheet_name:
            worksheet = ws
            break

    if worksheet is None:
        worksheet = spreadsheet.add_worksheet(title=sheet_name, rows=500, cols=12)
        # 週報標題
        worksheet.append_row([
            "記錄時間", "週瀏覽數", "週按讚數", "週回覆數",
            "週轉發數", "週引用數", "粉絲數變化"
        ])
        # 貼文標題（空一行後）
        worksheet.append_row([])
        worksheet.append_row([
            "─── 近期貼文摘要 ───", "發文時間", "類型", "內容摘要",
            "按讚數", "回覆數"
        ])
        print(f"  建立新分頁: {sheet_name}")

    # 寫入帳號週報
    fetch_time = now.strftime("%Y/%m/%d %H:%M")
    weekly_row = [
        fetch_time,
        account_data.get("views", "N/A"),
        account_data.get("likes", "N/A"),
        account_data.get("replies", "N/A"),
        account_data.get("reposts", "N/A"),
        account_data.get("quotes", "N/A"),
        account_data.get("followers_count", "N/A"),
    ]
    worksheet.append_row(weekly_row)
    print(f"  寫入帳號週報")

    # 寫入近期貼文摘要
    for post in posts:
        ts = post.get("timestamp", "")
        if ts:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(TW_TZ)
            post_date = dt.strftime("%Y/%m/%d %H:%M")
        else:
            post_date = ""

        text = post.get("text", "") or ""
        preview = text[:30].replace("\n", " ") + ("..." if len(text) > 30 else "")

        post_row = [
            "",
            post_date,
            post.get("media_type", "TEXT"),
            preview,
            post.get("like_count", 0),
            post.get("replies_count", 0),
        ]
        worksheet.append_row(post_row)
        time.sleep(0.3)

    print(f"  寫入 {len(posts)} 篇近期貼文摘要")
    return sheet_name


# ── 主流程 ────────────────────────────────────────────

def main():
    print("🚀 開始抓取 Threads 數據...")
    print(f"時間: {datetime.now(TW_TZ).strftime('%Y/%m/%d %H:%M')}")
    print()

    access_token = get_env("THREADS_ACCESS_TOKEN")

    # 1. 取得 User ID
    print("📋 取得 Threads 帳號資訊...")
    user_id = get_threads_user_id(access_token)

    # 2. 抓取帳號週報
    print("\n📊 抓取帳號週報數據（過去 7 天）...")
    account_data = fetch_account_insights(user_id, access_token)
    print(f"  週瀏覽: {account_data.get('views', 'N/A')}")
    print(f"  週按讚: {account_data.get('likes', 'N/A')}")
    print(f"  週回覆: {account_data.get('replies', 'N/A')}")
    print(f"  粉絲數: {account_data.get('followers_count', 'N/A')}")

    # 3. 抓取近期貼文
    print("\n📝 抓取最近 10 篇貼文...")
    posts = fetch_recent_posts_summary(user_id, access_token, limit=10)
    print(f"  找到 {len(posts)} 篇貼文")

    # 4. 寫入 Google Sheets
    print("\n📤 寫入 Google Sheets...")
    sheet_name = write_account_insights(account_data, posts)

    print(f"\n✅ 完成！數據已寫入分頁: {sheet_name}")


if __name__ == "__main__":
    main()
