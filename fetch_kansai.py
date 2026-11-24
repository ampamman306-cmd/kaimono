"""関西スーパー セルバ店のチラシをトクバイから取得し、Gemini で読み取って data/deals.json に追加する。

- 取得・読み取りの共通処理（Gemini 呼び出し、画像分割、正規化、保存）は fetch_life.py を再利用する。
- fetch_life.py の main() の最後から run() が呼ばれ、同じ自動実行の中で続けて処理する。
  単独でも実行できる: GEMINI_API_KEY=xxx python fetch_kansai.py
- トクバイの店舗ページ（ID 8111）→ チラシ詳細ページに、その店舗の全チラシ画像が
  「YYYY年M月D日〜M月D日までのチラシ」という alt 付きで並んでいるので、期間でグループ化して1チラシとする。
- 対象は「期間が10日以内で、まだ終わっていない」チラシだけ（週間チラシ）。
  クリスマスケーキ予約・フードドライブ等の長期告知は期間が長いため自動で外れる。
"""
import hashlib, json, os, re, sys, time, urllib.parse, urllib.request
from datetime import date, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

STORE_ID = "kansai_super_selva"
STORE_NAME = "関西スーパー セルバ店"
STORE_PAGE = "https://tokubai.co.jp/" + urllib.parse.quote("関西スーパー") + "/8111"
UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
MAX_PERIOD_DAYS = 10
LONG_SIDE = 2600   # 品数が多いチラシなので、ライフより大きめに読む（views() が左右分割もする）


def http_get_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "ignore")


def parse_period(alt):
    """alt='2026年11月23日〜29日までのチラシ' → (date, date)。年またぎにも対応。"""
    m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日〜(?:(\d{4})年)?(?:(\d{1,2})月)?(\d{1,2})日まで", alt or "")
    if not m:
        return None
    y1, mo1, d1 = int(m.group(1)), int(m.group(2)), int(m.group(3))
    y2 = int(m.group(4)) if m.group(4) else y1
    mo2 = int(m.group(5)) if m.group(5) else mo1
    d2 = int(m.group(6))
    try:
        start = date(y1, mo1, d1)
        end = date(y2, mo2, d2)
    except ValueError:
        return None
    if end < start:
        end = date(y2 + 1, mo2, d2)
    return start, end


def list_flyers(L):
    """トクバイから、この店舗の週間チラシ（期間でグループ化した画像の集まり）を返す。"""
    html = http_get_text(STORE_PAGE)
    ids = list(dict.fromkeys(re.findall(r'href="/[^"]*?/leaflets/(\d+)"', html)))
    if not ids:
        raise RuntimeError("トクバイの店舗ページからチラシが見つかりませんでした")
    # チラシ詳細ページごとに埋め込まれている画像の範囲が違うため、全ページを見て集める
    page = ""
    for lid in ids:
        try:
            page += http_get_text(STORE_PAGE + "/leaflets/" + lid)
        except Exception as e:
            L.log(f"  チラシ詳細ページ {lid} を取得できません: {str(e)[:120]}")
    groups = {}
    for m in re.finditer(r'<img class="leaflet_image[^"]*" alt="([^"]*)"[^>]*data-src="(https://image\.tokubai\.co\.jp/images/bargain_office_leaflets/o=true/(\d+)\.jpg)[^"]*"', page):
        per = parse_period(m.group(1))
        if not per:
            continue
        start, end = per
        key = (start.isoformat(), end.isoformat())
        g = groups.setdefault(key, {"store": STORE_ID, "start": key[0], "end": key[1], "images": []})
        if m.group(2) not in g["images"]:
            g["images"].append(m.group(2))
    out = []
    for g in groups.values():
        g["images"].sort()
        g["id"] = "kansai_" + hashlib.md5(("|".join(g["images"])).encode()).hexdigest()[:12]
        g["name"] = f"{g['start'][5:].replace('-', '/')}〜{g['end'][5:].replace('-', '/')} チラシ"
        out.append(g)
    L.log(f"チラシ一覧: {len(out)}件（期間でグループ化、{STORE_NAME}）")
    return out


