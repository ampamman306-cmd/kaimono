"""ライフ阪神芦屋店のチラシを取得し、Gemini で商品・価格・適用日を読み取って data/deals.json を更新する。

実行: GEMINI_API_KEY=xxx python fetch_life.py
- 1日1回の実行を想定。読み取り済みのチラシは data/processed.json で記録し、再読み取りしない。
- まず API キーで「使えるモデル」を問い合わせ、無料枠で通りやすいモデルから順に試す。
- 混雑(429/503)は待って再試行、タイムアウトは画像を小さくして再試行。失敗が続いたら打ち切って次回に持ち越す。
"""
import base64, hashlib, io, json, math, os, re, socket, sys, time, urllib.error, urllib.parse, urllib.request
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
BASE = "https://generativelanguage.googleapis.com/v1beta/"
API_KEY = os.environ.get("GEMINI_API_KEY", "")

LONG_SIDE = 1600       # 読み取り用画像の長辺
SPLIT_OVER = 2400      # 縮小後の長辺がこれを超える場合のみ分割
WAIT_BACKOFF = [20, 60]      # 429/503 のときの待ち時間（秒）
WAIT_BETWEEN = 8             # チラシ間の待ち時間（秒）
TIMEOUT = 150                # 1リクエストの制限時間（秒）
MAX_CALLS = 24               # 1回の実行で許す API 呼び出しの上限
MAX_FAILED = 3               # 読み取りに失敗した画像が続いたら打ち切る
RETRY_SCALES = [1.0, 0.68]   # タイムアウト時は画像を小さくしてやり直す

SKIP_WORDS = ("tts", "image", "live", "transcribe", "embedding", "omni", "veo", "lyria",
              "robotics", "deep-research", "antigravity", "computer", "gemma")

CATEGORIES = ["野菜", "果物", "肉", "魚", "惣菜", "日配", "加工食品", "菓子", "飲料", "酒", "米", "パン", "日用品", "その他"]
CAT_MAP = {"精肉": "肉", "精肉加工品": "肉", "鮮魚": "魚", "乳製品": "日配", "チーズ": "日配", "デザート": "日配",
           "ベーカリー・パン": "パン", "日用消耗品": "日用品", "健康・ヘルスケア": "日用品", "洋菓子": "菓子",
           "ノンアルコール飲料": "飲料", "冷凍食品": "加工食品", "レトルト": "加工食品", "調味料": "加工食品",
           "麺類": "加工食品", "粉類": "加工食品", "漬物": "日配"}


def log(*a):
    print(*a, flush=True)


class Busy(Exception):
    """混雑・レート制限。待てば通る可能性がある。"""


class Fatal(Exception):
    """キーや設定の問題。モデルを変えても無駄。"""


class Budget(Exception):
    """この実行で許した API 呼び出しの上限に達した。"""


class NoFreeQuota(Exception):
    """このモデルは無料枠の上限が 0（＝無料では使えない）。別のモデルを試す。"""


class DailyQuota(Exception):
    """1日の上限に達した。待っても今日は通らないので、この実行を終える。"""


STATE = {"calls": 0, "failed": 0, "daily": False}
COOLDOWN = set()   # この実行中に混雑で失敗したモデル（以降は試さない）


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


