"""anemone.blue / anemone2.blue 全ページ死活監視。

サイトマップから全URLを集め、スマホ(iPhone Safari / Android Chrome)とPC Chromeの
実ブラウザと同じ Accept-Encoding で取得し、壊れたページがあれば Slack に通知する。

2026-09-28 に「スマホ + `Accept-Encoding: gzip, deflate, br, zstd` のときだけ
トップページが0バイト」という壊れたキャッシュが1日半放置された件の再発検知が目的。
ヘッダーを実ブラウザと完全一致させないと再現しないので、PROFILES は変えないこと。

通知ルール: 新たに壊れた / 復旧した ときに通知し、壊れたままなら REMIND_HOURS ごとに再通知。
状態は STATE_FILE(GitHub Actions では actions/cache で世代間引き継ぎ)に保存する。

使い方:
  python check_sites.py            # 実行して必要なら Slack 通知
  python check_sites.py --dry-run  # Slack に送らず結果を表示
環境変数: SLACK_WEBHOOK_URL
"""
import argparse
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

SITEMAPS = [
    "https://anemone.blue/sitemap_index.xml",
    "https://anemone2.blue/sitemap.xml",
]
# サイトマップに載らない / 載り漏れても必ず見たいURL
EXTRA_URLS = ["https://anemone.blue/", "https://anemone2.blue/"]

PROFILES = {
    "iPhone": {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
        "Accept-Encoding": "gzip, deflate, br",
    },
    "Android": {
        "User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "sec-ch-ua-mobile": "?1",
    },
    "PC": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "sec-ch-ua-mobile": "?0",
    },
}
COMMON_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
}

MIN_BYTES = 5000  # 展開後のHTMLがこれ未満なら壊れているとみなす(通常は4万バイト以上)
TIMEOUT = 30
WORKERS = 6
RETRY_WAIT = 20
REMIND_HOURS = 6
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "monitor_state.json")
JST = timezone(timedelta(hours=9))


def collect_urls():
    urls, seen_maps = [], set()

    def walk(sm_url):
        if sm_url in seen_maps:
            return
        seen_maps.add(sm_url)
        r = requests.get(sm_url, timeout=TIMEOUT, headers={"User-Agent": PROFILES["PC"]["User-Agent"]})
        r.raise_for_status()
        root = ET.fromstring(r.content)
        ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
        for sm in root.iter(f"{ns}sitemap"):
            walk(sm.find(f"{ns}loc").text.strip())
        for u in root.iter(f"{ns}url"):
            urls.append(u.find(f"{ns}loc").text.strip())

    errors = []
    for sm in SITEMAPS:
        try:
            walk(sm)
        except Exception as e:  # サイトマップ自体が壊れているのも異常として報告する
            errors.append(f"サイトマップ取得失敗: {sm} ({type(e).__name__}: {e})")
    # 非HTMLと、プラグインが出す sitemap.html(本文が無い補助ページ)は除外
    urls = [u for u in dict.fromkeys(EXTRA_URLS + urls)
            if not re.search(r"(\.(xml|txt|pdf|jpe?g|png|gif|webp)|/sitemap\.html)$", u)]
    return urls, errors


def check_once(url, profile):
    headers = {**COMMON_HEADERS, **PROFILES[profile]}
    try:
        r = requests.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException as e:
        return f"接続エラー ({type(e).__name__})"
    ctype = r.headers.get("Content-Type", "")
    if r.status_code != 200:
        return f"HTTP {r.status_code}"
    body = r.content  # requests/urllib3 が gzip/br/zstd を展開済み
    if "text/html" not in ctype:
        return f"HTMLではない (Content-Type: {ctype or 'なし'}, {len(body)}バイト)"
    if len(body) < MIN_BYTES:
        return f"中身が小さすぎる ({len(body)}バイト)"
    if b"</html>" not in body[-5000:].lower():
        return f"HTMLが途中で切れている ({len(body)}バイト)"
    return None


