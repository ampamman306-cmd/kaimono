"""ライフ阪神芦屋店のチラシを取得し、Gemini で商品・価格・適用日を読み取って data/deals.json を更新する。

実行: GEMINI_API_KEY=xxx python fetch_life.py
- 1 日 1 回の実行を想定（GitHub Actions の schedule）。
- 読み取り済みのチラシは data/processed.json に記録し、再読み取りしない（API 呼び出し節約）。
- 終了したセールは deals.json から自動で消え、価格は data/history.json に蓄積される。
"""
import base64, hashlib, io, json, math, os, re, sys, time, urllib.request
from datetime import datetime, timedelta, timezone

from PIL import Image

JST = timezone(timedelta(hours=9))
_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE) if os.path.basename(_HERE) == "scripts" else _HERE
DATA = os.path.join(ROOT, "data")
STORE_ID = "life_hanshin_ashiya"
STORE_NAME = "ライフ阪神芦屋店"
BRANCH_URL = "https://chirashi.line.me/branches/65962d763b54907006de0ad7"
MODEL = os.environ.get("GEMINI_MODEL") or "gemini-flash-latest"
API_KEY = os.environ.get("GEMINI_API_KEY", "")
UA = "Mozilla/5.0 (kaimono-family-bot; personal use, 1 request/day)"
TILE = 1800  # この辺の長さを超える画像は分割して読ませる（小さい文字の読み落とし防止）

CATEGORIES = ["野菜", "果物", "肉", "魚", "惣菜", "日配", "加工食品", "菓子", "飲料", "酒", "米", "パン", "日用品", "その他"]
CAT_MAP = {"精肉": "肉", "精肉加工品": "肉", "鮮魚": "魚", "乳製品": "日配", "チーズ": "日配", "デザート": "日配",
           "ベーカリー・パン": "パン", "日用消耗品": "日用品", "健康・ヘルスケア": "日用品", "洋菓子": "菓子",
           "ノンアルコール飲料": "飲料", "冷凍食品": "加工食品", "レトルト": "加工食品", "調味料": "加工食品",
           "麺類": "加工食品", "粉類": "加工食品", "漬物": "日配"}


def today():
    return datetime.now(JST).date()


def load(name, default):
    p = os.path.join(DATA, name)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return default


def save(name, obj):
    with open(os.path.join(DATA, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def to_jst_date(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(JST).date().isoformat()


def list_flyers():
    html = http_get(BRANCH_URL).decode("utf-8")
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    st = json.loads(m.group(1))["props"]["pageProps"]["initialApolloState"]
    # ページには近隣の別店舗のチラシも含まれるため、この店舗の Branch に紐づく promotion だけに絞る
    branch_id = BRANCH_URL.rstrip("/").split("/")[-1]
    own = set()
    for k, v in st.items():
        if k.startswith(f"Branch:{branch_id}"):
            own |= {p["__ref"].split(":", 1)[1] for p in v.get("promotions", []) or []}
    out = []
    for k, v in st.items():
        if not k.startswith("Flyer:") or v.get("deleteTime") or v.get("promotionId") not in own:
            continue
        pr = st.get("Promotion:" + v["promotionId"], {})
        if not pr.get("promotionSalesStartDate"):
            continue
        out.append({
            "id": v["id"], "store": STORE_ID, "name": v["flyerName"],
            "start": to_jst_date(pr["promotionSalesStartDate"]),
            "end": to_jst_date(pr["promotionSalesEndDate"]),
            "image": v["flyerWebpImage"],
        })
    return out


PROMPT = """スーパー「{store}」のチラシ画像の一部です（チラシ名「{name}」、チラシ全体の販売期間 {start}〜{end}）。
価格が書かれた商品をすべて抽出してください。画像の端で切れて価格が読めない商品は除外。
- date_from/date_to: 商品ごとのアイコン・帯・見出し（例「17日(土)限り」「17日(土)・18日(日)限り」「19日(月)のみ」）から判断。個別表記がなければチラシ全体の期間。年は{year}年。
- price: 本体価格（税抜）。税込しか書かれていなければ price_tax に入れ price は null。
- 割引率だけの商品（例: 表示価格より2割引）は price=null, price_tax=null とし condition に内容。
- item_key: 比較用の一般名（例: 卵, 牛乳, 豚こま切落し, 鶏もも, みかん, 食パン, ティッシュ, 米）。
- category: {cats} のいずれか。"""

SCHEMA = {
    "type": "OBJECT",
    "properties": {"items": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "name": {"type": "STRING"}, "item_key": {"type": "STRING"}, "category": {"type": "STRING"},
        "price": {"type": "NUMBER", "nullable": True}, "price_tax": {"type": "NUMBER", "nullable": True},
        "quantity": {"type": "STRING"}, "date_from": {"type": "STRING"}, "date_to": {"type": "STRING"},
        "condition": {"type": "STRING", "nullable": True}},
        "required": ["name", "item_key", "category", "date_from", "date_to"]}}},
    "required": ["items"],
}


