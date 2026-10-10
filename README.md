# Shopify App Pricing

Keeps a JSON snapshot of the pricing plans for every app listed on the
[Codebeans partner page](https://apps.shopify.com/partners/codebeans-private-limited3).

Shopify has no public API for App Store listings, so `scrape_pricing.py` reads the
public listing pages directly. It uses only the Python standard library (3.9+).

## Usage

```bash
python scrape_pricing.py
```

- Writes `data/pricing.json`, but only when something actually changed.
- Records each change, such as a price, plan, feature, trial or rating update, at the top of `data/CHANGELOG.md`.
- If any page fails to load or parse, it exits with code 1 and writes nothing, so the last good JSON stays in place.

Options:

| Flag | Purpose |
|---|---|
| `--partner URL` | Scrape a different partner page |
| `--out PATH` | Write the JSON somewhere else |
| `--force` | Rewrite the JSON even if nothing changed |

## Daily updates

`.github/workflows/daily-pricing.yml` runs the scraper every day at 03:30 UTC (09:00 IST)
and commits `data/` when it changed. You can also trigger it from the **Actions** tab.
The workflow needs **Settings → Actions → General → Workflow permissions → Read and write**.

## JSON shape

```jsonc
{
  "source": "https://apps.shopify.com/partners/codebeans-private-limited3",
  "updatedAt": "2026-10-10T05:31:20+00:00",   // when the data last changed
  "appCount": 18,
  "apps": [
    {
      "slug": "cb-gift-registry",
      "url": "https://apps.shopify.com/cb-gift-registry",
      "name": "Codebeans Gift Registry",
      "rating": 5.0,
      "reviewCount": 1,
      "currency": "USD",
      "plans": [
        {
          "name": "Basic",
          "priceText": "$9",          // exactly as shown on the listing
          "price": 9,                 // 0 for Free plans
          "interval": "month",        // null for Free plans
          "yearlyPrice": 90,          // only when a yearly option is shown
          "notes": ["or $90/year and save 17%"],  // extra lines under the price
          "usageCharges": [ ... ],    // only on usage-based plans (e.g. Notifylyst)
          "features": ["5 free registries", "..."],
          "trial": "3-day free trial",  // null when no trial
          "trialDays": 3
        }
      ]
    }
  ]
}
```

## If it breaks

The scraper depends on the App Store's HTML. If Shopify changes its layout, the run
fails with a message like `no pricing cards found (layout changed?)` and leaves the JSON
untouched. In that case the parsing in `parse_plan()` and `parse_app()` needs updating.
