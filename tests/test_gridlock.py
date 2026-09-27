import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from sqlalchemy import create_engine

from overlap import cost_estimate, find_overlaps, get_tier, load_projects
from storage import (
    configured_database_url,
    initialize_database,
    load_opportunities,
    load_recent_events,
    load_project_records,
    replace_project_records,
    save_opportunity,
    update_opportunity_status,
)


PROJECTS_FILE = Path(__file__).resolve().parents[1] / "data" / "sample_projects.csv"


class ProjectDataTests(unittest.TestCase):
    def test_rejects_missing_columns(self):
        with self.assertRaisesRegex(ValueError, "Missing required CSV columns"):
            load_projects(pd.DataFrame({"id": ["one"]}))

    def test_rejects_duplicate_project_ids(self):
        frame = pd.read_csv(PROJECTS_FILE)
        frame.loc[1, "id"] = frame.loc[0, "id"]
        with self.assertRaisesRegex(ValueError, "IDs must be unique"):
            load_projects(frame)

    def test_rejects_non_finite_project_years(self):
        frame = pd.read_csv(PROJECTS_FILE)
        frame.loc[0, "start_year"] = float("nan")
        with self.assertRaisesRegex(ValueError, "finite number"):
            load_projects(frame)

    def test_rejects_invalid_wkt(self):
        frame = pd.read_csv(PROJECTS_FILE)
        frame.loc[0, "geometry_wkt"] = "not WKT"
        with self.assertRaisesRegex(ValueError, "valid WKT"):
            load_projects(frame)


class OverlapRankingTests(unittest.TestCase):
    def test_tiers_follow_the_challenge_distance_boundaries(self):
        self.assertEqual(get_tier(0.01)[1], "Touching / crossing")
        self.assertEqual(get_tier(1.6)[1], "Under 8 km")
        self.assertEqual(get_tier(8.0)[1], "Under 40 km")
        self.assertEqual(get_tier(40.0)[1], "Under 40 km")
        self.assertIsNone(get_tier(40.01))

    def test_timeline_bonus_only_applies_when_build_windows_overlap(self):
        records = pd.DataFrame(
            [
                ["D1", "DESC", "DESC early", "substation", 115, 2026, 2027,
                 "POINT (-81 32)"],
                ["D2", "DESC", "DESC late", "substation", 115, 2027, 2028,
                 "POINT (-81 32)"],
                ["G1", "GPC", "GPC early", "substation", 115, 2027, 2028,
                 "POINT (-81 32)"],
                ["G2", "GPC", "GPC late", "substation", 115, 2030, 2031,
                 "POINT (-81 32)"],
            ],
            columns=[
                "id", "utility", "name", "kind", "voltage_kv", "start_year",
                "end_year", "geometry_wkt",
            ],
        )
        matches = find_overlaps(load_projects(records), "DESC", "GPC")

        matching_windows = matches[matches.overlap_years > 0]
        nonmatching_windows = matches[matches.overlap_years == 0]
        self.assertEqual(len(matching_windows), 2)
        self.assertEqual(len(nonmatching_windows), 2)
        self.assertTrue((matching_windows.score == 120).all())
        self.assertTrue((nonmatching_windows.score == 100).all())
        self.assertTrue(nonmatching_windows.timeline.str.startswith("No overlap").all())


class StorageWorkflowTests(unittest.TestCase):
    def test_tiger_data_postgres_url_selects_psycopg(self):
        with patch.dict(
            "os.environ",
            {"TIGERDATA_URL": "postgres://demo:secret@example.com/gridlock"},
            clear=True,
        ):
            self.assertEqual(
                configured_database_url(),
                "postgresql+psycopg://demo:secret@example.com/gridlock",
            )

    def test_import_save_update_and_activity_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = create_engine(f"sqlite:///{temp_dir}/gridlock.sqlite3")
            try:
                self.assertFalse(initialize_database(engine, PROJECTS_FILE))
                projects = load_projects(load_project_records(engine))
                self.assertEqual(len(projects), 11)

                imported = projects.iloc[:2]
                replace_project_records(engine, imported)
                self.assertEqual(len(load_project_records(engine)), 2)

                opportunity = find_overlaps(projects, "DESC", "GPC").iloc[0]
                estimate = cost_estimate(projects, opportunity, 38, 15000, 250000)
                self.assertTrue(save_opportunity(engine, opportunity, estimate))
                self.assertFalse(save_opportunity(engine, opportunity, estimate))

                pipeline = load_opportunities(engine)
                self.assertEqual(pipeline.iloc[0].status, "Saved")
                key = pipeline.iloc[0].opportunity_key
                update_opportunity_status(engine, key, "Contacted")

                self.assertEqual(load_opportunities(engine).iloc[0].status, "Contacted")
                events = load_recent_events(engine)
                self.assertEqual(list(events.event_type), ["status_changed", "saved"])
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
