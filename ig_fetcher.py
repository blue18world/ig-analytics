"""
Instagram Analytics Fetcher
用 Meta Graph API 抓取 IG 貼文成效，寫入 Google Sheets
"""
import os
import time
import requests
from datetime import datetime, timezone, timedelta

TW_TZ = timezone(timedelta(hours=8))
GRAPH_BASE = "https://graph.facebook.com/v21.0"


def get_env(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(f"缺少環境變數: {key}")
    return val


# ── Meta Graph API ────────────────────────────────────

def get_ig_user_id(access_token: str) -> tuple[str, str]:
    """
    取得 IG Business Account 的 user_id。
    優先使用環境變數 META_IG_USER_ID 直接指定（推薦），
    否則嘗試透過粉絲專頁查詢。
    回傳 (ig_user_id, access_token)
    """
    # 直接指定 IG ID（最穩定的方式）
    ig_user_id = os.getenv("META_IG_USER_ID")
    if ig_user_id:
        print(f"  使用指定的 IG User ID: {ig_user_id}")
        return ig_user_id, access_token

    # 嘗試透過粉絲專頁查詢
    url = f"{GRAPH_BASE}/me/accounts"
    resp = requests.get(url, params={"access_token": access_token}, timeout=30)
    resp.raise_for_status()
    pages = resp.json().get("data", [])

    if not pages:
        raise ValueError(
            "找不到粉絲專頁。請設定環境變數 META_IG_USER_ID=17841466923315453"
        )

    page = pages[0]
    page_token = page["access_token"]
    page_id = page["id"]

    url2 = f"{GRAPH_BASE}/{page_id}"
    resp2 = requests.get(url2, params={
        "fields": "instagram_business_account",
        "access_token": page_token
    }, timeout=30)
    resp2.raise_for_status()
    ig_data = resp2.json().get("instagram_business_account")

    if not ig_data:
        raise ValueError("此粉絲專頁沒有連結 Instagram 商業帳號")

    return ig_data["id"], page_token


def fetch_recent_posts(ig_user_id: str, page_token: str, limit: int = 20) -> list[dict]:
    """抓取最近 N 篇貼文的基本資訊"""
    url = f"{GRAPH_BASE}/{ig_user_id}/media"
    params = {
        "fields": "id,caption,media_type,timestamp,permalink",
        "limit": limit,
        "access_token": page_token,
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("data", [])


def fetch_post_insights(media_id: str, page_token: str) -> dict:
    """抓取單篇貼文的成效數據"""
    url = f"{GRAPH_BASE}/{media_id}/insights"

    # IG 貼文支援的 metrics
    metrics = "impressions,reach,likes,comments,shares,saved,total_interactions"

    params = {
        "metric": metrics,
        "access_token": page_token,
    }
    resp = requests.get(url, params=params, timeout=30)

    if resp.status_code != 200:
        # 部分貼文類型（限時動態等）不支援所有 metrics，回傳空值
        return {}

    data = resp.json().get("data", [])
    result = {}
    for item in data:
        result[item["name"]] = item["values"][0]["value"] if item.get("values") else 0
    return result


def fetch_account_insights(ig_user_id: str, page_token: str) -> dict:
    """抓取帳號整體數據（過去 7 天）"""
    url = f"{GRAPH_BASE}/{ig_user_id}/insights"
    result = {}

    # 第一組：reach、follower_count
    r1 = requests.get(url, params={
        "metric": "reach,follower_count",
        "period": "week",
        "access_token": page_token,
    }, timeout=30)
    if r1.status_code == 200:
        for item in r1.json().get("data", []):
            values = item.get("values", [])
            result[item["name"]] = values[-1]["value"] if values else 0

    # 第二組：profile_views、accounts_engaged（需要 metric_type=total_value）
    r2 = requests.get(url, params={
        "metric": "profile_views,accounts_engaged",
        "period": "week",
        "metric_type": "total_value",
        "access_token": page_token,
    }, timeout=30)
    if r2.status_code == 200:
        for item in r2.json().get("data", []):
            tv = item.get("total_value", {})
            result[item["name"]] = tv.get("value", 0)

    return result


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
            # 處理 GitHub Secrets 換行符號問題
            creds_info = json.loads(creds_json_str.replace("\\n", "\n"))
        creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    else:
        creds_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
        creds = Credentials.from_service_account_file(creds_file, scopes=SCOPES)

    return gspread.authorize(creds)


def write_to_sheets(posts_data: list[dict], account_data: dict):
    """將抓取到的數據寫入 Google Sheets"""
    client = get_gspread_client()
    sheet_id = get_env("GOOGLE_SHEET_ID")
    spreadsheet = client.open_by_key(sheet_id)

    now = datetime.now(TW_TZ)
    sheet_name = f"Analytics {now.strftime('%Y-%m')}"

    # 找或建立本月分頁
    worksheet = None
    for ws in spreadsheet.worksheets():
        if ws.title == sheet_name:
            worksheet = ws
            break

    if worksheet is None:
        worksheet = spreadsheet.add_worksheet(title=sheet_name, rows=200, cols=15)
        # 寫入標題列
        headers = [
            "抓取時間", "發文日期", "貼文類型", "標題摘要",
            "曝光數", "觸及數", "按讚數", "留言數",
            "分享數", "收藏數", "總互動數", "互動率(%)", "連結"
        ]
        worksheet.append_row(headers)
        print(f"  建立新分頁: {sheet_name}")

    # 寫入帳號整體數據（第一行備註）
    if account_data:
        summary = [
            f"帳號週報 {now.strftime('%Y/%m/%d %H:%M')}",
            f"週曝光: {account_data.get('impressions', 'N/A')}",
            f"週觸及: {account_data.get('reach', 'N/A')}",
            f"個人檔案瀏覽: {account_data.get('profile_views', 'N/A')}",
            f"粉絲數: {account_data.get('follower_count', 'N/A')}",
        ]
        worksheet.append_row(summary)

    # 寫入各貼文數據
    fetch_time = now.strftime("%Y/%m/%d %H:%M")
    rows_written = 0

    for post in posts_data:
        reach = post.get("reach", 0) or 0
        total = post.get("total_interactions", 0) or 0
        engagement_rate = round((total / reach * 100), 2) if reach > 0 else 0

        # 標題摘要：取 caption 前 20 字
        caption = post.get("caption", "") or ""
        title_preview = caption[:20].replace("\n", " ") + ("..." if len(caption) > 20 else "")

        # 發文時間轉台灣時區
        ts = post.get("timestamp", "")
        if ts:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(TW_TZ)
            post_date = dt.strftime("%Y/%m/%d %H:%M")
        else:
            post_date = ""

        row = [
            fetch_time,
            post_date,
            post.get("media_type", ""),
            title_preview,
            post.get("impressions", 0),
            reach,
            post.get("likes", 0),
            post.get("comments", 0),
            post.get("shares", 0),
            post.get("saved", 0),
            total,
            engagement_rate,
            post.get("permalink", ""),
        ]
        worksheet.append_row(row)
        rows_written += 1
        time.sleep(0.5)  # 避免 API rate limit

    print(f"  寫入 {rows_written} 筆貼文數據到 {sheet_name}")
    return sheet_name


# ── 主流程 ────────────────────────────────────────────

def main():
    print("🚀 開始抓取 Instagram 數據...")
    print(f"時間: {datetime.now(TW_TZ).strftime('%Y/%m/%d %H:%M')}")
    print()

    access_token = get_env("META_ACCESS_TOKEN")

    # 1. 取得 IG User ID
    print("📋 取得 IG 帳號資訊...")
    ig_user_id, page_token = get_ig_user_id(access_token)
    print(f"  IG User ID: {ig_user_id}")

    # 2. 抓取帳號整體數據
    print("\n📊 抓取帳號週報數據...")
    account_data = fetch_account_insights(ig_user_id, page_token)
    print(f"  週觸及: {account_data.get('reach', 'N/A')}")
    print(f"  粉絲數: {account_data.get('follower_count', 'N/A')}")

    # 3. 抓取最近貼文列表
    print("\n📝 抓取最近 20 篇貼文...")
    posts = fetch_recent_posts(ig_user_id, page_token, limit=20)
    print(f"  找到 {len(posts)} 篇貼文")

    # 4. 逐篇抓取成效數據
    print("\n🔍 抓取各貼文成效...")
    posts_with_insights = []
    for i, post in enumerate(posts):
        print(f"  [{i+1}/{len(posts)}] {post.get('timestamp', '')[:10]}...")
        insights = fetch_post_insights(post["id"], page_token)
        post.update(insights)
        posts_with_insights.append(post)
        time.sleep(0.3)  # 避免 rate limit

    # 5. 寫入 Google Sheets
    print("\n📤 寫入 Google Sheets...")
    sheet_name = write_to_sheets(posts_with_insights, account_data)

    print(f"\n✅ 完成！數據已寫入分頁: {sheet_name}")


if __name__ == "__main__":
    main()
