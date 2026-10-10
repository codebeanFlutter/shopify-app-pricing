#!/usr/bin/env python3
"""Scrape the pricing plans of every app a Shopify partner lists on the App Store.

Reads the partner page, visits each app listing, and writes data/pricing.json.
The file is only rewritten when something actually changed, and each change is
appended to data/CHANGELOG.md. data/last-run.json is written on every run with
the time and outcome of the check.

Standard library only. Exit code is 0 on success, 1 on any failure (in which
case only last-run.json is written and the previous pricing JSON stays as it was).
"""

import argparse
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PARTNER = "https://apps.shopify.com/partners/codebeans-private-limited3"
ROOT = Path(__file__).resolve().parent
DEFAULT_OUT = ROOT / "data" / "pricing.json"
CHANGELOG = ROOT / "data" / "CHANGELOG.md"

USER_AGENT = "Mozilla/5.0 (compatible; codebeans-pricing-sync/1.0)"
REQUEST_DELAY = 1.0  # seconds between requests, to stay polite
RETRIES = 3


class ScrapeError(Exception):
    pass


# --------------------------------------------------------------------------- fetching

def fetch(url):
    last_error = None
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError) as e:
            last_error = e
            if attempt < RETRIES:
                time.sleep(2 * attempt)
    raise ScrapeError(f"could not fetch {url}: {last_error}")


# --------------------------------------------------------------------------- parsing

def text(fragment):
    """Strip tags and SVGs, return the non-empty text lines."""
    fragment = re.sub(r"<svg.*?</svg>", "", fragment, flags=re.S)
    fragment = re.sub(r"<[^>]+>", "\n", fragment)
    return [line for line in (html.unescape(l).strip() for l in fragment.split("\n")) if line]


def money(value):
    """'$1,499' / '1,499.00' -> 1499 (int when whole, else float)."""
    number = float(value.replace("$", "").replace(",", ""))
    return int(number) if number.is_integer() else number


def parse_partner_apps(page):
    slugs = re.findall(
        r'href="https://apps\.shopify\.com/([a-z0-9-]+)\?[^"]*surface_type=partners', page
    )
    unique = sorted(set(slugs))
    if not unique:
        raise ScrapeError("no apps found on the partner page (layout changed?)")
    return unique


def parse_plan(card):
    name_match = re.search(r'<p data-test-id="name"[^>]*>(.*?)</p>', card, re.S)
    if not name_match:
        raise ScrapeError("pricing card without a plan name")
    plan = {"name": " ".join(text(name_match.group(1)))}

    # Heading: price, interval, and optional notes such as a yearly option or usage notice.
    heading = card.split('data-pricing-component-target="cardHeadingPrice">', 1)[-1]
    lines = text(heading.split("</div>", 1)[0])

    price_text = lines[0] if lines else ""
    plan["priceText"] = price_text
    if price_text.lower() == "free":
        plan["price"] = 0
    elif re.fullmatch(r"\$[\d,]+(\.\d+)?", price_text):
        plan["price"] = money(price_text)
    else:
        plan["price"] = None

    plan["interval"] = None
    notes = []
    for line in lines[1:]:
        if line.startswith("/"):
            plan["interval"] = line.lstrip("/ ").strip()
            continue
        yearly = re.search(r"\$([\d,]+(?:\.\d+)?)\s*/\s*year", line)
        if yearly:
            plan["yearlyPrice"] = money(yearly.group(1))
        notes.append(line)
    if notes:
        plan["notes"] = notes

    # Usage-based plans carry their meter definition as a data attribute.
    usage = re.search(r'data-plan-usage-charges="([^"]*)"', card)
    if usage:
        plan["usageCharges"] = json.loads(html.unescape(usage.group(1)))
    base = re.search(r'data-plan-base-price="([^"]*)"', card)
    if base and plan["price"] is None:
        plan["price"] = money(base.group(1))

    features_block = re.search(r'data-test-id="features"[^>]*>(.*?)</ul>', card, re.S)
    plan["features"] = []
    if features_block:
        for item in re.findall(r"<li[^>]*>(.*?)(?=<li|$)", features_block.group(1), re.S):
            item_text = " ".join(text(item))
            if item_text:
                plan["features"].append(item_text)

    # Footer (e.g. "3-day free trial") sits right after the features list.
    after = card.split("</ul>", 1)[-1] if features_block else card
    footer = re.search(r'<p class="tw-text-label-sm[^"]*"[^>]*>(.*?)</p>', after, re.S)
    plan["trial"] = " ".join(text(footer.group(1))) if footer else None
    trial_days = re.search(r"(\d+)-day", plan["trial"] or "")
    plan["trialDays"] = int(trial_days.group(1)) if trial_days else 0
    return plan


