#!/usr/bin/env python3
"""AidMap data pipeline.

Fetches public datasets from the Humanitarian Data Exchange (HDX, CKAN API),
cleans them, attaches data-quality flags and source/licence/date metadata,
and writes two static files used by the website:

    site/data/aid_points.geojson
    site/data/metadata.json

Standard library only (no pip install needed).

Usage:
    python pipeline/build_data.py            # live data from HDX
    python pipeline/build_data.py --sample   # synthetic demo data (offline)
"""
import argparse
import csv
import io
import json
import math
import random
import re
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HDX_API = "https://data.humdata.org/api/action/"
USER_AGENT = "AidMap/1.0 (open-source humanitarian data explorer)"
MAX_BYTES = 150 * 1024 * 1024

# ---------------------------------------------------------------- safeguards
# Records whose text mentions any of these are NOT published (conservative).
SENSITIVE = re.compile(
    r"safe[\s_-]?house|safe[\s_-]?space|shelter for (women|girls|survivors|children)"
    r"|\bgbv\b|gender[\s_-]?based|survivor|refuge for|protection (site|cent(re|er))"
    r"|detention|prison|police|military|barracks|\barmy\b|\bsspdf\b|\bspla\b|armed"
    r"|checkpoint|hideout|orphan|child[\s_-]?friendly space",
    re.I,
)
# Only these fields are ever published. Everything else (phones, e-mails,
# contact persons, notes ...) is discarded at this point.
PUBLISHED_FIELDS = ("name", "kind", "category", "status", "flags", "quality", "src", "ref")

LAT_KEYS = ("lat", "latitude", "y", "lat_y", "y_coord", "ycoord", "gps_lat", "latitude_dd")
LON_KEYS = ("lon", "long", "lng", "longitude", "x", "lon_x", "x_coord", "xcoord", "gps_lon", "longitude_dd")
NAME_KEYS = ("name", "name:en", "facility_name", "facilityname", "hf_name", "health_facility",
             "facility", "site_name", "sitename", "wp_name", "water_point_name",
             "waterpoint_name", "official_name", "title")
KIND_KEYS = ("amenity", "healthcare", "facility_type", "facilitytype", "nature_of_facility",
             "type", "category", "wp_type", "water_point_type", "water_source",
             "source_type", "technology")
STATUS_KEYS = ("status", "functional", "functionality", "wp_status", "condition",
               "operational_status", "status_clean", "operating_status")
ID_KEYS = ("osm_id", "@id", "facility_id", "wp_id", "uuid", "ref", "code", "id")

HEALTH_KINDS = [
    (r"hospital", "Hospital"),
    (r"phcc|primary health care cent|health cent", "Primary health care centre"),
    (r"phcu|primary health care unit|health unit", "Primary health care unit"),
    (r"dispensar", "Dispensary"),
    (r"mobile", "Mobile clinic"),
    (r"clinic", "Clinic"),
    (r"pharmac", "Pharmacy"),
    (r"doctor", "Doctors"),
    (r"dentist|dental", "Dentist"),
    (r"laborator", "Laboratory"),
    (r"nutrition|feeding", "Nutrition site"),
]
WATER_KINDS = [
    (r"bore ?hole", "Borehole"),
    (r"hand ?pump", "Handpump"),
    (r"\bwells?\b", "Well"),
    (r"spring", "Spring"),
    (r"\btap\b|kiosk|standpipe", "Tap / kiosk"),
    (r"tank|reservoir|tower", "Tank / reservoir"),
    (r"rain", "Rainwater harvesting"),
    (r"\bdam\b|hafir|pond", "Dam / hafir / pond"),
    (r"river|stream|lake", "Surface water"),
]
DEFAULT_KIND = {"health": "Health facility", "water": "Water point"}
KIND_TABLES = {"health": HEALTH_KINDS, "water": WATER_KINDS}


