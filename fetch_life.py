"""ライフ阪神芦屋店のチラシを取得し、Gemini で商品・価格・適用日を読み取って data/deals.json を更新する。

実行: GEMINI_API_KEY=xxx python fetch_life.py
- 1日1回の実行を想定。読み取り済みのチラシは data/processed.json で記録し、再読み取りしない。
- 混雑時（429/503）は待って再試行し、それでもだめなチラシは次回に持ち越す。
- 終了したセールは deals.json から消え、価格は data/history.json にたまる。
"""
import base64, hashlib, io, json, math, os, re, sys, time, urllib.error, urllib.request
from datetime import datetime, timedelta, timezone

from PIL import Image

JST = timezone(timedelta(hours=9))
_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE) if os.path.basename(_HERE) == "scripts" else _HERE
DATA = os.path.join(ROOT, "data")
STORE_ID = "life_hanshin_ashiya"
STORE_NAME = "ライフ阪神芦屋店"
BRANCH_URL = "https://chirashi.line.me/branches/65962d763b54907006de0ad7"
UA = "Mozilla/5.0 (kaimono-family-bot; personal use, <=3 requests/day)"
API = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"

# 使えるモデルを上から順に試す（GEMINI_MODEL が設定されていればそれだけを使う）
MODEL_CANDIDATES = ["gemini-flash-latest", "gemini-3.6-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash"]
API_KEY = os.environ.get("GEMINI_API_KEY", "")
LONG_SIDE = 2000      # 長辺をこの長さに縮小して読ませる
SPLIT_OVER = 2200     # 縮小後の長辺がこれを超える場合のみ分割
WAIT_BACKOFF = [15, 30, 60]   # 429/503 のときの待ち時間（秒）
MAX_FAILED = 3                # 読み取りに失敗した画像が続いたら、その回の実行を打ち切る
COUNTER = {"failed": 0}
WAIT_BETWEEN = 8      # リクエスト間の待ち時間（無料枠のレート制限対策）

CATEGORIES = ["野菜", "果物", "肉", "魚", "惣菜", "日配", "加工食品", "菓子", "飲料", "酒", "米", "パン", "日用品", "その他"]
CAT_MAP = {"精肉": "肉", "精肉加工品": "肉", "鮮魚": "魚", "乳製品": "日配", "チーズ": "日配", "デザート": "日配",
           "ベーカリー・パン": "パン", "日用消耗品": "日用品", "健康・ヘルスケア": "日用品", "洋菓子": "菓子",
           "ノンアルコール飲料": "飲料", "冷凍食品": "加工食品", "レトルト": "加工食品", "調味料": "加工食品",
           "麺類": "加工食品", "粉類": "加工食品", "漬物": "日配"}


def log(*a):
    print(*a, flush=True)


def today():
    return datetime.now(JST).date()


def load(name, default):
    p = os.path.join(DATA, name)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return default


def save(name, obj):
    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def to_jst_date(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(JST).date().isoformat()


def list_flyers():
    """この店舗のチラシ一覧（販売期間つき）。近隣の別店舗のチラシは除外する。"""
    html = http_get(BRANCH_URL).decode("utf-8")
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError("チラシ一覧を取得できませんでした（ページの作りが変わった可能性があります）")
    st = json.loads(m.group(1))["props"]["pageProps"]["initialApolloState"]
    branch_id = BRANCH_URL.rstrip("/").split("/")[-1]
    own = set()
    for k, v in st.items():
        if k.startswith("Branch:" + branch_id):
            own |= {p["__ref"].split(":", 1)[1] for p in (v.get("promotions") or [])}
    out = []
    for k, v in st.items():
        if not k.startswith("Flyer:") or v.get("deleteTime") or v.get("promotionId") not in own:
            continue
        pr = st.get("Promotion:" + v["promotionId"], {})
        if not pr.get("promotionSalesStartDate"):
            continue
        out.append({"id": v["id"], "store": STORE_ID, "name": v["flyerName"],
                    "start": to_jst_date(pr["promotionSalesStartDate"]),
                    "end": to_jst_date(pr["promotionSalesEndDate"]),
                    "image": v["flyerWebpImage"]})
    return out


PROMPT = """スーパー「{store}」のチラシ画像です（チラシ名「{name}」、チラシ全体の販売期間 {start}〜{end}）。
価格が書かれた商品をすべて抽出し、JSONのみで出力してください。切れて価格が読めない商品は除外。
{{"items":[{{"name":"商品名(ブランド・産地含む)","item_key":"比較用の一般名(例:卵,牛乳,豚こま切落し,鶏もも,みかん,食パン,ティッシュ,米)","category":"{cats} のいずれか","price":本体価格(税抜)の数値かnull,"price_tax":税込価格の数値かnull,"quantity":"内容量・単位(例:10個入1パック,100g当り)","date_from":"YYYY-MM-DD","date_to":"YYYY-MM-DD","condition":"会員限定・お一人様〇点限り・時間限定など、なければnull"}}]}}
date_from/date_to は商品ごとの「〇日限り」「〇日〜〇日」の表記から判断し、表記がなければチラシ全体の期間。年は{year}年。
割引率だけの商品（例:表示価格より2割引）は price と price_tax を null にし、condition に割引内容を入れる。"""

SCHEMA = {"type": "OBJECT", "properties": {"items": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "name": {"type": "STRING"}, "item_key": {"type": "STRING"}, "category": {"type": "STRING"},
    "price": {"type": "NUMBER"}, "price_tax": {"type": "NUMBER"}, "quantity": {"type": "STRING"},
    "date_from": {"type": "STRING"}, "date_to": {"type": "STRING"}, "condition": {"type": "STRING"}},
    "required": ["name", "item_key", "category", "date_from", "date_to"]}}}, "required": ["items"]}


