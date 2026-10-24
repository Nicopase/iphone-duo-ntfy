#!/usr/bin/env python3
"""
Free Apple Store pickup tracker (built for iPhone Duo, works for any Apple part number).

  python3 duo_tracker.py discover            # list iPhone Duo part numbers (color/storage)
  python3 duo_tracker.py check               # check every watch in watches.json once
  python3 duo_tracker.py watch               # keep checking on a loop, alert on restock

Needs one package:  pip install curl_cffi
(Apple's bot protection rejects Python's built-in HTTP client with error 541;
curl_cffi makes requests look like Chrome.)
Alerts go to your phone via ntfy.sh (free app, no account).
"""
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("WATCHES_FILE", os.path.join(HERE, "watches.json"))
STATE_PATH = os.path.join(HERE, ".tracker_state.json")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"),
    "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Apple storefront path prefix by country. US has none.
STOREFRONTS = {
    "us": "", "ca": "/ca", "uk": "/uk", "au": "/au", "de": "/de", "fr": "/fr",
    "jp": "/jp", "ie": "/ie", "nz": "/nz", "sg": "/sg", "hk": "/hk/en",
    "it": "/it", "es": "/es", "nl": "/nl", "mx": "/mx",
}


# ---------------------------------------------------------------- helpers
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


class Blocked(Exception):
    """Apple refused the request (bot protection or rate limit)."""


try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    cffi_requests = None

IMPERSONATE = os.environ.get("IMPERSONATE", "chrome")
_session = None


def session():
    global _session
    if _session is None:
        if cffi_requests is None:
            sys.exit("Missing package. Run:  pip install curl_cffi")
        _session = cffi_requests.Session(impersonate=IMPERSONATE)
    return _session


def http_get(url, timeout=25, referer=None, expect_json=False):
    headers = {"Accept-Language": "en-US,en;q=0.9"}
    if referer:
        headers["Referer"] = referer
    if expect_json:
        headers["Accept"] = "application/json, text/plain, */*"
        headers["X-Requested-With"] = "XMLHttpRequest"
    r = session().get(url, headers=headers, timeout=timeout)
    if r.status_code in (403, 429, 541):
        raise Blocked(f"HTTP {r.status_code}")
    r.raise_for_status()
    text = r.text
    if expect_json:
        if text.lstrip().startswith("<"):
            raise Blocked("got an HTML page instead of data")
        return json.loads(text)
    return text


_warmed = set()


def warm_up(country, slug):
    """Visit the product page once so Apple sets its normal store cookies."""
    if country in _warmed:
        return
    try:
        http_get(f"{base_url(country)}/shop/buy-iphone/{slug}")
    except Exception as e:
        log(f"warm-up skipped ({e})")
    _warmed.add(country)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def base_url(country):
    prefix = STOREFRONTS.get(country.lower())
    if prefix is None:
        prefix = "/" + country.lower()
    return f"https://www.apple.com{prefix}"


# ---------------------------------------------------------------- discover
def discover(country="us", slug="iphone-duo"):
    """Scrape Apple's buy page for part numbers and their color/storage/carrier."""
    url = f"{base_url(country)}/shop/buy-iphone/{slug}"
    log(f"Fetching {url}")
    html = http_get(url)

    found = {}
    # Flat JSON objects that mention a partNumber (Apple embeds these in the page).
    for m in re.finditer(r'\{[^{}]*"partNumber"\s*:\s*"([A-Z0-9]+/[A-Z])"[^{}]*\}', html):
        part = m.group(1)
        try:
            obj = json.loads(m.group(0))
        except ValueError:
            obj = {}
        info = {k: v for k, v in obj.items()
                if isinstance(v, str) and re.search(r"color|capacity|carrier|model", k, re.I)}
        found.setdefault(part, {}).update(info)

    # Fallback: any bare part number pattern.
    if not found:
        for part in set(re.findall(r'\b(M[A-Z0-9]{3,4}[0-9]?(?:LL|ZA|ZP|B|C|D|F|J|X)/A)\b', html)):
            found[part] = {}

    if not found:
        print("No part numbers found. Apple may have changed the page — see README for "
              "the manual way to find a part number.")
        return

    print(f"\nFound {len(found)} part numbers:\n")
    for part, info in sorted(found.items()):
        details = ", ".join(f"{k}={v}" for k, v in info.items()) or "(details not in page)"
        print(f"  {part:<12} {details}")
    print("\nCopy the ones you want into watches.json.")


