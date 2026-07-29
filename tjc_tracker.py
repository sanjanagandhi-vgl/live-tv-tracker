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
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

UK_TZ = ZoneInfo("Europe/London")

WATCH_URL = "https://www.tjc.co.uk/pages/livetv"
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
        "price": price, "title": title, "img": img, "login": False, "buy": buy, "oos": oos,
    }


def extract_missed_grid(raw_text):
    by_auction, by_sku = {}, {}

    try:
        data = json.loads(raw_text)
        arr = data.get("products") or data.get("items") or data.get("results") or (data if isinstance(data, list) else [])
        for p in arr:
            sku = str(p.get("sku") or p.get("variant_sku") or p.get("id") or "")
            if not sku:
                continue
            auction_code = p.get("auction_code") or p.get("auctionCode") or (p.get("properties") or {}).get("_auctionCode")
            price = p.get("price")
            if price is not None:
                price = float(price)
            elif p.get("price_formatted"):
                price = float(re.sub(r"[^\d.]", "", str(p["price_formatted"])))
            title = p.get("title") or p.get("product_title") or ""
            img = p.get("image") or p.get("featured_image")
            rec = {"title": title, "price": price, "auctionCode": auction_code, "img": img}
            if sku not in by_sku:
                by_sku[sku] = rec
            if auction_code and auction_code not in by_auction:
                by_auction[auction_code] = {"sku": sku, **rec}
        return by_auction, by_sku, "json"
    except Exception:
        pass

    for m in re.finditer(r'data-product-id="(\d+)"', raw_text):
        sku = m.group(1)
        win = raw_text[m.start():m.start() + 3000]
        title_m = re.search(r'class="[^"]*title[^"]*"[^>]*>\s*([\s\S]*?)\s*</[a-z]+>', win, re.I)
        title = html.unescape(re.sub(r"\s+", " ", title_m.group(1)).strip()) if title_m else ""
        price_m = re.search(r'£\s?([\d,.]+)', win)
        price = float(price_m.group(1).replace(",", "")) if price_m else None
        img_m = re.search(r'\ssrc="([^"]+)"', win)
        img = img_m.group(1) if img_m else None
        auction_m = re.search(r'name="properties\[_auctionCode\]" value="([^"]*)"', win)
        auction_code = auction_m.group(1) if auction_m else None
        if sku not in by_sku:
            by_sku[sku] = {"title": title, "price": price, "auctionCode": auction_code, "img": img}
        if auction_code and auction_code not in by_auction:
            by_auction[auction_code] = {"sku": sku, "title": title, "price": price, "img": img}
    return by_auction, by_sku, "html-fallback"


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
    with open(CSV_PATH, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Time", "SKU", "AuctionCode", "Title", "PDP Price", "Missed Price",
                    "Parity", "Overcharged", "OnAir Img", "Missed Img", "Buy", "Login",
                    "In Missed", "Delay(min)", "Source"])
        records = sorted(state["air"].values(), key=lambda a: a.get("lastUpdated", 0), reverse=True)
        for a in records:
            pdp, missed = a.get("pdpPrice"), a.get("missedPrice")
            par, overcharged = "", ""
            if pdp is not None and missed is not None:
                par = "MATCH" if abs(pdp - missed) <= 0.01 else "MISMATCH"
                overcharged = "Y" if missed > pdp + 0.01 else ""
            w.writerow([
                a.get("date", ""), a.get("time"), a.get("sku"), a.get("auctionCode"), a.get("title"),
                pdp, missed, par, overcharged,
                "Y" if a.get("img") else "N", "Y" if a.get("missedImg") else "N",
                "Y" if a.get("buy") else "N", "Y" if a.get("login") else "N",
                "Y" if a.get("missedAt") else "N", a.get("missedDelay"), a.get("source"),
            ])


def main():
    state = load_state()
    air = state.setdefault("air", {})
    t = int(time.time() * 1000)
    now_dt = datetime.now(UK_TZ)
    now_date = now_dt.strftime("%Y-%m-%d")
    now_str = now_dt.strftime("%H:%M:%S")

    w_status, w_body = fetch(WATCH_URL)
    m_status, m_body = fetch(MISSED_URL)
    print(f"watch status={w_status} len={len(w_body) if w_status == 200 else 0}")
    print(f"missed status={m_status} len={len(m_body) if m_status == 200 else 0}")

    if w_status == 200:
        r = extract_on_air(w_body)
        if r and r["sku"]:
            a = air.setdefault(r["sku"], {"sku": r["sku"], "firstAir": t, "tvPrice": "", "remarks": ""})
            a.update({
                "lastAir": t, "title": r["title"] or a.get("title"),
                "pdpPrice": r["price"], "auctionCode": r["auctionCode"],
                "buy": r["buy"], "login": r["login"],
                "img": is_valid_image(r["img"]), "date": now_date, "time": now_str,
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
        }

    save_state(state)
    export_csv(state)


if __name__ == "__main__":
    main()