def gemini_extract(jpeg_bytes, prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    body = {
        "contents": [{"role": "user", "parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(jpeg_bytes).decode()}}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": SCHEMA, "temperature": 0},
    }
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "x-goog-api-key": API_KEY})
            with urllib.request.urlopen(req, timeout=180) as r:
                res = json.load(r)
            text = "".join(p.get("text", "") for p in res["candidates"][0]["content"]["parts"])
            return json.loads(text[text.find("{"): text.rfind("}") + 1]).get("items", [])
        except Exception as e:  # 429 などは待って再試行
            print(f"  retry {attempt + 1}: {e}", file=sys.stderr)
            time.sleep(15 * (attempt + 1))
    return []


def tiles(img_bytes):
    im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    w, h = im.size
    cols, rows, ov = math.ceil(w / TILE), math.ceil(h / TILE), 100
    for r in range(rows):
        for c in range(cols):
            box = (max(0, c * w // cols - ov), max(0, r * h // rows - ov),
                   min(w, (c + 1) * w // cols + ov), min(h, (r + 1) * h // rows + ov))
            buf = io.BytesIO()
            im.crop(box).save(buf, "JPEG", quality=88)
            yield buf.getvalue()


def num(x):
    try:
        return None if x in (None, "", "null") else float(str(x).replace(",", "").replace("円", ""))
    except ValueError:
        return None


def normalize(it, flyer):
    cat = CAT_MAP.get(it.get("category"), it.get("category"))
    if cat not in CATEGORIES:
        cat = "その他"
    d = {
        "store": flyer["store"], "flyer_id": flyer["id"], "name": it.get("name", "").strip(),
        "item_key": (it.get("item_key") or it.get("name", "")).strip(), "category": cat,
        "price": num(it.get("price")), "price_tax": num(it.get("price_tax")),
        "quantity": it.get("quantity") or "", "date_from": it.get("date_from") or flyer["start"],
        "date_to": it.get("date_to") or flyer["end"], "condition": it.get("condition") or None,
    }
    if not re.match(r"\d{4}-\d\d-\d\d$", d["date_from"]): d["date_from"] = flyer["start"]
    if not re.match(r"\d{4}-\d\d-\d\d$", d["date_to"]): d["date_to"] = flyer["end"]
    key = (d["item_key"], d["name"], d["price"], d["date_from"], d["date_to"], d["flyer_id"])
    d["id"] = hashlib.md5(json.dumps(key, ensure_ascii=False).encode()).hexdigest()[:10]
    return d


def main():
    if not API_KEY:
        sys.exit("GEMINI_API_KEY が設定されていません")
    os.makedirs(DATA, exist_ok=True)
    deals = load("deals.json", {"flyers": [], "items": []})
    processed = load("processed.json", {})
    history = load("history.json", {})
    t = today().isoformat()

    flyers = list_flyers()
    print(f"{len(flyers)} 件のチラシを確認")
    items = [i for i in deals.get("items", []) if i["date_to"] >= t]
    known = {f["id"] for f in deals.get("flyers", [])}

    for f in flyers:
        if f["end"] < t or f["id"] in processed:
            continue
        print(f"読み取り: {f['name']} ({f['start']}〜{f['end']})")
        prompt = PROMPT.format(store=STORE_NAME, name=f["name"], start=f["start"], end=f["end"],
                               year=f["start"][:4], cats="/".join(CATEGORIES))
        got = []
        for tile in tiles(http_get(f["image"])):
            got += [normalize(x, f) for x in gemini_extract(tile, prompt)]
            time.sleep(4)  # 無料枠のレート制限対策
        got = [g for g in got if g["price"] is not None or g["price_tax"] is not None or g["condition"]]
        seen = {i["id"] for i in items}
        items += [g for g in got if g["id"] not in seen and not seen.add(g["id"])]
        processed[f["id"]] = {"name": f["name"], "at": datetime.now(JST).isoformat(), "count": len(got)}
        print(f"  {len(got)} 品")

    # 価格履歴（普段の価格の推定に使う）
    for i in items:
        p = i["price"] or i["price_tax"]
        if p is None:
            continue
        rec = {"d": i["date_from"], "p": p, "q": i["quantity"], "s": i["store"]}
        h = history.setdefault(i["item_key"], [])
        if rec not in h:
            h.append(rec)

    active = {f["id"] for f in flyers if f["end"] >= t}
    deals = {
        "generated_at": datetime.now(JST).isoformat(timespec="seconds"),
        "source": "LINEチラシ（ライフ阪神芦屋店）",
        "flyers": [f for f in flyers if f["id"] in active],
        "items": sorted(items, key=lambda x: (x["date_from"], x["category"])),
    }
    save("deals.json", deals)
    save("processed.json", {k: v for k, v in processed.items() if k in active or k in known})
    save("history.json", history)
    print(f"完了: {len(deals['items'])} 品を保存")


if __name__ == "__main__":
    main()
