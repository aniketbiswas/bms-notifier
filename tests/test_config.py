import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import check_shows


class ConfigTests(unittest.TestCase):
    def load(self, yaml_text):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = Path(temp_dir) / "config.yml"
            config_file.write_text(yaml_text)
            with (
                patch.object(check_shows, "CONFIG_FILE", config_file),
                patch.dict(os.environ, {}, clear=True),
            ):
                return check_shows.load_config()

    def test_scalar_dates_and_theatres_are_normalized(self):
        config = self.load(
            """
            city: Hyderabad
            movies:
              - name: The Odyssey
                dates: "20260725"
                theatres: Cinema A
            """
        )

        self.assertEqual(config["movies"][0]["dates"], ["20260725"])
        self.assertEqual(config["movies"][0]["theatres"], ["Cinema A"])

    def test_invalid_calendar_date_is_rejected(self):
        with self.assertRaisesRegex(check_shows.ConfigError, "20260230"):
            self.load(
                """
                city: Hyderabad
                movies:
                  - name: The Odyssey
                    dates: ["20260230"]
                """
            )

    def test_movies_must_be_a_list(self):
        with self.assertRaisesRegex(check_shows.ConfigError, "movies"):
            self.load(
                """
                city: Hyderabad
                movies:
                  name: The Odyssey
                """
            )

    def test_seat_filters_are_normalized(self):
        config = self.load(
            """
            city: Hyderabad
            movies:
              - name: The Odyssey
                dates: ["20260725"]
                filters:
                  show_types: Dolby Atmos
                  showtimes:
                    after: "18:00"
                    before: "23:30"
                  seats:
                    position: backmost
                    count: 2
                    categories: Loungers
            """
        )

        self.assertEqual(config["movies"][0]["filters"], {
            "show_types": ["Dolby Atmos"],
            "showtimes": {"after": "18:00", "before": "23:30"},
            "seats": {
                "position": "backmost",
                "count": 2,
                "categories": ["Loungers"],
            },
        })

    def test_seat_filters_require_explicit_dates(self):
        with self.assertRaisesRegex(check_shows.ConfigError, "dates"):
            self.load(
                """
                city: Hyderabad
                movies:
                  - name: The Odyssey
                    filters:
                      seats:
                        position: backmost
                """
            )

    def test_unknown_seat_position_is_rejected(self):
        with self.assertRaisesRegex(check_shows.ConfigError, "position"):
            self.load(
                """
                city: Hyderabad
                movies:
                  - name: The Odyssey
                    dates: ["20260725"]
                    filters:
                      seats:
                        position: balcony
                """
            )


if __name__ == "__main__":
    unittest.main()