# ---------------------------------------------------------------- checking
_endpoint_pref = {}


def fetch_availability(country, location, parts, slug="iphone-duo"):
    """One request to Apple for several parts near one location.

    Tries Apple's current endpoint (/shop/retail/pickup-message) first and falls
    back to the older /shop/fulfillment-messages, remembering whichever works.
    """
    warm_up(country, slug)
    params = {"pl": "true", "mts.0": "regular", "searchNearby": "true", "location": location}
    for i, p in enumerate(parts):
        params[f"parts.{i}"] = p
    qs = urllib.parse.urlencode(params)
    referer = f"{base_url(country)}/shop/buy-iphone/{slug}"

    endpoints = ["/shop/retail/pickup-message", "/shop/fulfillment-messages"]
    if _endpoint_pref.get(country) in endpoints:
        endpoints.remove(_endpoint_pref[country])
        endpoints.insert(0, _endpoint_pref[country])

    last_err = None
    for ep in endpoints:
        try:
            data = http_get(f"{base_url(country)}{ep}?{qs}", referer=referer, expect_json=True)
        except Blocked as e:
            last_err = e
            continue
        body = data.get("body", {}) or {}
        stores = (body.get("stores")
                  or body.get("content", {}).get("pickupMessage", {}).get("stores")
                  or [])
        _endpoint_pref[country] = ep
        return stores
    raise Blocked(str(last_err))


def parse_stores(stores, parts, radius_miles=None, store_filter=None):
    """Return list of hits: dicts with part, store, quote, distance, available."""
    results = []
    for s in stores:
        name = s.get("storeName", "?")
        if store_filter and not any(f.lower() in name.lower() for f in store_filter):
            continue
        dist_txt = s.get("storeDistanceWithUnit") or ""
        dist = None
        m = re.match(r"([\d.]+)", dist_txt.replace(",", ""))
        if m:
            dist = float(m.group(1))
        if radius_miles and dist is not None and dist > radius_miles:
            continue
        for part in parts:
            pa = (s.get("partsAvailability") or {}).get(part)
            if not pa:
                continue
            title = (pa.get("messageTypes", {}).get("regular", {})
                       .get("storePickupProductTitle")) or part
            results.append({
                "part": part,
                "title": title,
                "store": name,
                "store_id": s.get("storeNumber", name),
                "city": s.get("city", ""),
                "distance": dist_txt,
                "quote": pa.get("pickupSearchQuote", ""),
                "available": pa.get("pickupDisplay") == "available",
            })
    return results


def notify(cfg, title, body, url=None, quiet=False):
    n = cfg.get("notify", {})
    topic = n.get("ntfy_topic") or os.environ.get("NTFY_TOPIC")
    if topic:
        server = n.get("ntfy_server", "https://ntfy.sh").rstrip("/")
        req = urllib.request.Request(
            f"{server}/{topic}", data=body.encode("utf-8"), method="POST",
            headers={"Title": title.encode("ascii", "ignore").decode(),
                     "Priority": "low" if quiet else "urgent",
                     "Tags": "hourglass" if quiet else "iphone,rotating_light",
                     **({"Click": url} if url else {})})
        try:
            urllib.request.urlopen(req, timeout=15).read()
        except Exception as e:
            log(f"ntfy failed: {e}")
    # Desktop notification on macOS, best effort.
    if n.get("desktop", True) and sys.platform == "darwin":
        safe = body.replace('"', "'")[:200]
        os.system(f'osascript -e \'display notification "{safe}" with title "{title}" sound name "Glass"\'')
    print("\a", end="", flush=True)


