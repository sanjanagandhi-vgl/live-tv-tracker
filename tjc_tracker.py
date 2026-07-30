#!/usr/bin/env python3
"""TJC Web TV QA Tracker — GitHub Actions cron backup.
Fetches watch-tjc + missed-auctions directly, tracks on-air -> missed
transitions, computes delay, price parity, overcharge flag, image validity.
State persists in data/tjc_state.json (committed back to the repo each run).
"""
import csv
import html
import json
import os
import re
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

UK_TZ = ZoneInfo("Europe/London")

WATCH_URL = "https://www.tjc.co.uk/pages/livetv"
ONAIR_API_URL = "https://www.tjc.co.uk/apps/live-tv/currently-on-air"
MISSED_URL = "https://www.tjc.co.uk/apps/live-tv/last-24-hours"
STATE_PATH = "data/tjc_state.json"
LOG_PATH = "data/tjc_events.log"
CSV_PATH = "data/tjc_report.csv"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def fetch(url):
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(
        f"{url}{sep}_cb={int(time.time())}",
        headers={"User-Agent": UA, "Cache-Control": "no-cache", "Pragma": "no-cache"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except Exception as e:
        return None, str(e)


def is_valid_image(url):
    if not url:
        return False
    u = url.lower()
    from_shopify = "cdn.shopify.com" in u or "cdn/shop/files" in u or "tjcuk.sirv.com" in u
    return from_shopify and "no-image" not in u


def extract_on_air(raw_html):
    idx = raw_html.find('currently-on-air-card"')
    if idx < 0:
        return None
    start = raw_html.rfind('<article', 0, idx)
    if start < 0:
        return None
    win = raw_html[start:start + 16000]

    sku_m = re.search(r'class="currently-on-air-card__sku">\s*([\s\S]*?)\s*</p>', win)
    sku = sku_m.group(1).strip() if sku_m else None
    if not sku:
        return None
    product_id_m = re.search(r'data-product-id="(\d+)"', win)
    product_id = product_id_m.group(1) if product_id_m else None
    auction_m = re.search(r'name="properties\[_auctionCode\]" value="([^"]*)"', win)
    auction_code = auction_m.group(1) if auction_m else None
    title_m = re.search(r'class="currently-on-air-card__title">[\s\S]*?<a[^>]*>\s*([\s\S]*?)\s*</a>', win)
    title = html.unescape(re.sub(r"\s+", " ", title_m.group(1)).strip()) if title_m else None
    price_m = re.search(r'class="currently-on-air-card__price">\s*(?:£|&#163;|&pound;)?\s*([\d,.]+)', win)
    price = float(price_m.group(1).replace(",", "")) if price_m else None
    img_m = (re.search(r'class="currently-on-air-card__img"[^>]*\ssrc="([^"]+)"', win)
             or re.search(r'currently-on-air-card__media-link"[^>]*>\s*<img[^>]*\ssrc="([^"]+)"', win))
    img = img_m.group(1) if img_m else None
    url_m = re.search(r'<a href="(/products/[^"]+)" class="currently-on-air-card__media-link"', win)
    product_url = url_m.group(1) if url_m else None

    available = None
    inv_m = re.search(r'data-shopify-inventory="([^"]*)"', win)
    if inv_m:
        try:
            decoded = html.unescape(inv_m.group(1))
            inv = json.loads(decoded)
            if sku in inv:
                available = bool(inv[sku].get("availableForSale"))
        except Exception:
            pass
    oos = available is False
    has_btn = "currently-on-air-card__cta--add" in win
    buy = has_btn and not oos

    return {
        "sku": sku, "productId": product_id, "auctionCode": auction_code,
        "price": price, "title": title, "img": img, "productUrl": product_url,
        "login": False, "buy": buy, "oos": oos,
    }


def extract_on_air_api(raw_text):
    """Parse the dedicated currently-on-air API endpoint. Schema unconfirmed on first use —
    tries a few reasonable shapes (single object, or hours/auctions like the missed-grid API)
    and returns (result_dict_or_None, debug_sample_str_or_None)."""
    try:
        data = json.loads(raw_text)
    except Exception as e:
        return None, f"not JSON: {e}. First 500 chars: {raw_text[:500]}"

    def from_auction_obj(auc):
        sku = str(auc.get("stockCode") or auc.get("sku") or "")
        if not sku:
            return None
        shopify_data = auc.get("shopifyData") or {}
        media = shopify_data.get("productMedia") or []
        img = None
        if media:
            first = media[0]
            img = (first.get("src") or first.get("url")) if isinstance(first, dict) else first
        available = shopify_data.get("availableForSale")
        return {
            "sku": sku,
            "productId": shopify_data.get("productId"),
            "auctionCode": auc.get("auctionCode"),
            "price": auc.get("price") if auc.get("price") is not None else shopify_data.get("productPrice"),
            "title": auc.get("itemName") or shopify_data.get("productName"),
            "img": img,
            "productUrl": shopify_data.get("productUrl"),
            "login": False,
            "oos": available is False,
            "buy": available is not False,
        }

    payload = data.get("data") if isinstance(data, dict) else None
    if isinstance(payload, dict):
        if payload.get("stockCode") or payload.get("auctionCode"):
            r = from_auction_obj(payload)
            if r:
                return r, None
        hours = payload.get("hours") or []
        for hour in hours:
            for auc in (hour.get("auctions") or []):
                status = str(auc.get("runningStatus") or "").lower()
                if status in ("live", "on air", "onair", "running", "current"):
                    r = from_auction_obj(auc)
                    if r:
                        return r, None
        for hour in hours:
            for auc in (hour.get("auctions") or []):
                r = from_auction_obj(auc)
                if r:
                    return r, None
    if isinstance(data, dict) and (data.get("stockCode") or data.get("auctionCode")):
        r = from_auction_obj(data)
        if r:
            return r, None

    return None, f"JSON parsed but no recognizable on-air fields. Keys: {list(data.keys()) if isinstance(data, dict) else type(data)}. Sample: {raw_text[:800]}"


def extract_missed_grid(raw_text):
    by_auction, by_sku = {}, {}

    try:
        data = json.loads(raw_text)
    except Exception:
        return by_auction, by_sku, "parse-error"

    hours = ((data.get("data") or {}).get("hours")) or []
    for hour in hours:
        for auc in (hour.get("auctions") or []):
            sku = str(auc.get("stockCode") or "")
            if not sku:
                continue
            auction_code = auc.get("auctionCode")
            price = auc.get("price")
            if price is not None:
                price = float(price)
            title = auc.get("itemName") or ""
            shopify_data = auc.get("shopifyData") or {}
            img = None
            media = shopify_data.get("productMedia") or []
            if media:
                first = media[0]
                img = (first.get("src") or first.get("url")) if isinstance(first, dict) else first
            product_url = shopify_data.get("productUrl")
            rec = {"title": title, "price": price, "auctionCode": auction_code, "img": img, "productUrl": product_url}
            if sku not in by_sku:
                by_sku[sku] = rec
            if auction_code and auction_code not in by_auction:
                by_auction[auction_code] = {"sku": sku, **rec}
    return by_auction, by_sku, "json"


def check_product_url(rel_url):
    if not rel_url:
        return None, "no-url"
    sep = "&" if "?" in rel_url else "?"
    abs_url = f"https://www.tjc.co.uk{rel_url}{sep}_cb={int(time.time())}"
    req = urllib.request.Request(abs_url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, None
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return None, str(e)


URL_CHECKS_PER_RUN = 15  # cap per GitHub Actions run since it fires every ~1 min via cron-job.org


def run_product_url_checks(air):
    checked = 0
    for sku, a in air.items():
        if checked >= URL_CHECKS_PER_RUN:
            break
        if not a.get("productUrl") or a.get("productUrlChecked"):
            continue
        status, err = check_product_url(a["productUrl"])
        a["productUrlChecked"] = int(time.time() * 1000)
        a["productUrlStatus"] = status if status is not None else "error"
        a["productUrl404"] = status == 404
        if status == 404:
            log_event(f"404 ERROR: {sku} — product page not found ({a['productUrl']})")
        checked += 1
    return checked


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"air": {}}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def log_event(msg):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_PATH, "a") as f:
        f.write(f"[{stamp} UTC] {msg}\n")
    print(msg)


def export_csv(state):
    os.makedirs("data", exist_ok=True)

    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)

        w.writerow([
            "Date", "Time", "SKU", "AuctionCode", "Title",
            "PDP Price", "Missed Price",
            "Parity", "Overcharged",
            "OnAir Img", "Missed Img",
            "ProductUrl", "Is404",
            "Buy", "Login",
            "In Missed", "Delay(min)", "Source"
        ])

        records = sorted(
            state["air"].values(),
            key=lambda a: a.get("lastUpdated", 0),
            reverse=True
        )

        for a in records:

            pdp = a.get("pdpPrice")
            missed = a.get("missedPrice")

            # Safely convert to float
            try:
                pdp = float(pdp) if pdp not in (None, "") else None
            except (TypeError, ValueError):
                pdp = None

            try:
                missed = float(missed) if missed not in (None, "") else None
            except (TypeError, ValueError):
                missed = None

            parity = ""
            overcharged = ""

            if pdp is not None and missed is not None:
                parity = "MATCH" if abs(pdp - missed) <= 0.01 else "MISMATCH"
                overcharged = "Y" if missed > pdp + 0.01 else ""

            w.writerow([
                a.get("date", ""),
                a.get("time", ""),
                a.get("sku", ""),
                a.get("auctionCode", ""),
                a.get("title", ""),
                pdp,
                missed,
                parity,
                overcharged,
                "Y" if a.get("img") else "N",
                "Y" if a.get("missedImg") else "N",
                a.get("productUrl", ""),
                "Y" if a.get("productUrl404") else "N",
                "Y" if a.get("buy") else "N",
                "Y" if a.get("login") else "N",
                "Y" if a.get("missedAt") else "N",
                a.get("missedDelay"),
                a.get("source", "")
            ])


