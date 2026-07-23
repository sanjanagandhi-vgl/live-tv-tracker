#!/usr/bin/env python3
"""TJC Web TV QA Tracker — GitHub Actions cron backup.
Fetches watch-tjc + missed-auctions directly, tracks on-air -> missed
transitions, computes delay, price parity, overcharge flag, image validity.
State persists in data/tjc_state.json (committed back to the repo each run).
"""
import csv
import json
import os
import re
import time
import urllib.request
from datetime import datetime, timezone

WATCH_URL = "https://www.tjc.co.uk/watch-tjc"
MISSED_URL = "https://www.tjc.co.uk/live-tv/missed-auctions"
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
    return "sirv.com" in u and "noimage" not in u


def extract_on_air(html):
    idx = html.find("newLiveTVcurrProd")
    if idx < 0:
        return None
    start = html.rfind('<div class="tile-inner', 0, idx)
    if start < 0:
        return None
    win = html[start:start + 6000]

    def m1(pat):
        mm = re.search(pat, win)
        return mm.group(1) if mm else None

    auction_code = m1(r'data-auctioncode="(\d+)"')
    price = m1(r'data-price="([\d.]+)"')
    sku = m1(r'class="product-id" data-value="(\d+)"')
    if not sku:
        return None
    title_m = re.search(r'class="text-bottom"[^>]*>\s*([\s\S]*?)\s*</div>', win)
    title = re.sub(r"\s+", " ", title_m.group(1)).strip() if title_m else None
    img = m1(r'class="Sirv image-main"[^>]*\ssrc="([^"]+)"')
    login = bool(re.search(r'home-page-login-bid-now|href="/live-tv/login"', win))
    buy = (not login) and bool(re.search(r'id="ltvpagebidnow"[^>]*class="[^"]*enablebutton', win))
    return {
        "sku": sku, "auctionCode": auction_code,
        "price": float(price) if price else None,
        "title": title, "img": img, "login": login, "buy": buy,
    }


def extract_missed_grid(html):
    by_auction, by_sku = {}, {}
    for m in re.finditer(r'data-pdpproductid="(\d+)"', html):
        top_sku = m.group(1)
        win = html[m.start():m.start() + 5000]
        top_auction_m = re.search(r'data-auctioncode="(\d+)"', win)
        top_auction = top_auction_m.group(1) if top_auction_m else None
        title_m = re.search(r'class="product-name mb-0">\s*([\s\S]*?)\s*</div>', win)
        title = re.sub(r"\s+", " ", title_m.group(1)).strip() if title_m else ""
        price_m = re.search(r'class="price-sales"[^>]*>\s*£?([\d,.]+)', win)
        price = float(price_m.group(1).replace(",", "")) if price_m else None
        top_img_m = (re.search(r'class="clickable-image pdp-main-img"[^>]*\ssrc="([^"]+)"', win)
                     or re.search(r'class="Sirv image-main"[^>]*\ssrc="([^"]+)"', win))
        top_img = top_img_m.group(1) if top_img_m else None

        if top_sku and top_sku not in by_sku:
            by_sku[top_sku] = {"title": title, "price": price, "auctionCode": top_auction, "img": top_img}
        if top_auction and top_auction not in by_auction:
            by_auction[top_auction] = {"sku": top_sku, "title": title, "price": price, "img": top_img}

        sel_end = win.find("</select>")
        sel_win = win[:sel_end] if sel_end >= 0 else win
        for opt in re.finditer(r'<option\b[^>]*>', sel_win):
            tag = opt.group(0)
            a_m = re.search(r'data-auctioncode="(\d+)"', tag)
            if not a_m:
                continue
            opt_auction = a_m.group(1)
            v_m = re.search(r'\svalue="([^"]+)"', tag)
            opt_sku = v_m.group(1) if v_m else None
            if not opt_sku or "," in opt_sku:
                continue
            n_m = re.search(r'data-name="([^"]*)"', tag)
            opt_title = n_m.group(1) if n_m else title
            i_m = re.search(r'data-image="([^"]*)"', tag)
            opt_img = i_m.group(1) if i_m else top_img
            if opt_auction not in by_auction:
                by_auction[opt_auction] = {"sku": opt_sku, "title": opt_title, "price": price, "img": opt_img}
            if opt_sku not in by_sku:
                by_sku[opt_sku] = {"title": opt_title, "price": price, "auctionCode": opt_auction, "img": opt_img}
    return by_auction, by_sku


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
        w.writerow(["Time", "SKU", "AuctionCode", "Title", "PDP Price", "Missed Price",
                    "Parity", "Overcharged", "OnAir Img", "Missed Img", "Buy", "Login",
                    "In Missed", "Delay(min)", "Source"])
        for a in state["air"].values():
            pdp, missed = a.get("pdpPrice"), a.get("missedPrice")
            par, overcharged = "", ""
            if pdp is not None and missed is not None:
                par = "MATCH" if abs(pdp - missed) <= 0.01 else "MISMATCH"
                overcharged = "Y" if missed > pdp + 0.01 else ""
            w.writerow([
                a.get("time"), a.get("sku"), a.get("auctionCode"), a.get("title"),
                pdp, missed, par, overcharged,
                "Y" if a.get("img") else "N", "Y" if a.get("missedImg") else "N",
                "Y" if a.get("buy") else "N", "Y" if a.get("login") else "N",
                "Y" if a.get("missedAt") else "N", a.get("missedDelay"), a.get("source"),
            ])


def main():
    state = load_state()
    air = state.setdefault("air", {})
    t = int(time.time() * 1000)
    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")

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
                "img": is_valid_image(r["img"]), "time": now_str, "source": "gha",
            })
            print(f"on-air: {r['sku']} auction={r['auctionCode']} price={r['price']}")
        else:
            print("no on-air SKU parsed — page layout may differ from a bare fetch")

    by_auction, by_sku = extract_missed_grid(m_body) if m_status == 200 else ({}, {})
    print(f"missed grid tiles parsed: {len(by_sku)}")

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
        log_event(f"RECENTLY-ON-AIR: {sku} appeared (delay ~{a['missedDelay']}m, £{hit['price']})")

    for sku, hit in by_sku.items():
        if sku in air:
            continue
        air[sku] = {
            "sku": sku, "tvPrice": "", "remarks": "", "time": now_str,
            "preExisting": True, "missedAt": t, "missedDelay": None,
            "missedPrice": hit["price"], "missedImg": is_valid_image(hit["img"]),
            "title": hit["title"], "auctionCode": hit["auctionCode"], "source": "gha",
        }

    save_state(state)
    export_csv(state)


if __name__ == "__main__":
    main()