def run_check(cfg, state):
    """Check everything once. Alerts only on unavailable -> available changes."""
    watches = cfg.get("watches", [])
    country_default = cfg.get("country", "us")

    # Group watches by (country, location) so each ZIP is one request.
    groups = defaultdict(list)
    for w in watches:
        if w.get("enabled", True) is False:
            continue
        if not w.get("parts"):
            w["parts"] = cfg.get("parts", [])  # fall back to the shared list
        if not w["parts"]:
            continue
        groups[(w.get("country", country_default), str(w["location"]))].append(w)

    any_available = False
    any_blocked = False
    stores_seen = set()
    for (country, location), ws in groups.items():
        parts = sorted({p for w in ws for p in w["parts"]})
        try:
            stores = fetch_availability(country, location, parts, cfg.get("product_slug", "iphone-duo"))
        except Blocked as e:
            log(f"{location}: BLOCKED by Apple ({e}). Not the same as out of stock — "
                f"try again later, or run: pip install -U curl_cffi")
            any_blocked = True
            continue
        except Exception as e:
            log(f"{location}: request failed ({e})")
            continue

        for w in ws:
            label = w.get("name", ", ".join(w["parts"]))
            hits = parse_stores(stores, w["parts"], w.get("radius_miles"), w.get("stores"))
            fake = None
            if os.environ.get("FAKE_IN_STOCK") and hits and not state.get("_fake_sent"):
                fake = hits[0]  # pretend the first result is in stock, once
                fake["available"] = True
                fake["title"] = "[TEST] " + fake["title"]
                state["_fake_sent"] = True
            avail = [h for h in hits if h["available"]]
            if avail:
                any_available = True
            log(f"{label} @ {location}: {len(avail)} of {len(hits)} store/config combos available")

            for h in hits:
                stores_seen.add(h["store_id"])
                key = f"{country}|{h['part']}|{h['store_id']}"
                if h is fake:
                    was = False  # don't read or save real history for the fake hit
                else:
                    was = state.get(key, False)
                    state[key] = h["available"]
                mark = "✅" if h["available"] else "  "
                print(f"     {mark} {h['store']:<22} {h['distance']:<9} {h['quote']}  [{h['part']}]")
                if h["available"] and not was:
                    buy = f"{base_url(country)}/shop/buy-iphone/{cfg.get('product_slug', 'iphone-duo')}"
                    notify(cfg,
                           f"In stock: {h['store']}",
                           f"{h['title']}\n{h['quote']} at {h['store']} ({h['city']}, {h['distance']})",
                           buy)
    if any_blocked:
        print("::warning::Apple blocked one or more checks this run")
    no_stock_summary(cfg, state, any_available, any_blocked, len(stores_seen))
    return any_available


HEARTBEAT_SECONDS = {"every_run": 0, "hourly": 3600, "daily": 86400}


def no_stock_summary(cfg, state, any_available, any_blocked, n_stores):
    """Optional quiet 'nothing yet' message, set by "no_stock_alert" in watches.json."""
    mode = str(cfg.get("no_stock_alert", "hourly")).lower()
    if mode not in HEARTBEAT_SECONDS or any_available:
        return
    now = time.time()
    last = state.get("_last_no_stock_alert", 0)
    # Allow a few minutes of slack so a late GitHub run still counts as "hourly".
    if now - last < HEARTBEAT_SECONDS[mode] - 300:
        return
    if any_blocked and n_stores == 0:
        body = "Apple blocked this check. The tracker will keep trying."
    else:
        body = f"Checked {n_stores} stores. No iPhone Duo in stock yet."
    notify(cfg, "Still checking", body, quiet=True)
    state["_last_no_stock_alert"] = now


# ---------------------------------------------------------------- main
def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "watch"
    cfg = load_json(CONFIG_PATH, None)

    if cmd == "discover":
        country = sys.argv[2] if len(sys.argv) > 2 else (cfg or {}).get("country", "us")
        discover(country, (cfg or {}).get("product_slug", "iphone-duo"))
        return

    if cfg is None:
        sys.exit(f"Missing {CONFIG_PATH}. Copy the example watches.json next to this script.")

    state = load_json(STATE_PATH, {})

    if cmd == "test-alert":
        notify(cfg, "Tracker test", "If you see this, alerts are working.")
        log("Sent test alert.")
        return

    if cmd == "check":
        # Optional: check N times, S seconds apart, e.g.  check 4 60
        repeat = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        every = max(30, int(sys.argv[3])) if len(sys.argv) > 3 else 60
        for i in range(repeat):
            if i:
                time.sleep(every)
            run_check(cfg, state)
            save_json(STATE_PATH, state)
        return

    if cmd == "watch":
        interval = max(60, int(cfg.get("interval_seconds", 120)))  # be polite to Apple
        log(f"Watching {len(cfg.get('watches', []))} watches every ~{interval}s. Ctrl+C to stop.")
        while True:
            run_check(cfg, state)
            save_json(STATE_PATH, state)
            time.sleep(interval + random.randint(0, 20))
        return

    sys.exit(f"Unknown command '{cmd}'. Use: discover | check | watch | test-alert")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
