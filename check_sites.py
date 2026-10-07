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
from urllib.parse import urlparse

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
WORKERS = 2  # 同時接続が多いとサーバーの自動遮断(DoS判定)に掛かるおそれがあるため控えめに
RETRY_WAIT = 20
REMIND_HOURS = 6
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state", "monitor_state.json")
JST = timezone(timedelta(hours=9))
# GitHub の実行サーバー(IPは実行ごとに変わる)だけが Xserver に接続できなくなることがある
# (10/4, 10/6-7 に発生。同じ実行の間はずっと繋がらず、次の実行で直る)。
# 接続エラーが出たら外部の確認サービスでも見て、外からは正常なら「監視側だけの問題」として分けて通知する。
CHECK_HOST_NODES = ["jp1.node.check-host.net", "hk1.node.check-host.net", "sg1.node.check-host.net"]
BLOCKED_PROFILE = "監視側"


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
            # 詳細文にはオブジェクトのアドレス等が入り毎回変わるため、通知の照合キーには例外名だけを使う
            errors.append(f"サイトマップ取得失敗: {sm} ({type(e).__name__})")
            print(f"  サイトマップ取得失敗の詳細: {sm}: {e}")
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


def is_connect_error(err):
    return err.startswith("接続エラー") or "(ConnectTimeout)" in err or "(ConnectionError)" in err


def reachable_from_outside(url):
    """check-host.net の東京/香港/シンガポール拠点から取得し、どこかで200なら True。
    確認サービス自体が使えないときは None(判断できないので通常の異常として扱う)。"""
    try:
        h = {"Accept": "application/json"}
        r = requests.get("https://check-host.net/check-http", headers=h, timeout=TIMEOUT,
                         params=[("host", url)] + [("node", n) for n in CHECK_HOST_NODES])
        r.raise_for_status()
        rid = r.json()["request_id"]
        for _ in range(6):
            time.sleep(5)
            res = requests.get(f"https://check-host.net/check-result/{rid}", headers=h, timeout=TIMEOUT).json()
            done = [v for v in res.values() if v is not None]
            # 結果は [[成否, 秒, "OK", "200", IP]] の形
            if any(v and v[0] and v[0][0] == 1 and str(v[0][3]) == "200" for v in done):
                return True
            if len(done) == len(res):
                return False
        return False
    except Exception as e:
        print(f"  外部確認(check-host.net)失敗: {type(e).__name__}: {e}")
        return None


def split_runner_blocked(failing_now):
    """外部からは正常に見えるホストへの接続エラー(とその巻き添えのボタン確認エラー)を、
    ホストごとに1件の「監視側」項目へまとめ直す。"""
    hosts = set()
    for key, err in failing_now.items():
        if is_connect_error(key if err == "サイトマップ" else err):
            hosts.add(urlparse(re.search(r"https://\S+", key).group(0)).hostname)
    blocked = set()
    for host in sorted(hosts):
        ok = reachable_from_outside(f"https://{host}/")
        print(f"  外部からの確認 {host}: {ok}")
        if ok:
            blocked.add(host)
    if not blocked:
        return failing_now
    out = {}
    for key, err in failing_now.items():
        m = re.search(r"https://\S+", key)
        host = urlparse(m.group(0)).hostname if m else None
        if host in blocked and (is_connect_error(key if err == "サイトマップ" else err) or "ボタン確認でエラー" in err):
            continue
        if key.startswith("監視ツールのエラー"):  # ボタン確認の全滅も同じ原因
            continue
        out[key] = err
    for host in sorted(blocked):
        out[f"監視サーバーから {host} に接続できない | {BLOCKED_PROFILE}"] = "外部(東京など)からは正常に表示できています"
    return out


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