def http_get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def to_jst_date(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(JST).date().isoformat()


def request(path, body=None, timeout=TIMEOUT, query_key=False):
    """Gemini API を1回呼ぶ。混雑=Busy、キーや設定の問題=Fatal、時間切れ=TimeoutError。"""
    if STATE["calls"] >= MAX_CALLS:
        raise Budget(f"API 呼び出しの上限 {MAX_CALLS} 回に達しました")
    STATE["calls"] += 1
    url = BASE + path
    headers = {"Content-Type": "application/json"}
    if query_key:
        url += ("&" if "?" in url else "?") + "key=" + urllib.parse.quote(API_KEY)
    else:
        headers["x-goog-api-key"] = API_KEY
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.load(r)
        log(f"  API {path.split(chr(63))[0]} 成功 {int((time.time()-t0)*1000)}ms")
        return out
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", "ignore")
        except Exception:
            pass
        detail = re.sub(r"\s+", " ", raw)[:400]
        if e.code == 429:
            low = raw.lower()
            log(f"  Google からの応答: {re.sub(chr(92)+'s+', ' ', raw)[:600]}")
            if "limit: 0" in low or '"quotavalue": "0"' in low or "limit\": 0" in low:
                raise NoFreeQuota(f"HTTP 429 無料枠なし / {detail}")
            if "perday" in low or "per_day" in low or "requests per day" in low or "daily" in low:
                raise DailyQuota(f"HTTP 429 1日の上限 / {detail}")
            raise Busy(f"HTTP 429 {e.reason} / {detail}")
        if e.code in (500, 502, 503, 504):
            raise Busy(f"HTTP {e.code} {e.reason} / {detail}")
        if e.code in (400, 401, 403, 404):
            raise Fatal(f"HTTP {e.code} {e.reason} / {detail}")
        raise Fatal(f"HTTP {e.code} {e.reason} / {detail}")
    except (socket.timeout, TimeoutError) as e:
        raise TimeoutError(f"{int((time.time()-t0)*1000)}ms で応答なし（{e}）")


def list_models():
    """キーで使えるモデルの名前を取得して、試す順に並べる。"""
    forced = os.environ.get("GEMINI_MODEL", "").strip()
    if forced:
        log(f"モデルは設定で指定されています: {forced}")
        return [forced]
    for query_key in (False, True):
        try:
            d = request("models?pageSize=200", None, timeout=60, query_key=query_key)
            break
        except Busy as e:
            log(f"  モデル一覧の取得に失敗（混雑）: {e}")
            time.sleep(WAIT_BACKOFF[0])
        except Fatal as e:
            if "401" in str(e) or "403" in str(e):
                log(f"  キーの確認: {e}")
                if not query_key:
                    continue
            raise
    else:
        raise Fatal("モデル一覧を取得できませんでした（キーを確認してください）")

    names = []
    for m in d.get("models", []):
        name = m.get("name", "").split("/")[-1]
        methods = m.get("supportedGenerationMethods") or m.get("supportedActions") or []
        if methods and "generateContent" not in methods and "generateContent" not in str(methods):
            continue
        if not name or any(w in name.lower() for w in SKIP_WORDS):
            continue
        names.append(name)

    def rank(n):
        ver = re.findall(r"(\d+(?:\.\d+)?)", n)
        v = float(ver[0]) if ver else 0.0
        tier = 0 if "flash-lite" in n else (1 if "flash" in n else 2)
        return (tier, -v)

    names = sorted(set(names), key=rank)
    log(f"使えるモデル（試す順）: {', '.join(names[:6])}")
    if not names:
        raise Fatal("generateContent に対応したモデルが見つかりませんでした")
    return names


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


def parse_items(res):
    text = "".join(p.get("text", "") for p in res["candidates"][0]["content"]["parts"])
    js = json.loads(text[text.find("{"): text.rfind("}") + 1])
    return js.get("items", [])


def ask_model(model, jpeg, prompt, use_schema=True):
    body = {"contents": [{"role": "user", "parts": [
        {"text": prompt}, {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(jpeg).decode()}}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0}}
    if use_schema:
        body["generationConfig"]["responseSchema"] = SCHEMA
    return request(f"models/{model}:generateContent", body)


def extract_items(models, jpeg, prompt):
    """画像1枚を読む。モデルを順に試し、待ってもだめなら None。"""
    usable = [m for m in models if m not in COOLDOWN] or models
    for model in usable:
        use_schema = True
        for attempt in range(len(WAIT_BACKOFF) + 1):
            try:
                return parse_items(ask_model(model, jpeg, prompt, use_schema))
            except Budget as e:
                log(f"  {e}")
                return None
            except DailyQuota:
                log(f"  {model}: 今日の利用上限に達しています。次のモデルを試します")
                COOLDOWN.add(model)
                STATE["daily"] = True
                break
            except NoFreeQuota:
                log(f"  {model}: このキーでは無料枠が 0 のモデルです。次のモデルを試します")
                COOLDOWN.add(model)
                break
            except TimeoutError as e:
                log(f"  {model}: 応答なし {e}")
                break
            except Busy as e:
                log(f"  {model}: 混雑 ({attempt + 1}/{len(WAIT_BACKOFF) + 1}) {e}")
                if attempt < len(WAIT_BACKOFF):
                    time.sleep(WAIT_BACKOFF[attempt])
                    continue
                COOLDOWN.add(model)
                log(f"  {model}: 今回の実行では使わないことにします")
                break
            except Fatal as e:
                msg = str(e)
                if use_schema and ("400" in msg or "schema" in msg.lower() or "invalid" in msg.lower()):
                    log(f"  {model}: 出力形式の指定が通らないため、指定なしで再試行")
                    use_schema = False
                    continue
                log(f"  {model}: 使えません {msg}")
                COOLDOWN.add(model)
                break
            except Exception as e:
                log(f"  {model}: 応答を解釈できません {str(e)[:200]}")
                break
    return None


def encode(im, box, q=90):
    buf = io.BytesIO()
    im.crop(box).save(buf, "JPEG", quality=q)
    return buf.getvalue()


def views(img_bytes, long_side):
    im = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    w, h = im.size
    scale = min(1.0, long_side / max(w, h))
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


def read_flyer(models, flyer):
    prompt = PROMPT.format(store=STORE_NAME, name=flyer["name"], start=flyer["start"], end=flyer["end"],
                           year=flyer["start"][:4], cats="/".join(CATEGORIES))
    try:
        img = http_get(flyer["image"], timeout=120)
    except Exception as e:
        log(f"  チラシ画像を取得できません: {str(e)[:150]}")
        return [], True
    got, seen, failed = [], set(), False
    for scale in RETRY_SCALES:
        for label, jpeg in views(img, int(LONG_SIDE * scale)):
            if STATE["failed"] >= MAX_FAILED:
                log("  読み取りの失敗が続くため、残りは次回に持ち越します")
                return got, True
            items = extract_items(models, jpeg, prompt)
            if items is None:
                failed = True
                STATE["failed"] += 1
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
            if label == "全体" and len(got) >= 8:
                return got, failed
            time.sleep(WAIT_BETWEEN)
        if got:
            return got, failed
        log(f"  サイズを {int(scale * 100)}% にして再試行します")
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
        models = list_models()
        for f in targets:
            if all(m in COOLDOWN for m in models):
                log("使えるモデルがすべて上限・混雑のため、残りは次回に持ち越します")
                fail.append(f["name"])
                break
            log(f"読み取り: {f['name']} ({f['start']}〜{f['end']})")
            try:
                got, failed = read_flyer(models, f)
            except DailyQuota as e:
                log("")
                log("■ Gemini の1日の利用上限に達しています。今日はこれ以上読み取れません。")
                log("  上限は日本時間 16:00（太平洋時間の0時）に戻ります。翌朝の自動実行で再挑戦します。")
                log("  利用状況: https://aistudio.google.com/rate-limit")
                fail.append(f["name"])
                break
            have = {i["id"] for i in items}
            items += [g for g in got if g["id"] not in have]
            if got:
                processed[f["id"]] = {"name": f["name"], "at": datetime.now(JST).isoformat(), "count": len(got)}
                done += 1
            if failed or not got:
                fail.append(f["name"])
                log(f"  {f['name']}: 読み取りが不完全のため、次回に持ち越します")
            if STATE["failed"] >= MAX_FAILED or STATE["calls"] >= MAX_CALLS:
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
    log(f"完了: {len(active)}件のチラシ・{len(items)}品を保存（今回読み取り {done}件 / API {STATE['calls']}回）")
    if fail:
        log("次回に持ち越すチラシ: " + " / ".join(fail))
    if STATE["daily"]:
        log("")
        log("■ Gemini の無料枠の『1日の上限』に達したモデルがありました。")
        log("  上限は日本時間 16:00 に戻ります（太平洋時間の0時）。手動実行を繰り返さず、翌朝の自動実行に任せてください。")
        log("  利用状況: https://aistudio.google.com/rate-limit")
    if not done and fail:
        sys.exit("今回は読み取れませんでした（理由は上のログ参照。翌朝の自動実行で再挑戦します）")


if __name__ == "__main__":
    main()