# ---------------------------------------------------------------- networking
def http_get(url, timeout=90, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ValueError("file larger than safety limit")
            return data
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET failed for {url}: {last}")


def ckan(action, **params):
    url = HDX_API + action + "?" + urllib.parse.urlencode(params)
    body = json.loads(http_get(url).decode("utf-8"))
    if not body.get("success"):
        raise RuntimeError(f"HDX API error for {action}: {body.get('error')}")
    return body["result"]


def resolve_dataset(src, region):
    """Return the HDX package dict for a source (by id, else by search)."""
    if src.get("dataset"):
        try:
            return ckan("package_show", id=src["dataset"])
        except Exception as e:  # noqa: BLE001
            print(f"  ! package_show failed for {src['dataset']}: {e}; falling back to search")
    res = ckan("package_search", q=src["search"], fq=f"groups:{region['iso3'].lower()}",
               rows=10, sort="metadata_modified desc")
    words = [w.lower() for w in src.get("match_words", [])]
    best, best_score = None, 0
    for pkg in res.get("results", []):
        title = (pkg.get("title") or "").lower()
        score = sum(1 for w in words if w in title)
        if score > best_score:  # ties keep the most recently modified (list order)
            best, best_score = pkg, score
    if not best:
        raise RuntimeError(f"no HDX dataset found for query '{src['search']}'")
    return ckan("package_show", id=best["id"])


# ---------------------------------------------------------------- parsing
def norm_fmt(fmt):
    f = (fmt or "").lower()
    if "shp" in f or "shape" in f:
        return "shp"
    if "geojson" in f:
        return "geojson"
    if "zip" in f:
        return "zip"
    if "csv" in f:
        return "csv"
    if f == "json":
        return "json"
    return f


def to_float(v):
    try:
        x = float(str(v).strip())
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def decimals(v):
    s = str(v).strip().lower()
    if "e" in s:
        return None
    return len(s.split(".")[1]) if "." in s else 0


def lower_keys(d):
    return {str(k).strip().lower(): v for k, v in (d or {}).items()}


def parse_csv(data):
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        return []
    cols = list(lower_keys(rows[0]).keys())
    lat_k = next((k for k in LAT_KEYS if k in cols), None)
    lon_k = next((k for k in LON_KEYS if k in cols), None)
    if not lat_k or not lon_k:
        raise ValueError(f"no latitude/longitude columns in CSV (columns: {cols[:12]})")
    out = []
    for raw in rows:
        r = lower_keys(raw)
        vals = [str(v).strip() for v in r.values() if v not in (None, "")]
        if vals and all(v.startswith("#") for v in vals):  # HXL tag row
            continue
        out.append({"lat": to_float(r.get(lat_k)), "lon": to_float(r.get(lon_k)),
                    "lat_dec": decimals(r.get(lat_k)), "lon_dec": decimals(r.get(lon_k)),
                    "props": r})
    return out


def _centroid(geom):
    t, c = geom.get("type"), geom.get("coordinates")
    if t == "Point":
        return c[0], c[1]
    ring = None
    if t == "Polygon":
        ring = c[0]
    elif t == "MultiPolygon":
        ring = c[0][0]
    if ring:
        return sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)
    return None


def parse_geojson(data):
    gj = json.loads(data.decode("utf-8-sig"))
    feats = gj.get("features", []) if isinstance(gj, dict) else []
    out = []
    for f in feats:
        geom = f.get("geometry") or {}
        cen = _centroid(geom) if geom else None
        if not cen:
            continue
        out.append({"lon": to_float(cen[0]), "lat": to_float(cen[1]),
                    "lon_dec": decimals(cen[0]), "lat_dec": decimals(cen[1]),
                    "props": lower_keys(f.get("properties"))})
    return out


def parse_zip(data):
    out, seen = [], []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            low = name.lower()
            if low.startswith("__macosx") or low.endswith("/"):
                continue
            seen.append(low)
            if low.endswith((".geojson", ".json")):
                out += parse_geojson(z.read(name))
            elif low.endswith(".csv"):
                out += parse_csv(z.read(name))
    if not out:
        raise ValueError(f"zip has no readable CSV/GeoJSON (files: {seen[:6]})")
    return out


def parse_bytes(fmt, data):
    if fmt in ("geojson", "json"):
        return parse_geojson(data)
    if fmt == "csv":
        return parse_csv(data)
    if fmt == "zip":
        return parse_zip(data)
    raise ValueError(f"unsupported format {fmt}")


