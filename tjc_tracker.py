#!/usr/bin/env python3

"""
TJC Web TV QA Tracker — GitHub Actions

Tracks:
- Currently On Air SKU
- Variant SKUs
- Missed auction transitions
- Delay time
- Price parity
- Image validity
- Stock availability
- Product URL status

State:
data/tjc_state.json

Output:
data/tjc_report.csv
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


WATCH_URL = (
    "https://www.tjc.co.uk/pages/livetv"
)

ONAIR_API_URL = (
    "https://www.tjc.co.uk/apps/live-tv/currently-on-air"
)

MISSED_URL = (
    "https://www.tjc.co.uk/apps/live-tv/last-24-hours"
)


STATE_PATH = (
    "data/tjc_state.json"
)

LOG_PATH = (
    "data/tjc_events.log"
)

CSV_PATH = (
    "data/tjc_report.csv"
)


UA = (
    "Mozilla/5.0 "
    "(Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)



# -------------------------------------------------------
# HTTP FETCH
# -------------------------------------------------------

def fetch(url):

    sep = "&" if "?" in url else "?"

    request_url = (
        f"{url}{sep}_cb={int(time.time())}"
    )

    req = urllib.request.Request(
        request_url,
        headers={
            "User-Agent": UA,
            "Cache-Control": "no-cache",
            "Pragma": "no-cache"
        }
    )


    try:

        with urllib.request.urlopen(
            req,
            timeout=25
        ) as response:

            return (
                response.status,
                response.read()
                .decode(
                    "utf-8",
                    "ignore"
                )
            )


    except Exception as e:

        return (
            None,
            str(e)
        )



# -------------------------------------------------------
# IMAGE CHECK
# -------------------------------------------------------

def is_valid_image(url):

    if not url:
        return False


    u = url.lower()


    valid_domain = (

        "cdn.shopify.com" in u
        or
        "cdn/shop/files" in u
        or
        "tjcuk.sirv.com" in u

    )


    if not valid_domain:
        return False


    if "no-image" in u:
        return False


    return True




# -------------------------------------------------------
# PRODUCT URL CHECK
# -------------------------------------------------------

def check_product_url(rel_url):

    if not rel_url:
        return (
            None,
            "no-url"
        )


    if rel_url.startswith("http"):

        url = rel_url

    else:

        url = (
            "https://www.tjc.co.uk"
            +
            rel_url
        )


    sep = "&" if "?" in url else "?"


    url = (
        f"{url}{sep}"
        f"_cb={int(time.time())}"
    )


    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA
        }
    )


    try:

        with urllib.request.urlopen(
            req,
            timeout=15
        ) as r:

            return (
                r.status,
                None
            )


    except urllib.error.HTTPError as e:

        return (
            e.code,
            None
        )


    except Exception as e:

        return (
            None,
            str(e)
        )



URL_CHECKS_PER_RUN = 15




def run_product_url_checks(air):

    checked = 0


    for sku, item in air.items():


        if checked >= URL_CHECKS_PER_RUN:
            break


        if (
            not item.get("productUrl")
            or
            item.get("productUrlChecked")
        ):
            continue



        status, err = check_product_url(
            item["productUrl"]
        )


        item["productUrlChecked"] = (
            int(time.time()*1000)
        )


        item["productUrlStatus"] = (
            status
            if status
            else
            "error"
        )


        item["productUrl404"] = (
            status == 404
        )


        if status == 404:

            log_event(
                f"404 ERROR {sku}: "
                f"{item['productUrl']}"
            )


        checked += 1



    return checked






# -------------------------------------------------------
# STATE
# -------------------------------------------------------

def load_state():

    if os.path.exists(
        STATE_PATH
    ):

        with open(
            STATE_PATH,
            encoding="utf-8"
        ) as f:

            return json.load(f)


    return {
        "air": {}
    }





def save_state(state):

    os.makedirs(
        "data",
        exist_ok=True
    )


    with open(
        STATE_PATH,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            state,
            f,
            indent=2
        )





def log_event(msg):

    os.makedirs(
        "data",
        exist_ok=True
    )


    stamp = datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


    line = (
        f"[{stamp} UTC] "
        f"{msg}\n"
    )


    with open(
        LOG_PATH,
        "a",
        encoding="utf-8"
    ) as f:

        f.write(line)


    print(msg)




# -------------------------------------------------------
# CURRENTLY ON AIR HTML PARSER
# -------------------------------------------------------

def extract_on_air(raw_html):


    idx = raw_html.find(
        "currently-on-air-card"
    )


    if idx < 0:
        return None



    start = raw_html.rfind(
        "<article",
        0,
        idx
    )


    if start < 0:
        return None



    block = raw_html[
        start:
        start + 30000
    ]



    sku = None


    sku_match = re.search(
        r'class="currently-on-air-card__sku">'
        r'\s*([\d]+)',
        block
    )


    if sku_match:

        sku = sku_match.group(1)




    if not sku:

        return None




    title_match = re.search(
        r'class="currently-on-air-card__title">'
        r'.*?<a[^>]*>'
        r'(.*?)</a>',
        block,
        re.S
    )


    title = None


    if title_match:

        title = html.unescape(
            re.sub(
                r"\s+",
                " ",
                title_match.group(1)
            ).strip()
        )




    price_match = re.search(
        r'class="currently-on-air-card__price">'
        r'.*?([\d,.]+)',
        block,
        re.S
    )


    price = None


    if price_match:

        price = float(
            price_match.group(1)
            .replace(",", "")
        )




    auction_match = re.search(
        r'name="properties\[_auctionCode\]"'
        r'\s+value="([^"]+)"',
        block
    )


    auction = (
        auction_match.group(1)
        if auction_match
        else None
    )




    product_id = None


    pid = re.search(
        r'data-product-id="(\d+)"',
        block
    )


    if pid:

        product_id = pid.group(1)




    image = None


    img = re.search(
        r'<img[^>]+src="([^"]+)"',
        block
    )


    if img:

        image = img.group(1)




    product_url = None


    url = re.search(
        r'href="(/products/[^"]+)"',
        block
    )


    if url:

        product_url = url.group(1)




    return {

        "sku": sku,

        "title": title,

        "price": price,

        "auctionCode": auction,

        "productId": product_id,

        "img": image,

        "productUrl": product_url,

        "buy": (
            "cta--add"
            in block
        ),

        "login": (
            "cta--login"
            in block
        )

    }
# -------------------------------------------------------
# MISSED AUCTION SKU / VARIANT PARSER
# -------------------------------------------------------

def extract_missed_grid(raw_text):

    """
    Extract missed auction data at SKU level.

    Handles:
    - groupauctions
    - mlauctions
    - variantSizeBySku
    - inventoryBySku

    Output:
        by_auction
        by_sku
    """


    by_auction = {}

    by_sku = {}



    try:

        data = json.loads(
            raw_text
        )


    except Exception:

        return (
            by_auction,
            by_sku,
            "parse-error"
        )




    hours = (

        (data.get("data") or {})
        .get("hours")
        or []

    )



    # ---------------------------------------------------
    # Helper
    # ---------------------------------------------------

    def add_sku(
        item,
        meta
    ):


        sku = str(
            item.get(
                "stockCode"
            )
            or ""
        )


        if not sku:

            return



        auction_code = (
            item.get(
                "auctionCode"
            )
        )



        price = (
            item.get(
                "price"
            )
        )


        try:

            if price is not None:

                price = float(
                    price
                )

        except:

            pass




        inventory = (
            meta
            .get(
                "inventory"
            )
            or {}
        )



        inventory_data = (
            inventory
            .get(
                sku
            )
            or {}
        )




        record = {


            # SKU

            "sku":
                sku,



            # auction

            "auctionCode":
                auction_code,



            # Product

            "title":
                item.get(
                    "itemName",
                    ""
                ),



            # Variant colour

            "variant":
                item.get(
                    "variant",
                    ""
                ),



            # Size

            "size":
                (
                    item.get(
                        "itemSize"
                    )
                    or
                    meta
                    .get(
                        "variantSizeBySku",
                        {}
                    )
                    .get(
                        sku
                    )
                    or
                    ""
                ),




            # Price

            "price":
                price,



            # Stock

            "quantity":
                (
                    inventory_data
                    .get(
                        "inventoryQuantity"
                    )
                    or
                    item.get(
                        "quantity"
                    )
                ),




            # Availability

            "available":
                inventory_data
                .get(
                    "availableForSale"
                ),



            # Media

            "img":
                meta.get(
                    "img"
                ),



            # URL

            "productUrl":
                meta.get(
                    "productUrl"
                )

        }



        by_sku[sku] = record



        if auction_code:

            by_auction[auction_code] = record





    # ---------------------------------------------------
    # Loop hours
    # ---------------------------------------------------

    for hour in hours:


        for auction in (
            hour.get(
                "auctions",
                []
            )
        ):



            shopify_data = (
                auction
                .get(
                    "shopifyData"
                )
                or {}
            )



            # -------------------------------------------
            # Images
            # -------------------------------------------

            image = None


            media = (
                shopify_data
                .get(
                    "productMedia"
                )
                or []
            )


            if media:


                first = media[0]


                if isinstance(
                    first,
                    dict
                ):

                    image = (
                        first.get(
                            "src"
                        )
                        or
                        first.get(
                            "url"
                        )
                    )


                else:

                    image = first





            # -------------------------------------------
            # Inventory extraction
            # -------------------------------------------

            inventory = {}



            raw_inventory = (
                auction
                .get(
                    "inventoryBySku"
                )
                or
                shopify_data
                .get(
                    "inventoryBySku"
                )
            )



            if raw_inventory:

                inventory = raw_inventory




            # -------------------------------------------
            # Variant size map
            # -------------------------------------------

            variant_size = {}




            # product form JSON sometimes contains this

            config = (
                auction
                .get(
                    "variantSizeBySku"
                )
                or {}
            )


            variant_size.update(
                config
            )




            meta = {


                "img":
                    image,


                "productUrl":
                    shopify_data
                    .get(
                        "productUrl"
                    ),


                "inventory":
                    inventory,


                "variantSizeBySku":
                    variant_size

            }




            # -------------------------------------------
            # Main SKU
            # -------------------------------------------

            add_sku(
                auction,
                meta
            )




            # -------------------------------------------
            # MLA child SKUs
            # -------------------------------------------

            for mla in (
                auction
                .get(
                    "mlauctions",
                    []
                )
            ):


                add_sku(
                    mla,
                    meta
                )



    return (

        by_auction,

        by_sku,

        "json"

    )




# -------------------------------------------------------
# SAVE MISSED SKU DATA
# -------------------------------------------------------

def merge_missed_into_state(
        air,
        by_sku,
        by_auction,
        timestamp
):


    for sku, hit in by_sku.items():


        if sku in air:

            continue



        air[sku] = {


            "sku":
                sku,


            "title":
                hit.get(
                    "title"
                ),



            "variant":
                hit.get(
                    "variant"
                ),



            "size":
                hit.get(
                    "size"
                ),



            "auctionCode":
                hit.get(
                    "auctionCode"
                ),



            "missedAt":
                timestamp,



            "missedPrice":
                hit.get(
                    "price"
                ),



            "missedImg":
                is_valid_image(
                    hit.get(
                        "img"
                    )
                ),



            "quantity":
                hit.get(
                    "quantity"
                ),



            "available":
                hit.get(
                    "available"
                ),



            "productUrl":
                hit.get(
                    "productUrl"
                ),



            "lastUpdated":
                timestamp,


            "source":
                "gha"

        }
# -------------------------------------------------------
# CSV EXPORT
# -------------------------------------------------------

def export_csv(state):


    os.makedirs(
        "data",
        exist_ok=True
    )


    with open(
        CSV_PATH,
        "w",
        newline="",
        encoding="utf-8"
    ) as f:


        writer = csv.writer(f)



        writer.writerow([


            "Date",
            "Time",

            "SKU",
            "Variant",
            "Size",

            "AuctionCode",

            "Title",

            "OnAir Price",
            "Missed Price",

            "Price Match",
            "Overcharged",

            "Image",

            "Missed Image",

            "Stock Qty",
            "Available",

            "Product URL",
            "404",

            "Buy",

            "Missed",

            "Delay(min)",

            "Source"


        ])




        records = sorted(

            state
            .get(
                "air",
                {}
            )
            .values(),

            key=lambda x:
                x.get(
                    "lastUpdated",
                    0
                ),

            reverse=True

        )




        for item in records:



            onair_price = (
                item.get(
                    "pdpPrice"
                )
            )


            missed_price = (
                item.get(
                    "missedPrice"
                )
            )



            parity = ""


            overcharged = ""



            try:


                if (

                    onair_price is not None

                    and

                    missed_price is not None

                ):



                    onair_price=float(
                        onair_price
                    )


                    missed_price=float(
                        missed_price
                    )



                    if abs(
                        onair_price -
                        missed_price
                    ) <= 0.01:


                        parity="MATCH"



                    else:

                        parity="MISMATCH"



                    if missed_price > onair_price:


                        overcharged="Y"



            except:

                pass




            writer.writerow([



                item.get(
                    "date",
                    ""
                ),


                item.get(
                    "time",
                    ""
                ),



                item.get(
                    "sku",
                    ""
                ),



                item.get(
                    "variant",
                    ""
                ),



                item.get(
                    "size",
                    ""
                ),



                item.get(
                    "auctionCode",
                    ""
                ),



                item.get(
                    "title",
                    ""
                ),



                onair_price,



                missed_price,



                parity,



                overcharged,



                "Y"
                if item.get(
                    "img"
                )
                else
                "N",



                "Y"
                if item.get(
                    "missedImg"
                )
                else
                "N",




                item.get(
                    "quantity",
                    ""
                ),



                item.get(
                    "available",
                    ""
                ),



                item.get(
                    "productUrl",
                    ""
                ),



                "Y"
                if item.get(
                    "productUrl404"
                )
                else
                "N",



                "Y"
                if item.get(
                    "buy"
                )
                else
                "N",



                "Y"
                if item.get(
                    "missedAt"
                )
                else
                "N",



                item.get(
                    "missedDelay",
                    ""
                ),



                item.get(
                    "source",
                    ""

                )

            ])






# -------------------------------------------------------
# MAIN
# -------------------------------------------------------

def main():


    state = load_state()


    air = state.setdefault(
        "air",
        {}
    )


    now = int(
        time.time()*1000
    )



    now_dt = datetime.now(
        UK_TZ
    )


    date = now_dt.strftime(
        "%Y-%m-%d"
    )


    clock = now_dt.strftime(
        "%H:%M:%S"
    )




    # -----------------------------------------------
    # FETCH
    # -----------------------------------------------


    w_status, w_body = fetch(
        WATCH_URL
    )


    api_status, api_body = fetch(
        ONAIR_API_URL
    )


    m_status, m_body = fetch(
        MISSED_URL
    )



    print(
        "watch:",
        w_status
    )

    print(
        "onair:",
        api_status
    )

    print(
        "missed:",
        m_status
    )




    # -----------------------------------------------
    # CURRENT AIR
    # -----------------------------------------------


    current = None



    if w_status == 200:


        current = extract_on_air(
            w_body
        )



    if current and current.get(
        "sku"
    ):


        sku = current["sku"]



        item = air.setdefault(
            sku,
            {}
        )



        item.update({


            "sku":
                sku,


            "title":
                current.get(
                    "title"
                ),



            "auctionCode":
                current.get(
                    "auctionCode"
                ),



            "pdpPrice":
                current.get(
                    "price"
                ),



            "img":
                is_valid_image(
                    current.get(
                        "img"
                    )
                ),



            "productUrl":
                current.get(
                    "productUrl"
                ),



            "buy":
                current.get(
                    "buy"
                ),



            "login":
                current.get(
                    "login"
                ),



            "date":
                date,


            "time":
                clock,


            "lastAir":
                now,


            "lastUpdated":
                now,


            "source":
                "gha"


        })



        print(
            "ON AIR:",
            sku
        )





    # -----------------------------------------------
    # MISSED AUCTIONS
    # -----------------------------------------------


    if m_status == 200:


        by_auction, by_sku, status = (
            extract_missed_grid(
                m_body
            )
        )



        print(
            "Missed SKU count:",
            len(by_sku)
        )



        for sku, hit in by_sku.items():



            if sku in air:

                item = air[sku]



                if not item.get(
                    "missedAt"
                ):


                    item.update({



                        "missedAt":
                            now,



                        "missedPrice":
                            hit.get(
                                "price"
                            ),



                        "missedImg":
                            is_valid_image(
                                hit.get(
                                    "img"
                                )
                            ),



                        "variant":
                            hit.get(
                                "variant"
                            ),



                        "size":
                            hit.get(
                                "size"
                            ),



                        "quantity":
                            hit.get(
                                "quantity"
                            ),



                        "available":
                            hit.get(
                                "available"
                            ),



                        "lastUpdated":
                            now



                    })



                    if item.get(
                        "lastAir"
                    ):


                        item["missedDelay"] = round(

                            (
                                now -
                                item["lastAir"]
                            )

                            /

                            60000

                        )




                    log_event(

                        f"RECENTLY ON AIR "
                        f"{sku} "
                        f"delay={item.get('missedDelay')}m"

                    )





            else:


                air[sku] = {


                    "sku":
                        sku,


                    "title":
                        hit.get(
                            "title"
                        ),



                    "variant":
                        hit.get(
                            "variant"
                        ),



                    "size":
                        hit.get(
                            "size"
                        ),



                    "auctionCode":
                        hit.get(
                            "auctionCode"
                        ),



                    "missedAt":
                        now,



                    "missedPrice":
                        hit.get(
                            "price"
                        ),



                    "missedImg":
                        is_valid_image(
                            hit.get(
                                "img"
                            )
                        ),



                    "quantity":
                        hit.get(
                            "quantity"
                        ),



                    "available":
                        hit.get(
                            "available"
                        ),



                    "productUrl":
                        hit.get(
                            "productUrl"
                        ),



                    "date":
                        date,


                    "time":
                        clock,


                    "lastUpdated":
                        now,


                    "source":
                        "gha"

                }







    # -----------------------------------------------
    # URL CHECK
    # -----------------------------------------------


    checked = run_product_url_checks(
        air
    )


    print(
        "URL checked:",
        checked
    )




    # -----------------------------------------------
    # SAVE
    # -----------------------------------------------


    save_state(
        state
    )


    export_csv(
        state
    )



    print(
        "Completed"
    )





if __name__ == "__main__":

    main()