def run_once(dry_run, cta=True):
    """1回分のチェックと通知。Slack 送信に失敗したら False を返す。"""
    now = datetime.now(JST)
    urls, sitemap_errors = collect_urls()
    jobs = [(u, p) for u in urls for p in PROFILES]
    with ThreadPoolExecutor(WORKERS) as ex:
        results = list(ex.map(lambda a: check(*a), jobs))

    failing_now = {f"{u} | {p}": e for u, p, e in results if e}
    for e in sitemap_errors:
        failing_now[f"{e} | -"] = "サイトマップ"
    summary = f"{len(urls)}ページ x {len(PROFILES)}環境 = {len(jobs)}件"
    if cta:
        from cta_check import check_ctas  # playwright が要るので必要なときだけ読み込む
        cta_urls, cta_failures = check_ctas()
        failing_now.update(cta_failures)
        summary += f" + ボタン確認 {len(cta_urls)}ページ x 3環境"
    failing_now = split_runner_blocked(failing_now)
    print(f"{now:%Y-%m-%d %H:%M} JST: {summary}チェック, 異常 {len(failing_now)}件")
    for k, v in failing_now.items():
        print(f"  NG {k}: {v}")

    state = load_state()
    prev = state.get("failing", {})
    new = {k: v for k, v in failing_now.items() if k not in prev}
    recovered = [k for k in prev if k not in failing_now]
    last = state.get("last_notified")
    remind_due = failing_now and (not last or now - datetime.fromisoformat(last) >= timedelta(hours=REMIND_HOURS))

    def site_only(d):
        return {k: v for k, v in d.items() if not k.endswith(f" | {BLOCKED_PROFILE}")}

    msgs = []
    new_site = site_only(new)
    new_blocked = {k: v for k, v in new.items() if k not in new_site}
    if new_site:
        msgs.append(f":rotating_light: *サイト異常を検知* ({now:%m/%d %H:%M})\n{fmt_failures(new_site.items())}")
    elif remind_due and site_only(failing_now):
        msgs.append(f":warning: *サイト異常が継続中* ({len(site_only(failing_now))}件, {now:%m/%d %H:%M})\n"
                    f"{fmt_failures(site_only(failing_now).items())}")
    if new_blocked:  # 監視側だけの問題は初回のみ知らせ、継続中の再通知はしない
        msgs.append(f":large_yellow_circle: *監視サーバーだけがサイトに接続できません(サイトは正常)* ({now:%m/%d %H:%M})\n"
                    + "\n".join(f"• {k.rsplit(' | ', 1)[0]}: {v}" for k, v in sorted(new_blocked.items()))
                    + "\nGitHub の監視サーバーが Xserver 側で弾かれているとみられます。対応は不要です"
                    "(次の監視サーバーに替わると通常は直ります)。この間、該当サイトの監視は止まっています。")
    if recovered:
        msgs.append(f":white_check_mark: *復旧しました* ({len(recovered)}件)\n"
                    + "\n".join(f"• {k.rsplit(' | ', 1)[0]} [{k.rsplit(' | ', 1)[1]}]" for k in sorted(recovered)[:30]))
    alert = new_site or (remind_due and site_only(failing_now))
    if msgs:
        if alert:
            if any("(ボタン)" in k for k in failing_now):
                run = os.environ.get("GITHUB_RUN_ID")
                where = (f"<{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{run}|この実行>"
                         if run else "screenshots/")
                msgs.append(f"ボタンの画面写真は {where} の Artifacts に保存されます(監視の実行が終わった後に見られます)。"
                            "原因調査のため、可能ならキャッシュクリアの前にClaudeに知らせてください。")
            msgs.append("キャッシュが原因なら、WP Rocket「キャッシュをクリア」で直ることが多いです。")
        try:
            post_slack("\n\n".join(msgs), dry_run)
        except Exception as e:  # 送れなかった異常は次回「新規」として再送されるよう状態を更新しない
            print(f"Slack送信失敗: {type(e).__name__}: {e}")
            return False
        if alert:
            state["last_notified"] = now.isoformat()
    if not site_only(failing_now):
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
    ap.add_argument("--no-cta", action="store_true", help="ブラウザでのボタン確認を省く")
    args = ap.parse_args()

    started = time.monotonic()
    ok = True
    while True:
        t0 = time.monotonic()
        ok = run_once(args.dry_run, cta=not args.no_cta) and ok
        next_at = t0 + args.interval_minutes * 60
        if next_at + 10 * 60 > started + args.loop_minutes * 60:  # 次の1回を終える余裕がなければ終了
            break
        time.sleep(max(0, next_at - time.monotonic()))
    if not ok:
        sys.exit("Slack送信に失敗した回があります(GitHubの失敗メールが代わりの通知になります)")


if __name__ == "__main__":
    main()