def load_records(pkg, src):
    prefer = src.get("prefer_formats", ["geojson", "csv", "json", "zip"])
    cands = []
    for r in pkg.get("resources", []):
        f = norm_fmt(r.get("format"))
        if f in prefer and (r.get("download_url") or r.get("url")):
            cands.append((prefer.index(f), f, r))
    cands.sort(key=lambda c: c[0])
    errors = []
    for _, fmt, res in cands:
        url = res.get("download_url") or res.get("url")
        try:
            print(f"  downloading {res.get('name')} [{fmt}]")
            recs = parse_bytes(fmt, http_get(url))
            if recs:
                return recs, res
            errors.append(f"{res.get('name')}: no points")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{res.get('name')}: {e}")
    raise RuntimeError("no usable resource (" + "; ".join(errors) + ")" if errors
                       else "dataset has no CSV/GeoJSON/JSON/ZIP resource (shapefile-only is not supported)")


# ---------------------------------------------------------------- cleaning
EMPTY = {"", "null", "none", "nan", "n/a", "na", "-", "unknown"}


def first_val(props, keys):
    for k in keys:
        v = props.get(k)
        if v is not None and str(v).strip().lower() not in EMPTY:
            return str(v).strip()
    return ""


def clean_name(name):
    name = re.sub(r"[\x00-\x1f]", " ", name).strip()
    # privacy: never publish names that contain e-mail addresses or phone-like numbers
    if re.search(r"@|\+?\d[\d\s-]{7,}", name):
        return ""
    return name[:120]


def classify(category, raw_kind, name):
    text = f"{raw_kind} {name}".lower()
    for pattern, label in KIND_TABLES.get(category, []):
        if re.search(pattern, text):
            return label
    return DEFAULT_KIND.get(category, "Service point")


def norm_status(raw):
    s = (raw or "").strip().lower()
    if not s:
        return "Unknown"
    if re.search(r"non|not|broken|damag|closed|abandon|dry|out of|inactive|^(no|false|0)$", s):
        return "Not functional / closed"
    if re.search(r"function|work|operat|open|active|^(yes|true|1)$", s):
        return "Functional / open"
    return "Unknown"


def norm_key(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def process_source(src, records, region):
    """Return (features, stats) for one source. Pure function (easy to test)."""
    min_lat, min_lon, max_lat, max_lon = region["bbox"]
    stats, feats, seen = Counter(), [], set()
    for rec in records:
        stats["raw"] += 1
        lat, lon = rec.get("lat"), rec.get("lon")
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            stats["dropped_bad_coordinates"] += 1
            continue
        if abs(lat) < 1e-6 and abs(lon) < 1e-6:
            stats["dropped_zero_coordinates"] += 1
            continue
        if not (min_lat <= lat <= max_lat and min_lon <= lon <= max_lon):
            stats["dropped_outside_region"] += 1
            continue
        props = rec.get("props", {})
        blob = " ".join(str(v) for v in props.values() if v is not None)
        if SENSITIVE.search(blob):
            stats["dropped_sensitive"] += 1
            continue

        name = clean_name(first_val(props, NAME_KEYS))
        raw_kind = first_val(props, KIND_KEYS)
        kind = classify(src["category"], raw_kind, name)
        status = norm_status(first_val(props, STATUS_KEYS))
        ref = first_val(props, ID_KEYS)[:40]

        flags = []
        if not name:
            flags.append("no_name")
        if not raw_kind:
            flags.append("no_type")
        dec = [d for d in (rec.get("lat_dec"), rec.get("lon_dec")) if d is not None]
        if dec and min(dec) < 3:
            flags.append("low_precision")
        if name:
            key = (round(lat, 3), round(lon, 3), norm_key(name))
            if key in seen:
                flags.append("possible_duplicate")
            seen.add(key)

        score = 100 - 30 * ("no_name" in flags) - 15 * ("no_type" in flags) \
            - 20 * ("low_precision" in flags) - 25 * ("possible_duplicate" in flags)
        quality = "good" if score >= 85 else "check" if score >= 60 else "poor"
        for f in flags:
            stats["flag_" + f] += 1
        stats["quality_" + quality] += 1
        stats["published"] += 1
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(lon, 5), round(lat, 5)]},
            "properties": {"name": name or "(unnamed)", "kind": kind, "category": src["category"],
                           "status": status, "flags": flags, "quality": quality,
                           "src": src["id"], "ref": ref},
        })
    return feats, dict(stats)