def check(url, profile):
    err = check_once(url, profile)
    if err:  # 一時的な瞬断で誤報しないよう1回だけ再試行
        time.sleep(RETRY_WAIT)
        err = check_once(url, profile)
    return url, profile, err


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"failing": {}, "last_notified": None}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def post_slack(text, dry_run):
    if dry_run:
        print("----- Slack (dry-run) -----\n" + text)
        return
    # PowerShell 経由で Secret を登録すると先頭に BOM が付き、requests が URL と認識できなくなる
    webhook = os.environ.get("SLACK_WEBHOOK_URL", "").strip().lstrip("﻿")
    if not webhook:
        sys.exit("SLACK_WEBHOOK_URL が未設定")
    r = requests.post(webhook, json={"text": text}, timeout=TIMEOUT)
    r.raise_for_status()


def fmt_failures(items):
    grouped = {}
    for key, err in items:
        url, profile = key.rsplit(" | ", 1)
        grouped.setdefault((url, err), []).append(profile)
    lines = [f"• <{url}|{url}> [{'/'.join(ps)}] {err}" for (url, err), ps in sorted(grouped.items())]
    if len(lines) > 30:
        lines = lines[:30] + [f"…ほか{len(lines) - 30}件"]
    return "\n".join(lines)


def run_once(dry_run):
    """1回分のチェックと通知。Slack 送信に失敗したら False を返す。"""
    now = datetime.now(JST)
    urls, sitemap_errors = collect_urls()
    jobs = [(u, p) for u in urls for p in PROFILES]
    with ThreadPoolExecutor(WORKERS) as ex:
        results = list(ex.map(lambda a: check(*a), jobs))

    failing_now = {f"{u} | {p}": e for u, p, e in results if e}
    for e in sitemap_errors:
        failing_now[f"{e} | -"] = "サイトマップ"
    print(f"{now:%Y-%m-%d %H:%M} JST: {len(urls)}ページ x {len(PROFILES)}環境 = {len(jobs)}件チェック, 異常 {len(failing_now)}件")
    for k, v in failing_now.items():
        print(f"  NG {k}: {v}")

    state = load_state()
    prev = state.get("failing", {})
    new = {k: v for k, v in failing_now.items() if k not in prev}
    recovered = [k for k in prev if k not in failing_now]
    last = state.get("last_notified")
    remind_due = failing_now and (not last or now - datetime.fromisoformat(last) >= timedelta(hours=REMIND_HOURS))

    msgs = []
    if new:
        msgs.append(f":rotating_light: *サイト異常を検知* ({now:%m/%d %H:%M})\n{fmt_failures(new.items())}")
    elif remind_due:
        msgs.append(f":warning: *サイト異常が継続中* ({len(failing_now)}件, {now:%m/%d %H:%M})\n{fmt_failures(failing_now.items())}")
    if recovered:
        msgs.append(f":white_check_mark: *復旧しました* ({len(recovered)}件)\n"
                    + "\n".join(f"• {k.rsplit(' | ', 1)[0]} [{k.rsplit(' | ', 1)[1]}]" for k in sorted(recovered)[:30]))
    if msgs:
        if new or remind_due:
            msgs.append("キャッシュが原因なら、WP Rocket「キャッシュをクリア」で直ることが多いです。")
        try:
            post_slack("\n\n".join(msgs), dry_run)
        except Exception as e:  # 送れなかった異常は次回「新規」として再送されるよう状態を更新しない
            print(f"Slack送信失敗: {type(e).__name__}: {e}")
            return False
        if new or remind_due:
            state["last_notified"] = now.isoformat()
    if not failing_now:
        state["last_notified"] = None
    state["failing"] = failing_now
    state["last_run"] = now.isoformat()
    state["checked"] = len(jobs)
    save_state(state)
    return True


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    # GitHub の定期実行は数時間に1回まで間引かれることがあるため、1回の実行の中で
    # interval 分ごとにチェックを繰り返し、合計 loop 分で終える(0なら1回だけ)
    ap.add_argument("--loop-minutes", type=float, default=0)
    ap.add_argument("--interval-minutes", type=float, default=60)
    args = ap.parse_args()

    started = time.monotonic()
    ok = True
    while True:
        t0 = time.monotonic()
        ok = run_once(args.dry_run) and ok
        next_at = t0 + args.interval_minutes * 60
        if next_at + 10 * 60 > started + args.loop_minutes * 60:  # 次の1回を終える余裕がなければ終了
            break
        time.sleep(max(0, next_at - time.monotonic()))
    if not ok:
        sys.exit("Slack送信に失敗した回があります(GitHubの失敗メールが代わりの通知になります)")


if __name__ == "__main__":
    main()