def read_flyer(L, models, flyer):
    """チラシの全ページを読む。全体＋分割の全ビューを読み、重複は除く（ライフの早期終了はしない）。"""
    prompt = L.PROMPT.format(store=STORE_NAME, name=flyer["name"], start=flyer["start"], end=flyer["end"],
                             year=flyer["start"][:4], cats="/".join(L.CATEGORIES))
    got, seen, failed = [], set(), False
    for url in flyer["images"]:
        try:
            img = L.http_get(url, timeout=120)
        except Exception as e:
            L.log(f"  チラシ画像を取得できません: {str(e)[:150]}")
            failed = True
            continue
        for label, jpeg in L.views(img, LONG_SIDE):
            if L.STATE["failed"] >= L.MAX_FAILED:
                L.log("  読み取りの失敗が続くため、残りは次回に持ち越します")
                return got, True
            items = L.extract_items(models, jpeg, prompt)
            if label == "全体" and items is not None and len(items) >= 25:
                # 全体で十分に読めたページは分割読みを省く（API 呼び出し節約）
                for it in items:
                    d = L.normalize(it, flyer)
                    if d["price"] is None and d["price_tax"] is None and not d["condition"]:
                        continue
                    if d["id"] in seen:
                        continue
                    seen.add(d["id"])
                    got.append(d)
                L.log(f"  全体: {len(items)}品（累計 {len(got)}品、分割読みは省略）")
                break
            if items is None:
                failed = True
                L.STATE["failed"] += 1
                continue
            for it in items:
                d = L.normalize(it, flyer)
                if d["price"] is None and d["price_tax"] is None and not d["condition"]:
                    continue
                if d["id"] in seen:
                    continue
                seen.add(d["id"])
                got.append(d)
            L.log(f"  {label}: {len(items)}品（累計 {len(got)}品）")
            time.sleep(L.WAIT_BETWEEN)
    return got, failed


def run(L=None):
    if L is None:
        import fetch_life as L
    t = L.today().isoformat()
    deals = L.load("deals.json", {"flyers": [], "items": []})
    processed = L.load("processed.json", {})
    history = L.load("history.json", {})

    flyers = list_flyers(L)
    targets = []
    for f in flyers:
        span = (date.fromisoformat(f["end"]) - date.fromisoformat(f["start"])).days
        if f["end"] >= t and span <= MAX_PERIOD_DAYS and f["id"] not in processed:
            targets.append(f)
    L.log(f"今回読み取るチラシ: {len(targets)}件（{STORE_NAME}）")
    if not targets:
        return

    models = L.list_models()
    items = [i for i in deals.get("items", []) if i["date_to"] >= t]
    done = 0
    for f in targets:
        L.log(f"読み取り: {f['name']} ({f['start']}〜{f['end']})")
        got, failed = read_flyer(L, models, f)
        have = {i["id"] for i in items}
        items += [g for g in got if g["id"] not in have]
        if got:
            processed[f["id"]] = {"name": f["name"], "at": L.datetime.now(L.JST).isoformat(), "count": len(got)}
            done += 1
        elif not failed:
            processed[f["id"]] = {"name": f["name"], "at": L.datetime.now(L.JST).isoformat(), "count": 0, "no_products": True}
            L.log(f"  {f['name']}: 商品がなかったため、次回からは読み取り対象外にします")
        else:
            L.log(f"  {f['name']}: 読み取りが不完全のため、次回に持ち越します")
        if L.STATE["failed"] >= L.MAX_FAILED or L.STATE["calls"] >= L.MAX_CALLS:
            L.log("混雑が続くため、残りのチラシは次回に持ち越します")
            break
        time.sleep(L.WAIT_BETWEEN)

    for i in items:
        if i["store"] != STORE_ID:
            continue
        p = i["price"] if i["price"] is not None else i["price_tax"]
        if p is None:
            continue
        rec = {"d": i["date_from"], "p": p, "q": i["quantity"], "s": i["store"]}
        h = history.setdefault(i["item_key"], [])
        if rec not in h:
            h.append(rec)

    others = [f for f in deals.get("flyers", []) if f.get("store") != STORE_ID]
    active = [f for f in flyers if f["end"] >= t
              and (date.fromisoformat(f["end"]) - date.fromisoformat(f["start"])).days <= MAX_PERIOD_DAYS]
    mine = [{"id": f["id"], "store": STORE_ID, "name": f["name"], "start": f["start"], "end": f["end"],
             "image": f["images"][0] if f["images"] else ""} for f in active]
    L.save("deals.json", {"generated_at": L.datetime.now(L.JST).isoformat(timespec="seconds"),
                          "source": "LINEチラシ（ライフ阪神芦屋店）＋トクバイ（関西スーパー セルバ店）",
                          "flyers": others + mine,
                          "items": sorted(items, key=lambda x: (x["date_from"], x["category"]))})
    L.save("processed.json", processed)
    L.save("history.json", history)
    L.log(f"完了: {STORE_NAME} のチラシ {len(mine)}件を反映（今回読み取り {done}件）")


if __name__ == "__main__":
    run()