# ---------------------------------------------------------------- metadata
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_dt(s):
    if not s:
        return None
    m = ISO_DATE.search(str(s))
    if not m:
        return None
    try:
        return datetime.strptime(m.group(0), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def iso_day(dt):
    return dt.strftime("%Y-%m-%d") if dt else None


def dataset_info(pkg, src, res, stats, now):
    dates = ISO_DATE.findall(str(pkg.get("dataset_date") or ""))
    coverage_end = parse_dt(dates[-1]) if dates else None
    modified = max([d for d in (parse_dt(pkg.get("last_modified")), parse_dt(pkg.get("metadata_modified")),
                                parse_dt((res or {}).get("last_modified"))) if d], default=None)
    freq = None
    try:
        f = int(str(pkg.get("data_update_frequency")))
        freq = f if f > 0 else None
    except (TypeError, ValueError):
        pass

    ref_date = coverage_end or modified
    age = (now - ref_date).days if ref_date else None
    warnings = []
    if age is None:
        warnings.append("No date found in the source metadata: cannot judge how current this data is.")
    elif age > 365:
        warnings.append(f"Source data covers the situation up to {iso_day(ref_date)} ({age} days ago). "
                        "Facilities may have opened, closed, moved or been damaged since.")
    elif age > 180:
        warnings.append(f"Source data is {age} days old; treat as indicative.")
    if modified and (now - modified).days > 365:
        warnings.append("Dataset has not been updated on HDX for over a year.")
    if freq and modified and (now - modified).days > 2 * freq:
        warnings.append(f"Overdue: the publisher expects updates every {freq} days.")
    if not pkg.get("license_title"):
        warnings.append("Licence not stated in metadata: check the HDX page before reusing.")
    if stats.get("raw", 0) and stats.get("published", 0) < 0.5 * stats["raw"]:
        warnings.append("More than half of the raw records were removed by validation or safeguards.")
    if src["category"] == "health":
        warnings.append("Operating status, opening hours and services offered are generally NOT included.")
    if src["category"] == "water":
        warnings.append("Water points may be dry, broken or contaminated; functionality is rarely verified.")

    org = (pkg.get("organization") or {}).get("title") or pkg.get("dataset_source") or ""
    caveats = (pkg.get("caveats") or "").strip()
    return {
        "id": src["id"], "label": src.get("label", src["id"]), "category": src["category"],
        "title": pkg.get("title"), "url": f"https://data.humdata.org/dataset/{pkg.get('name')}",
        "organization": org, "license": pkg.get("license_title") or "not stated",
        "coverage_end": iso_day(coverage_end), "last_modified": iso_day(modified),
        "update_frequency_days": freq, "age_days": age,
        "resource": (res or {}).get("name"), "resource_format": (res or {}).get("format"),
        "caveats": caveats[:600] or None, "counts": stats, "warnings": warnings, "status": "ok",
    }


# ---------------------------------------------------------------- sample data
def make_sample(cfg):
    rng = random.Random(42)
    towns = [("Juba", 4.85, 31.58), ("Wau", 7.70, 28.00), ("Malakal", 9.53, 31.66),
             ("Bor", 6.21, 31.56), ("Yei", 4.09, 30.68), ("Bentiu", 9.23, 29.83)]
    by_src = {}
    for src in cfg["sources"]:
        recs = []
        for i in range(70):
            town = towns[i % len(towns)]
            lat = town[1] + rng.uniform(-0.35, 0.35)
            lon = town[2] + rng.uniform(-0.35, 0.35)
            props = {"name": f"SAMPLE {src['category']} site {i:02d}",
                     "type": "clinic" if src["category"] == "health" else "borehole"}
            rec = {"lat": round(lat, 5), "lon": round(lon, 5), "lat_dec": 5, "lon_dec": 5, "props": props}
            if i % 11 == 0:
                props["name"] = ""
            if i % 13 == 0:
                rec["lat"], rec["lon"], rec["lat_dec"], rec["lon_dec"] = round(lat, 1), round(lon, 1), 1, 1
            recs.append(rec)
        recs.append(dict(recs[3]))  # duplicate
        recs.append({"lat": 0.0, "lon": 0.0, "lat_dec": 0, "lon_dec": 0, "props": {"name": "bad"}})
        by_src[src["id"]] = recs
    return by_src


# ---------------------------------------------------------------- main
def write_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--out", default=str(ROOT / "site" / "data"))
    ap.add_argument("--sample", action="store_true", help="synthetic offline demo data")
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    out = Path(args.out)
    now = datetime.now(timezone.utc)
    region = cfg["region"]

    # previous good output (used if a source fails to refresh)
    prev_feats, prev_meta = [], {}
    try:
        old_meta = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
        if not old_meta.get("is_sample"):
            prev_meta = {s["id"]: s for s in old_meta.get("sources", [])}
            prev_feats = json.loads((out / "aid_points.geojson").read_text(encoding="utf-8"))["features"]
    except (OSError, ValueError, KeyError):
        pass

    all_feats, sources_meta, ok = [], [], 0
    sample = make_sample(cfg) if args.sample else None

    for src in cfg["sources"]:
        print(f"== {src['id']}")
        try:
            if sample is not None:
                feats, stats = process_source(src, sample[src["id"]], region)
                meta = {"id": src["id"], "label": src["label"], "category": src["category"],
                        "title": f"SAMPLE (synthetic) {src['label']}: NOT REAL DATA",
                        "url": "https://data.humdata.org", "organization": "Synthetic test data",
                        "license": "n/a", "coverage_end": iso_day(now), "last_modified": iso_day(now),
                        "update_frequency_days": None, "age_days": 0, "resource": None,
                        "resource_format": None, "caveats": None, "counts": stats,
                        "warnings": ["Synthetic data for testing the app. Do not use for decisions."],
                        "status": "ok"}
            else:
                pkg = resolve_dataset(src, region)
                records, res = load_records(pkg, src)
                feats, stats = process_source(src, records, region)
                meta = dataset_info(pkg, src, res, stats, now)
            all_feats += feats
            sources_meta.append(meta)
            ok += 1
            print(f"  published {len(feats)} of {stats.get('raw', 0)} records")
        except Exception as e:  # noqa: BLE001
            print(f"  !! refresh failed: {e}")
            old = prev_meta.get(src["id"])
            if old:
                all_feats += [f for f in prev_feats if f["properties"].get("src") == src["id"]]
                old["status"] = "stale_fallback"
                old["warnings"] = [f"Refresh failed on {iso_day(now)}; showing the previous successful "
                                   "download."] + [w for w in old.get("warnings", []) if not w.startswith("Refresh failed")]
                sources_meta.append(old)
            else:
                sources_meta.append({"id": src["id"], "label": src.get("label", src["id"]),
                                     "category": src["category"], "title": src.get("search"),
                                     "status": "failed", "error": str(e)[:300], "counts": {}, "warnings":
                                     ["This source could not be loaded and is missing from the map."]})

    if ok == 0 and not args.sample:
        print("All sources failed; leaving existing output untouched.")
        sys.exit(1)

    meta_out = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "is_sample": bool(args.sample),
        "region": {"name": region["name"], "bbox": region["bbox"]},
        "sources": sources_meta,
        "total_points": len(all_feats),
        "notes": [
            "A missing point does not mean a missing service: it may simply be missing from the data.",
            "Locations are straight-line positions as published; AidMap does not know road access or security.",
            "Records mentioning protection, shelter-for-survivors, security-force or similar sensitive "
            "sites are excluded, and no personal data is published.",
        ],
    }
    write_atomic(out / "aid_points.geojson",
                 json.dumps({"type": "FeatureCollection", "features": all_feats}, separators=(",", ":")))
    write_atomic(out / "metadata.json", json.dumps(meta_out, indent=2))
    print(f"Wrote {len(all_feats)} points to {out}")


if __name__ == "__main__":
    main()
