import json
import tempfile
import unittest
from pathlib import Path

from check_shows import process_state_change, save_state


class StateChangeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.state_file = Path(self.temp_dir.name) / "movie.json"

    def test_failed_notification_preserves_previous_state(self):
        previous = {"Cinema A": ["10:00"]}
        current = {"Cinema A": ["10:00", "13:00"]}
        save_state(self.state_file, previous)

        notified = []
        succeeded = process_state_change(
            self.state_file,
            current,
            lambda additions: notified.append(additions) or False,
        )

        self.assertFalse(succeeded)
        self.assertEqual(notified, [{"Cinema A": ["13:00"]}])
        self.assertEqual(json.loads(self.state_file.read_text()), previous)

    def test_removals_update_state_without_notification(self):
        previous = {"Cinema A": ["10:00", "13:00"]}
        current = {"Cinema A": ["10:00"]}
        save_state(self.state_file, previous)

        notified = []
        succeeded = process_state_change(
            self.state_file,
            current,
            lambda additions: notified.append(additions) or True,
        )

        self.assertTrue(succeeded)
        self.assertEqual(notified, [])
        self.assertEqual(json.loads(self.state_file.read_text()), current)

    def test_only_additions_are_sent_and_committed(self):
        previous = {"Cinema A": ["10:00"], "Cinema B": []}
        current = {
            "Cinema A": ["10:00", "13:00"],
            "Cinema B": [],
            "Cinema C": [],
        }
        save_state(self.state_file, previous)

        notified = []
        succeeded = process_state_change(
            self.state_file,
            current,
            lambda additions: notified.append(additions) or True,
        )

        self.assertTrue(succeeded)
        self.assertEqual(
            notified,
            [{"Cinema A": ["13:00"], "Cinema C": []}],
        )
        self.assertEqual(json.loads(self.state_file.read_text()), current)


if __name__ == "__main__":
    unittest.main()