def parse_app(slug, page):
    app = {"slug": slug, "url": f"https://apps.shopify.com/{slug}"}

    ld = re.search(r'<script type="application/ld\+json">(.*?)</script>', page, re.S)
    meta = json.loads(ld.group(1)) if ld else {}
    app["name"] = " ".join((meta.get("name") or "").split()) or slug
    rating = meta.get("aggregateRating") or {}
    app["rating"] = rating.get("ratingValue")
    app["reviewCount"] = rating.get("ratingCount", 0)

    currency = re.search(r"billed in ([A-Z]{3})", page)
    app["currency"] = currency.group(1) if currency else "USD"

    chunks = page.split("app-details-pricing-plan-card")[1:]
    if not chunks:
        raise ScrapeError(f"{slug}: no pricing cards found (layout changed?)")
    # Each chunk runs until the next card; the last one is cut at the billing note below the cards.
    app["plans"] = [
        parse_plan(chunk.split("tw-snap-center tw-snap-always", 1)[0].split("All charges are billed", 1)[0])
        for chunk in chunks
    ]
    return app


# --------------------------------------------------------------------------- diffing

def describe_changes(old_apps, new_apps):
    old = {a["slug"]: a for a in old_apps}
    new = {a["slug"]: a for a in new_apps}
    changes = []
    for slug in sorted(new.keys() - old.keys()):
        changes.append(f"New app: {new[slug]['name']} ({len(new[slug]['plans'])} plans)")
    for slug in sorted(old.keys() - new.keys()):
        changes.append(f"App removed from partner page: {old[slug]['name']}")
    for slug in sorted(old.keys() & new.keys()):
        a, b = old[slug], new[slug]
        if a == b:
            continue
        label = b["name"]
        for field in ("name", "rating", "reviewCount", "currency"):
            if a.get(field) != b.get(field):
                changes.append(f"{label}: {field} {a.get(field)} -> {b.get(field)}")
        old_plans = {p["name"]: p for p in a["plans"]}
        new_plans = {p["name"]: p for p in b["plans"]}
        for name in new_plans.keys() - old_plans.keys():
            changes.append(f"{label}: new plan '{name}' ({new_plans[name]['priceText']})")
        for name in old_plans.keys() - new_plans.keys():
            changes.append(f"{label}: plan removed '{name}'")
        for name in old_plans.keys() & new_plans.keys():
            p, q = old_plans[name], new_plans[name]
            for field in ("priceText", "interval", "yearlyPrice", "trial", "notes", "usageCharges"):
                if p.get(field) != q.get(field):
                    changes.append(f"{label} / {name}: {field} {p.get(field)!r} -> {q.get(field)!r}")
            for feature in q["features"]:
                if feature not in p["features"]:
                    changes.append(f"{label} / {name}: + feature '{feature}'")
            for feature in p["features"]:
                if feature not in q["features"]:
                    changes.append(f"{label} / {name}: - feature '{feature}'")
        old_order = [p["name"] for p in a["plans"]]
        new_order = [p["name"] for p in b["plans"]]
        if old_order != new_order and set(old_order) == set(new_order):
            changes.append(f"{label}: plan order changed")
    return changes


# --------------------------------------------------------------------------- main

def write_last_run(path, info):
    """Written on every run, so the daily commit keeps the repo active and shows the last check."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--partner", default=DEFAULT_PARTNER, help="partner page URL")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output JSON path")
    parser.add_argument("--force", action="store_true", help="rewrite the JSON even if nothing changed")
    args = parser.parse_args()

    run_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    last_run_path = args.out.parent / "last-run.json"

    try:
        slugs = parse_partner_apps(fetch(args.partner))
        print(f"Found {len(slugs)} apps on {args.partner}")
        apps = []
        for slug in slugs:
            time.sleep(REQUEST_DELAY)
            app = parse_app(slug, fetch(f"https://apps.shopify.com/{slug}"))
            print(f"  {slug}: {len(app['plans'])} plans")
            apps.append(app)
    except ScrapeError as e:
        print(f"ERROR: {e}. Pricing data was not touched.", file=sys.stderr)
        write_last_run(last_run_path, {"lastRunAt": run_at, "status": "failed", "error": str(e)})
        return 1

    previous = {}
    if args.out.exists():
        previous = json.loads(args.out.read_text(encoding="utf-8"))
    changes = describe_changes(previous.get("apps", []), apps)

    write_last_run(last_run_path, {
        "lastRunAt": run_at,
        "status": "success",
        "appCount": len(apps),
        "planCount": sum(len(a["plans"]) for a in apps),
        "changed": bool(changes),
        "changeCount": len(changes),
        "dataUpdatedAt": run_at if changes or args.force else previous.get("updatedAt"),
    })

    if not changes and not args.force:
        print("No changes since last run.")
        return 0

    now = run_at
    data = {
        "source": args.partner,
        "updatedAt": now,
        "appCount": len(apps),
        "apps": apps,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if changes and args.out.resolve() == DEFAULT_OUT:
        heading = f"## {now}\n\n" + "\n".join(f"- {c}" for c in changes) + "\n\n"
        existing = CHANGELOG.read_text(encoding="utf-8") if CHANGELOG.exists() else "# Pricing changes\n\n"
        title, _, rest = existing.partition("\n\n")
        CHANGELOG.write_text(f"{title}\n\n{heading}{rest}", encoding="utf-8")

    print(f"Wrote {args.out} ({len(changes)} change(s)):")
    for c in changes:
        print(f"  - {c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
