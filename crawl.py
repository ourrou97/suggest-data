"""Crawl of Redbubble's public search suggestions, meant to run on a GitHub Actions runner.
Real call used by the site's search box: GET /typeahead/?term=<text>&locale=en
Rules: a list shorter than 20 = branch exhausted; a full list (20) = expand it.
State lives in data/state.json (committed by the workflow), results in data/phrases.csv.
Every first-seen phrase keeps its date, so a phrase that appears later can be told apart as new."""
import csv
import json
import os
import random
import string
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from DrissionPage import ChromiumOptions, ChromiumPage

DATA = Path(os.getenv("DATA_DIR", "data"))
MAX_REQ = int(os.getenv("MAX_REQUESTS", "250"))
GAP = float(os.getenv("GAP_SECONDS", "8"))
CAP = 20
MAX_WORDS = 4
STATE = DATA / "state.json"
DATA.mkdir(parents=True, exist_ok=True)


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"queue": [[1, 0, c] for c in string.ascii_lowercase], "done": {}, "phrases": {}, "runs": []}


def save(st):
    STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")


def make_page():
    co = ChromiumOptions()
    co.auto_port()
    co.headless(True)
    for arg in ("--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"):
        co.set_argument(arg)
    co.set_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
    return ChromiumPage(co)


def wait_cloudflare(page):
    for _ in range(10):
        t = (page.title or "").lower()
        if "instant" not in t and "moment" not in t and "cloudflare" not in t:
            return True
        time.sleep(1.5)
    return False


def visitor_country(page):
    """The country Redbubble sees for this runner (written to the log, not stored)."""
    page.get("https://www.redbubble.com/")
    if not wait_cloudflare(page):
        return None
    el = page.ele("@id=__NEXT_DATA__")
    if not el:
        return None
    try:
        return json.loads(el.raw_text or el.inner_html)["props"]["pageProps"]["userInfo"]["country"]
    except Exception:  # noqa: BLE001
        return None


def fetch(page, term):
    page.get(f"https://www.redbubble.com/typeahead/?term={quote(term)}&locale=en")
    if not wait_cloudflare(page):
        raise RuntimeError("cloudflare challenge")
    text = page.run_js("return document.body.innerText")
    data = json.loads(text).get("data") or {}

    def labels(key):
        return [(x.get("keywords") or x.get("label") or "").lower().strip() for x in data.get(key) or []]

    return [p for p in labels("popular_searches_v2") if p], [t for t in labels("trending_searches") if t]


def main():
    st = load()
    day = today()
    page = make_page()
    made, last_lists = 0, []
    try:
        print("country seen by Redbubble:", visitor_country(page), flush=True)
        while st["queue"] and made < MAX_REQ:
            st["queue"].sort(key=lambda z: (z[0], z[1], random.random()))
            depth, kind, term = st["queue"].pop(0)
            if term in st["done"]:
                continue
            try:
                pop, trend = fetch(page, term)
            except Exception as exc:  # noqa: BLE001 - stop at the first refusal, keep progress
                st["queue"].append([depth, kind, term])
                print("stop:", str(exc)[:100], flush=True)
                break
            made += 1
            last_lists.append(tuple(pop))
            if len(last_lists) >= 3 and pop and len(set(last_lists[-3:])) == 1:
                print("silent fallback (same list for 3 queries) - stopping", flush=True)
                break
            st["done"][term] = len(pop)
            new = 0
            for rank, p in enumerate(pop):
                if p not in st["phrases"]:
                    st["phrases"][p] = {"kind": "pop", "first": day, "last": day, "n": 1, "rank": rank, "from": term}
                    new += 1
                    if len(p.split()) < MAX_WORDS and p not in st["done"]:
                        st["queue"].append([depth + 1, 1, p])
                else:
                    st["phrases"][p]["last"], st["phrases"][p]["n"] = day, st["phrases"][p]["n"] + 1
            for rank, p in enumerate(trend):
                if p not in st["phrases"]:
                    st["phrases"][p] = {"kind": "trend", "first": day, "last": day, "n": 1, "rank": rank, "from": term}
                    new += 1
                else:
                    st["phrases"][p]["last"], st["phrases"][p]["n"] = day, st["phrases"][p]["n"] + 1
            if len(pop) >= CAP and " " not in term and len(term) < 6:
                for c in string.ascii_lowercase:
                    if term + c not in st["done"]:
                        st["queue"].append([depth + 1, 0, term + c])
            print(f"{made} {term!r}: {len(pop)} suggestions, new {new}, total {len(st['phrases'])}, queue {len(st['queue'])}", flush=True)
            time.sleep(GAP + random.uniform(0, 3))
    finally:
        page.quit()
        st["runs"].append([day, made, len(st["phrases"])])
        st["runs"] = st["runs"][-200:]
        save(st)
        with (DATA / "phrases.csv").open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["phrase", "words", "kind", "first_seen", "last_seen", "times_seen", "rank", "from_query"])
            for p, m in st["phrases"].items():
                w.writerow([p, len(p.split()), m["kind"], m["first"], m["last"], m["n"], m["rank"], m["from"]])
    print(f"done: {made} requests, {len(st['phrases'])} phrases, {len(st['queue'])} queued", flush=True)
    return 0 if made else 1


if __name__ == "__main__":
    sys.exit(main())