def main():
    state = load_state()
    air = state.setdefault("air", {})
    t = int(time.time() * 1000)
    now_dt = datetime.now(UK_TZ)
    now_date = now_dt.strftime("%Y-%m-%d")
    now_str = now_dt.strftime("%H:%M:%S")

    w_status, w_body = fetch(WATCH_URL)
    api_status, api_body = fetch(ONAIR_API_URL)
    m_status, m_body = fetch(MISSED_URL)
    print(f"watch status={w_status} len={len(w_body) if w_status == 200 else 0}")
    print(f"on-air API status={api_status} len={len(api_body) if api_status == 200 else 0}")
    print(f"missed status={m_status} len={len(m_body) if m_status == 200 else 0}")

    r = None
    if api_status == 200:
        r, api_debug = extract_on_air_api(api_body)
        if r:
            print(f"on-air (via live API): {r['sku']} auction={r['auctionCode']} price={r['price']}")
        else:
            print(f"DEBUG: on-air API did not yield usable data — {api_debug}")
    if not r and w_status == 200:
        r = extract_on_air(w_body)
        if r and r["sku"]:
            print(f"on-air (via page scrape, fallback): {r['sku']} auction={r['auctionCode']} price={r['price']}")

    if r and r["sku"]:
        a = air.setdefault(r["sku"], {"sku": r["sku"], "firstAir": t, "tvPrice": "", "remarks": ""})
        a.update({
            "lastAir": t, "title": r["title"] or a.get("title"),
            "pdpPrice": r["price"], "auctionCode": r["auctionCode"],
            "buy": r["buy"], "login": r["login"],
            "img": is_valid_image(r["img"]), "date": now_date, "time": now_str,
            "productUrl": r["productUrl"] or a.get("productUrl"),
            "lastUpdated": t, "source": "gha",
        })
        print(f"on-air: {r['sku']} auction={r['auctionCode']} price={r['price']}")
    else:
        print("no on-air SKU parsed — page layout may differ from a bare fetch")

    by_auction, by_sku, parsed_as = extract_missed_grid(m_body) if m_status == 200 else ({}, {}, "n/a")
    print(f"missed grid tiles parsed: {len(by_sku)} (as {parsed_as})")

    if m_status == 200 and len(by_sku) == 0:
        marker = m_body.find('_auctionCode')
        if marker >= 0:
            count = m_body.count('_auctionCode')
            print(f"DEBUG: found '_auctionCode' {count} time(s) in missed response — sample around first occurrence:")
            print(m_body[max(0, marker - 400):marker + 1500])
            print("---END SAMPLE---")
        else:
            pid_marker = m_body.find('data-product-id')
            if pid_marker >= 0:
                print(f"DEBUG: no '_auctionCode' found, but 'data-product-id' occurs {m_body.count('data-product-id')} time(s) — sample:")
                print(m_body[max(0, pid_marker - 200):pid_marker + 1500])
                print("---END SAMPLE---")
            else:
                print("DEBUG: neither '_auctionCode' nor 'data-product-id' found anywhere in the response.")
                print(f"DEBUG: response length={len(m_body)}. First 800 chars:\n{m_body[:800]}")
                print(f"DEBUG: last 800 chars:\n{m_body[-800:]}")
                print("---END SAMPLE---")

    for sku, a in list(air.items()):
        if a.get("missedAt"):
            continue
        hit = by_auction.get(a.get("auctionCode")) or by_sku.get(sku)
        if not hit:
            continue
        a["missedAt"] = t
        a["missedDelay"] = max(0, round((t - a["lastAir"]) / 60000)) if a.get("lastAir") else None
        a["missedPrice"] = hit["price"]
        a["missedImg"] = is_valid_image(hit["img"])
        a["title"] = a.get("title") or hit["title"]
        a["productUrl"] = a.get("productUrl") or hit.get("productUrl")
        a["lastUpdated"] = t
        log_event(f"RECENTLY-ON-AIR: {sku} appeared (delay ~{a['missedDelay']}m, £{hit['price']})")

    for sku, hit in by_sku.items():
        if sku in air:
            continue
        air[sku] = {
            "sku": sku, "tvPrice": "", "remarks": "", "date": now_date, "time": now_str,
            "preExisting": True, "missedAt": t, "missedDelay": None, "lastUpdated": t,
            "missedPrice": hit["price"], "missedImg": is_valid_image(hit["img"]),
            "title": hit["title"], "auctionCode": hit["auctionCode"], "source": "gha",
            "productUrl": hit.get("productUrl"),
        }

    checked_count = run_product_url_checks(air)
    print(f"product URL checks this run: {checked_count}")

    save_state(state)
    export_csv(state)


if __name__ == "__main__":
    main()
