# AidMap: humanitarian needs and service coverage explorer

A static web map of public **health facilities** and **water points** (default region: South Sudan)
built from the Humanitarian Data Exchange (HDX). Every source shows its publisher, licence,
"data up to" date and data-quality warnings.

## How it works (and why it is light on your laptop)

```
GitHub Actions (free cloud machine, daily)        GitHub Pages (free hosting)
  pipeline/build_data.py  --->  site/data/*.json  --->  static site (Leaflet map)
  downloads latest from HDX                              runs in the visitor's browser
```

* Downloading and cleaning data happens on GitHub's servers, not on your laptop.
* The website is plain HTML/CSS/JS. No server, no database.
* You never need to install Python, Node or Git locally. Everything can be done in the browser.

## Deploy for free (no software on your laptop)

1. Create a GitHub account (free) and click **New repository**. Name it `aidmap`, set it **Public**
   (Pages and Actions are free for public repos), click **Create repository**.
2. Unzip this project. In the new repo click **Add file → Upload files** and drag in these items:
   `config.json`, `README.md`, `.gitignore`, and the folders `pipeline`, `site`, `tests`.
   Commit to the `main` branch.
3. The hidden `.github` folder often doesn't drag-and-drop. Instead click **Add file → Create new file**,
   type the name `.github/workflows/deploy.yml` (typing the slashes creates the folders), paste the
   contents of `.github/workflows/deploy.yml` from this project, and commit.
4. Go to **Settings → Pages → Build and deployment → Source: GitHub Actions**.
5. Go to the **Actions** tab → **Refresh data and deploy to GitHub Pages** → **Run workflow**.
   After 1 to 2 minutes your site is live at `https://<your-username>.github.io/aidmap/`.

From then on the workflow re-downloads the latest HDX data **every day** (00:30 UTC) and on every push.
Note: GitHub pauses scheduled workflows after 60 days without repo activity; just click Run workflow again.

## What "latest data" means here

AidMap fetches from HDX every day, but each dataset has its own publication date. A site refreshed today
can still show data that the publisher last updated years ago. That is why each source card shows
**Data up to**, **Updated on HDX**, and a recent/ageing/outdated badge, and the map shows a red banner
for data older than a year. Real source dates appear only after the first live run.

## Data-quality warnings and safeguards

Per record: `no_name`, `no_type`, `low_precision` (fewer than 3 decimals), `possible_duplicate`.
Records with zero/invalid/out-of-region coordinates are dropped and counted on the source card.

Safeguards (see `pipeline/build_data.py`):
* Only an allowlist of fields is published: name, type, status, category, source, reference ID.
  Phones, e-mails and contact people are discarded.
* Names containing phone numbers or e-mails are blanked.
* Records mentioning safe houses, survivor/GBV services, detention, police, military and similar
  sensitive terms are excluded entirely. Extend the `SENSITIVE` pattern if your region needs more.
* The nearest-service tool says plainly that distances are straight-line and that a blank area
  may mean missing data, not missing services.

Never add datasets about individuals, protection cases, or locations of vulnerable people without
proper authorisation. Check each dataset's licence and terms on its HDX page before reuse.

## Change region or datasets

Edit `config.json`: set `region.iso3`, `region.bbox` `[min_lat, min_lon, max_lat, max_lon]`, and per source
either a HDX dataset `id`/slug in `dataset`, or a `search` phrase plus `match_words` (the newest
matching dataset for that country is picked). Supported resource formats: CSV, GeoJSON/JSON, and ZIP
containing those. Shapefile-only datasets are not supported.

## Test locally (optional)

```
python -m unittest discover -s tests -v
python pipeline/build_data.py --sample     # offline synthetic demo data
cd site && python -m http.server 8000      # open http://localhost:8000
```

## Limits

* Does not know about road access, security, opening hours or stock levels.
* Quality flags detect formal problems, not whether a facility truly exists.
* Not for emergency dispatch or operational decisions without verification with local partners.

Code: free to use and adapt. Data: © respective publishers under their HDX licences. Map tiles © OpenStreetMap contributors.
