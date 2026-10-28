import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline"))
import build_data as b  # noqa: E402

REGION = {"name": "T", "iso3": "SSD", "bbox": [3.3, 23.4, 12.4, 36.0]}
HEALTH = {"id": "h", "category": "health"}


def rec(lat, lon, **props):
    return {"lat": lat, "lon": lon, "lat_dec": b.decimals(lat), "lon_dec": b.decimals(lon), "props": props}


class PipelineTests(unittest.TestCase):
    def test_csv_with_hxl_row_and_aliases(self):
        data = b"Name,Latitude,Longitude,phone\n#meta+name,#geo+lat,#geo+lon,#contact\nJuba Clinic,4.85123,31.58123,+211123456789\n"
        recs = b.parse_csv(data)
        self.assertEqual(len(recs), 1)
        self.assertAlmostEqual(recs[0]["lat"], 4.85123)

    def test_csv_missing_coordinates_raises(self):
        with self.assertRaises(ValueError):
            b.parse_csv(b"name,town\nA,B\n")

    def test_geojson_points_and_polygon_centroid(self):
        gj = {"features": [
            {"geometry": {"type": "Point", "coordinates": [31.5, 4.8]}, "properties": {"Name": "A"}},
            {"geometry": {"type": "Polygon", "coordinates": [[[31, 4], [32, 4], [32, 5], [31, 5]]]}, "properties": {}},
            {"geometry": None, "properties": {}}]}
        recs = b.parse_geojson(json.dumps(gj).encode())
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["props"]["name"], "A")

    def test_drops_and_flags(self):
        records = [
            rec(4.85123, 31.58123, name="Good Clinic", amenity="clinic"),
            rec(4.85123, 31.58123, name="Good Clinic", amenity="clinic"),      # duplicate
            rec(0.0, 0.0, name="Zero"),                                          # zero coords
            rec(50.1, 10.1, name="Far away"),                                    # outside region
            rec(5.0123, 31.0123, name="Safe house for survivors"),               # sensitive
            rec(5.0123, 31.0123, name="Police post clinic"),                     # sensitive
            rec(6.1, 30.1, name="", amenity="clinic"),                           # no name + low precision
            rec(None, 30.1, name="No lat"),                                      # bad coords
        ]
        feats, stats = b.process_source(HEALTH, records, REGION)
        self.assertEqual(stats["raw"], 8)
        self.assertEqual(stats["published"], 3)
        self.assertEqual(stats["dropped_zero_coordinates"], 1)
        self.assertEqual(stats["dropped_outside_region"], 1)
        self.assertEqual(stats["dropped_sensitive"], 2)
        self.assertEqual(stats["dropped_bad_coordinates"], 1)
        self.assertIn("possible_duplicate", feats[1]["properties"]["flags"])
        self.assertIn("no_name", feats[2]["properties"]["flags"])
        self.assertIn("low_precision", feats[2]["properties"]["flags"])

    def test_only_allowlisted_fields_are_published(self):
        feats, _ = b.process_source(HEALTH, [rec(4.85123, 31.58123, name="Clinic", phone="123", email="a@b.c",
                                                 contact_person="Jane Doe")], REGION)
        self.assertEqual(set(feats[0]["properties"]), set(b.PUBLISHED_FIELDS))
        self.assertNotIn("Jane", json.dumps(feats))

    def test_names_with_contact_details_are_redacted(self):
        self.assertEqual(b.clean_name("Clinic call +211 912 345 678"), "")
        self.assertEqual(b.clean_name("info@clinic.org"), "")
        self.assertEqual(b.clean_name("Wau Teaching Hospital"), "Wau Teaching Hospital")

    def test_classify_and_status(self):
        self.assertEqual(b.classify("health", "", "Malakal PHCC"), "Primary health care centre")
        self.assertEqual(b.classify("water", "hand pump", "x"), "Handpump")
        self.assertEqual(b.norm_status("Non-functional"), "Not functional / closed")
        self.assertEqual(b.norm_status("Functional"), "Functional / open")
        self.assertEqual(b.norm_status(""), "Unknown")

    def test_staleness_warning(self):
        from datetime import datetime, timezone
        pkg = {"title": "T", "name": "t", "dataset_date": "[2014-04-04T00:00:00 TO 2019-09-10T23:59:59]",
               "last_modified": "2019-09-10T00:00:00", "license_title": "CC BY"}
        info = b.dataset_info(pkg, HEALTH, None, {"raw": 10, "published": 10},
                              datetime(2026, 10, 22, tzinfo=timezone.utc))
        self.assertTrue(any("days ago" in w for w in info["warnings"]))
        self.assertEqual(info["coverage_end"], "2019-09-10")


if __name__ == "__main__":
    unittest.main()