class Busy(Exception):
    """混雑・レート制限で再試行が必要"""


def call_api(model, body, timeout=180):
    req = urllib.request.Request(API.format(model), data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "x-goog-api-key": API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:300]
        except Exception:
            pass
        if e.code in (429, 500, 502, 503, 504):
            raise Busy(f"HTTP {e.code} {e.reason} {detail}")
        raise RuntimeError(f"HTTP {e.code} {e.reason} {detail}")


def pick_model():
    """実際に使えるモデル名を1つ選ぶ。"""
    forced = os.environ.get("GEMINI_MODEL")
    cands = [forced] if forced else MODEL_CANDIDATES
    for m in cands:
        for wait in [0] + WAIT_BACKOFF:
            if wait:
                time.sleep(wait)
            try:
                call_api(m, {"contents": [{"parts": [{"text": "ping"}]}], "generationConfig": {"maxOutputTokens": 5}}, timeout=60)
                log(f"使用モデル: {m}")
                return m
            except Busy as e:
                log(f"  モデル {m} は混雑: {e}")
            except Exception as e:
                log(f"  モデル {m} は使えません: {e}")
                break
    raise RuntimeError("利用できるモデルが見つかりませんでした。Google AI Studio のキーと、モデル名の設定を確認してください。")


def gemini_items(model, jpeg, prompt):
    """1枚の画像から商品を抽出。混雑時は待って再試行し、だめなら空を返す。"""
    body = {"contents": [{"role": "user", "parts": [
        {"text": prompt}, {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(jpeg).decode()}}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0}}
    schema = True
    for attempt in range(len(WAIT_BACKOFF) + 1):
        b = json.loads(json.dumps(body))
        if schema:
            b["generationConfig"]["responseSchema"] = SCHEMA
        try:
            res = call_api(model, b)
        except Busy as e:
            log(f"  混雑のため待機 ({attempt + 1}/{len(WAIT_BACKOFF) + 1}): {e}")
            if attempt < len(WAIT_BACKOFF):
                time.sleep(WAIT_BACKOFF[attempt])
                continue
            return None
        except RuntimeError as e:
            msg = str(e)
            if schema and ("schema" in msg.lower() or "400" in msg or "invalid" in msg.lower()):
                log(f"  出力形式の指定が通らないため、指定なしで再試行: {msg[:120]}")
                schema = False
                continue
            log(f"  読み取りエラー: {msg[:200]}")
            return None
        try:
            text = "".join(p.get("text", "") for p in res["candidates"][0]["content"]["parts"])
            return json.loads(text[text.find("{"): text.rfind("}") + 1]).get("items", [])
        except Exception as e:
            log(f"  応答を解釈できません: {e}")
            return None
    return None


def encode(im, box, q=90):
    buf = io.BytesIO()
    im.crop(box).save(buf, "JPEG", quality=q)
    return buf.getvalue()


def views(img_bytes):
    """読み取り用の画像を作る。まず全体を1枚で。小さい文字が多そうな大きいチラシは2分割も作る。"""
    im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    w, h = im.size
    scale = min(1.0, LONG_SIDE / max(w, h))
    im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    w, h = im.size
    out = [("全体", encode(im, (0, 0, w, h)))]
    if max(w, h) > SPLIT_OVER:
        ov = 120
        if h >= w:
            mid = h // 2
            out.append(("上半分", encode(im, (0, 0, w, min(h, mid + ov)))))
            out.append(("下半分", encode(im, (0, max(0, mid - ov), w, h))))
        else:
            mid = w // 2
            out.append(("左半分", encode(im, (0, 0, min(w, mid + ov), h))))
            out.append(("右半分", encode(im, (max(0, mid - ov), 0, w, h))))
    return out


def num(x):
    try:
        return None if x in (None, "", "null") else float(str(x).replace(",", "").replace("円", ""))
    except ValueError:
        return None


def normalize(it, flyer):
    cat = CAT_MAP.get(it.get("category"), it.get("category"))
    if cat not in CATEGORIES:
        cat = "その他"
    d = {"store": flyer["store"], "flyer_id": flyer["id"], "name": str(it.get("name") or "").strip(),
         "item_key": str(it.get("item_key") or it.get("name") or "").strip(), "category": cat,
         "price": num(it.get("price")), "price_tax": num(it.get("price_tax")),
         "quantity": it.get("quantity") or "", "date_from": it.get("date_from") or flyer["start"],
         "date_to": it.get("date_to") or flyer["end"], "condition": it.get("condition") or None}
    if not re.match(r"\d{4}-\d\d-\d\d$", d["date_from"]):
        d["date_from"] = flyer["start"]
    if not re.match(r"\d{4}-\d\d-\d\d$", d["date_to"]):
        d["date_to"] = flyer["end"]
    key = (d["item_key"], d["name"], d["price"], d["date_from"], d["date_to"], d["flyer_id"])
    d["id"] = hashlib.md5(json.dumps(key, ensure_ascii=False).encode()).hexdigest()[:10]
    return d


def read_flyer(model, flyer, t):
    prompt = PROMPT.format(store=STORE_NAME, name=flyer["name"], start=flyer["start"], end=flyer["end"],
                           year=flyer["start"][:4], cats="/".join(CATEGORIES))
    got, seen, failed = [], set(), False
    for label, jpeg in views(http_get(flyer["image"])):
        if COUNTER["failed"] >= MAX_FAILED:
            log("  読み取りの失敗が続くため、残りは次回に持ち越します")
            failed = True
            break
        items = gemini_items(model, jpeg, prompt)
        if items is None:
            failed = True
            COUNTER["failed"] += 1
            continue
        for it in items:
            d = normalize(it, flyer)
            if d["price"] is None and d["price_tax"] is None and not d["condition"]:
                continue
            if d["id"] in seen:
                continue
            seen.add(d["id"])
            got.append(d)
        log(f"  {label}: {len(items)}品（累計 {len(got)}品）")
        # 全体で十分取れたら分割は不要
        if label == "全体" and len(got) >= 8:
            break
        time.sleep(WAIT_BETWEEN)
    return got, failed


def main():
    if not API_KEY:
        sys.exit("GEMINI_API_KEY が設定されていません（GitHub の Secrets を確認してください）")
    t = today().isoformat()
    deals = load("deals.json", {"flyers": [], "items": []})
    processed = load("processed.json", {})
    history = load("history.json", {})

    flyers = list_flyers()
    log(f"{len(flyers)}件のチラシを確認（{STORE_NAME}）")
    targets = [f for f in flyers if f["end"] >= t and f["id"] not in processed]
    log(f"今回読み取るチラシ: {len(targets)}件")

    items = [i for i in deals.get("items", []) if i["date_to"] >= t]
    done, fail = 0, []
    if targets:
        model = pick_model()
        for f in targets:
            log(f"読み取り: {f['name']} ({f['start']}〜{f['end']})")
            got, failed = read_flyer(model, f, t)
            have = {i["id"] for i in items}
            items += [g for g in got if g["id"] not in have]
            if got:
                processed[f["id"]] = {"name": f["name"], "at": datetime.now(JST).isoformat(), "count": len(got)}
                done += 1
            else:
                fail.append(f["name"])
            if failed or not got:
                log(f"  {f['name']}: 読み取りが不完全のため、次回に持ち越します")
            if COUNTER["failed"] >= MAX_FAILED:
                log("混雑が続くため、残りのチラシは次回に持ち越します")
                break
            time.sleep(WAIT_BETWEEN)

    for i in items:
        p = i["price"] if i["price"] is not None else i["price_tax"]
        if p is None:
            continue
        rec = {"d": i["date_from"], "p": p, "q": i["quantity"], "s": i["store"]}
        h = history.setdefault(i["item_key"], [])
        if rec not in h:
            h.append(rec)

    active = {f["id"] for f in flyers if f["end"] >= t}
    save("deals.json", {"generated_at": datetime.now(JST).isoformat(timespec="seconds"),
                        "source": "LINEチラシ（ライフ阪神芦屋店）",
                        "flyers": [f for f in flyers if f["id"] in active],
                        "items": sorted(items, key=lambda x: (x["date_from"], x["category"]))})
    save("processed.json", processed)
    save("history.json", history)
    log(f"完了: {len(active)}件のチラシ・{len(items)}品を保存（今回読み取り {done}件）")
    if fail:
        log("次回に持ち越すチラシ: " + " / ".join(fail))
    if not items:
        sys.exit("商品を1品も読み取れませんでした。Google AI Studio のキーとモデル名を確認してください。")


if __name__ == "__main__":
    main()
