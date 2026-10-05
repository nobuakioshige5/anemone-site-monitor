"""登録ボタン(CTA)が実際に押せるかの監視。

2026-10-05 に anemone.blue トップ(スマホのみ)で登録ボタンが押せなくなり、
WP Rocket のキャッシュクリアで直った件の再発検知が目的。HTML は正常なのに
CSS/JS の最適化結果のせいで透明な要素がボタンを覆う、という壊れ方を想定している。

実ブラウザでページを開き、見えている登録ボタンの中心に「何が一番上にあるか」を調べる。
WP Rocket の「JavaScript の遅延実行」は操作があるまでスクリプトを止めるので、
読み込み直後と、スクロール操作でスクリプトを起こした後の2回確認する。

ボタンは押さない(判定だけ)。解析・広告タグへの通信は遮断し、監視アクセスが
GA4 や広告の成果に混ざらないようにする。
"""
import asyncio
import os
import re

import requests
from playwright.async_api import async_playwright

CTA_SITEMAPS = [
    "https://anemone.blue/page-sitemap.xml",
    "https://anemone2.blue/page-sitemap.xml",
]
CTA_EXTRA_URLS = ["https://anemone.blue/", "https://anemone2.blue/"]
# 1月から更新のない旧ページ。デザインが当たっておらずログイン枠が登録ボタンに重なっている(既知)
CTA_SKIP_URLS = {"https://anemone2.blue/%e3%83%95%e3%83%ad%e3%83%b3%e3%83%88%e3%83%9a%e3%83%bc%e3%82%b8/"}

CTA_SELECTOR = 'a[href*="anemone.blue/regist"]'
BLOCKED_HOSTS = re.compile(
    r"googletagmanager\.com|google-analytics\.com|analytics\.google\.com|doubleclick\.net|"
    r"googleadservices\.com|googlesyndication\.com|facebook\.(net|com)|analytics\.ahrefs\.com|"
    r"bance\.jp|im-apps\.net|clarity\.ms|yimg\.jp|yahoo\.co\.jp|line-scdn\.net|tr\.line\.me|"
    r"analytics\.tiktok\.com|ads-twitter\.com|criteo\.(com|net)"
)
SHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screenshots")

# 見えている登録ボタンごとに、中心点の最前面の要素がボタン自身(か子要素)かを調べる。
# 閉じたドロワーメニュー内・画面外・非表示のボタンは対象外。
HIT_TEST_JS = r"""(sel) => {
  const vw = innerWidth, out = [];
  for (const a of document.querySelectorAll(sel)) {
    if (a.closest('[id*="rawer" i], [class*="drawer" i]')) continue;
    let hidden = false;
    for (let e = a; e; e = e.parentElement) {
      const s = getComputedStyle(e);
      if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity) === 0) { hidden = true; break; }
    }
    let r = a.getBoundingClientRect();
    if (hidden || r.width < 2 || r.height < 2) continue;
    a.scrollIntoView({block: 'center', behavior: 'instant'});
    r = a.getBoundingClientRect();
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    if (x < 0 || x > vw || y < 0 || y > innerHeight) continue;
    const top = document.elementFromPoint(x, y);
    if (!top || top === a || a.contains(top)) continue;
    const desc = e => e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') +
      (typeof e.className === 'string' && e.className.trim() ? '.' + e.className.trim().split(/\s+/).slice(0, 2).join('.') : '');
    const chain = [];
    for (let e = top; e && e !== document.body && chain.length < 3; e = e.parentElement) chain.push(desc(e));
    out.push({text: a.innerText.trim().replace(/\s+/g, ' ').slice(0, 20), blocker: chain.join(' < ')});
  }
  return out;
}"""


def cta_urls():
    urls = list(CTA_EXTRA_URLS)
    for sm in CTA_SITEMAPS:
        try:
            r = requests.get(sm, timeout=30)
            urls += re.findall(r"<loc>([^<]+)</loc>", r.text)
        except requests.RequestException:
            pass  # サイトマップ自体の異常は check_sites 側で通知される
    return [u.strip() for u in dict.fromkeys(urls) if u.strip() not in CTA_SKIP_URLS]


async def _check_page(ctx, url, label):
    page = await ctx.new_page()
    try:
        await page.goto(url, wait_until="load", timeout=45000)
        await page.wait_for_timeout(2000)
        before = await page.evaluate(HIT_TEST_JS, CTA_SELECTOR)
        # スクロール操作で WP Rocket の遅延スクリプトを起こしてから再確認
        await page.mouse.wheel(0, 300)
        await page.wait_for_timeout(4000)
        await page.evaluate("window.scrollTo(0, 0)")
        after = await page.evaluate(HIT_TEST_JS, CTA_SELECTOR)
        bad = before + after
        if not bad:
            return None
        os.makedirs(SHOT_DIR, exist_ok=True)
        slug = re.sub(r"[^A-Za-z0-9]+", "_", url)[-60:]
        await page.screenshot(path=os.path.join(SHOT_DIR, f"{label}_{slug}.png"))
        b = bad[0]
        return f"登録ボタン「{b['text']}」が押せない(上に {b['blocker']} が重なっている, {len(bad)}箇所)"
    except Exception as e:
        return f"ボタン確認でエラー ({type(e).__name__})"
    finally:
        await page.close()


async def _check_device(pw, label, urls):
    dev = dict(pw.devices[{"iPhone": "iPhone 13", "Android": "Pixel 7", "PC": "Desktop Chrome"}[label]])
    engine = pw.webkit if label == "iPhone" else pw.chromium
    dev.pop("default_browser_type", None)
    try:
        browser = await engine.launch()
    except Exception as e:
        return {f"ブラウザ起動失敗 | {label}(ボタン)": f"{type(e).__name__}: {str(e)[:80]}"}
    ctx = await browser.new_context(**dev)
    await ctx.route(BLOCKED_HOSTS, lambda route: route.abort())
    results = {}
    try:
        for url in urls:
            err = await _check_page(ctx, url, label)
            if err and not err.startswith("ボタン確認でエラー"):
                err = await _check_page(ctx, url, label)  # 一時的な描画のずれで誤報しないよう再確認
            if err:
                results[f"{url} | {label}(ボタン)"] = err
    finally:
        await browser.close()
    return results


async def _check_all():
    urls = cta_urls()
    async with async_playwright() as pw:
        labels = os.environ.get("CTA_DEVICES", "iPhone,Android,PC").split(",")  # ローカル確認で絞る用
        parts = await asyncio.gather(*[_check_device(pw, label, urls) for label in labels])
    merged = {}
    for p in parts:
        merged.update(p)
    return urls, merged


def check_ctas():
    """(チェックしたURL一覧, {"url | 端末(ボタン)": エラー文}) を返す。"""
    return asyncio.run(_check_all())
