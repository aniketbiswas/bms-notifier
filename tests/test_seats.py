import base64
import json
import unittest

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from check_shows import (
    SEAT_LAYOUT_ENCRYPTION_KEY,
    decode_seat_layout,
    extract_show_sessions,
    fetch_seat_layout,
    parse_seat_layout,
    select_backmost_seats,
    session_matches_filters,
)


class SeatLayoutTests(unittest.TestCase):
    def test_parses_categories_rows_seat_statuses_and_aisles(self):
        layout = (
            "GOLD:B:0000000002:2:N:0|LOUNGERS:C:0000000003:3:N:0||"
            "1:P:B1001+1:B2002+2:B0+0:B1004+4|"
            "2:AA:C1001+1:C1002+2"
        )

        parsed = parse_seat_layout(layout)

        self.assertEqual(parsed["areas"]["B"]["name"], "GOLD")
        self.assertEqual([row["label"] for row in parsed["rows"]], ["P", "AA"])
        self.assertEqual(
            [seat["status"] for seat in parsed["rows"][0]["seats"]],
            [1, 2, 0, 1],
        )
        self.assertEqual(parsed["rows"][0]["seats"][3]["display_number"], "4")

    def test_uses_payload_order_instead_of_row_label_sorting(self):
        layout = (
            "GOLD:B:0000000002:2:N:0||"
            "1:Z:B1001+1:B1002+2|"
            "2:AA:B1001+1:B1002+2"
        )

        match = select_backmost_seats(parse_seat_layout(layout), count=1)

        self.assertEqual(match["row"], "Z")
        self.assertEqual(match["seats"], ["Z1"])

    def test_a_can_be_the_front_row_when_it_is_last_in_payload_order(self):
        layout = (
            "GOLD:B:0000000002:2:N:0||"
            "1:Z:B1001+1|"
            "2:A:B1001+1"
        )

        match = select_backmost_seats(parse_seat_layout(layout), count=1)

        self.assertEqual(match["row"], "Z")

    def test_waits_when_actual_back_row_has_no_contiguous_block(self):
        layout = (
            "GOLD:B:0000000002:2:N:0||"
            "1:BACK:B1001+1:B0+0:B1003+3|"
            "2:FRONT:B1001+1:B1002+2"
        )

        match = select_backmost_seats(parse_seat_layout(layout), count=2)

        self.assertIsNone(match)

    def test_prefers_the_most_central_block_in_the_back_row(self):
        layout = (
            "GOLD:B:0000000002:2:N:0||"
            "1:BACK:B1001+1:B1002+2:B1003+3:B2004+4:B1005+5:B1006+6"
        )

        match = select_backmost_seats(parse_seat_layout(layout), count=2)

        self.assertEqual(match["seats"], ["BACK2", "BACK3"])

    def test_category_filter_changes_the_backmost_eligible_row(self):
        layout = (
            "GOLD:B:0000000002:2:N:0|LOUNGERS:C:0000000003:3:N:0||"
            "1:Q:C1001+1|"
            "2:P:B1001+1"
        )

        match = select_backmost_seats(
            parse_seat_layout(layout),
            count=1,
            categories=["Gold"],
        )

        self.assertEqual(match["row"], "P")
        self.assertEqual(match["category"], "GOLD")
        self.assertEqual(match["seats"], ["P1"])


class SeatPayloadTests(unittest.TestCase):
    @staticmethod
    def encrypt(plaintext):
        padder = padding.PKCS7(128).padder()
        padded = padder.update(plaintext.encode()) + padder.finalize()
        encryptor = Cipher(
            algorithms.AES(SEAT_LAYOUT_ENCRYPTION_KEY),
            modes.CBC(bytes(16)),
        ).encryptor()
        return base64.b64encode(
            encryptor.update(padded) + encryptor.finalize()
        ).decode()

    def test_decodes_the_encrypted_layout_payload(self):
        plaintext = "GOLD:B:0000000002:2:N:0||1:A:B1001+1"
        encoded = self.encrypt(plaintext)

        self.assertEqual(decode_seat_layout(encoded), plaintext)

    def test_fetches_and_parses_the_private_layout_envelope(self):
        plaintext = "YR:B:BR:2:N:0||1:A:B1001+1"

        class Response:
            status_code = 200

            def json(self):
                return {
                    "BookMyShow": {
                        "blnSuccess": "true",
                        "strData": SeatPayloadTests.encrypt(plaintext),
                    }
                }

        class Session:
            def __init__(self):
                self.calls = []

            def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return Response()

        session = Session()
        layout = fetch_seat_layout(session, {
            "venue_code": "AMBH",
            "session_id": "113347",
            "show_seat_number": "Y",
            "categories": [
                {
                    "areaCatCode": "BR",
                    "priceDesc": "RECLINER ROWS",
                    "curPrice": "708.00",
                }
            ],
        })

        self.assertEqual(layout["rows"][0]["label"], "A")
        self.assertEqual(layout["areas"]["B"]["name"], "RECLINER ROWS")
        self.assertEqual(layout["areas"]["B"]["price"], "708.00")
        self.assertEqual(len(session.calls), 1)
        self.assertIn("doTrans.aspx", session.calls[0][0])
        self.assertIn("multipart", session.calls[0][1])


class ShowtimeSessionTests(unittest.TestCase):
    def setUp(self):
        state = {
            "showtimesByEvent": {
                "showDates": {
                    "20260717": {
                        "primaryStatic": {
                            "data": {
                                "venues": {
                                    "AMBH": {
                                        "venueCode": "AMBH",
                                        "venueName": "AMB Cinemas: Gachibowli",
                                        "showSeatNo": "Y",
                                    }
                                }
                            }
                        },
                        "dynamic": {
                            "data": {
                                "showtimeWidgets": [
                                    {
                                        "type": "groupList",
                                        "data": [
                                            {
                                                "data": [
                                                    {
                                                        "type": "venue-card",
                                                        "additionalData": {
                                                            "venueCode": "AMBH",
                                                            "venueName": "AMB Cinemas: Gachibowli",
                                                        },
                                                        "showtimes": [
                                                            {
                                                                "title": "08:10 PM",
                                                                "screenAttr": "DOLBY ATMOS",
                                                                "additionalData": {
                                                                    "sessionId": "113347",
                                                                    "showTime": "08:10 PM",
                                                                    "showTimeCode": "2010",
                                                                    "attributes": "DOLBY ATMOS",
                                                                    "categories": [
                                                                        {
                                                                            "priceDesc": "GOLD",
                                                                            "curPrice": "295.00",
                                                                        }
                                                                    ],
                                                                },
                                                            }
                                                        ],
                                                    }
                                                ]
                                            }
                                        ],
                                    }
                                ]
                            }
                        },
                    }
                }
            }
        }
        self.html = (
            "<html><script>window.__INITIAL_STATE__ = "
            f"{json.dumps(state)};\n</script></html>"
        )

    def test_extracts_session_and_venue_metadata(self):
        sessions = extract_show_sessions(self.html, "20260717")

        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["venue_code"], "AMBH")
        self.assertEqual(sessions[0]["session_id"], "113347")
        self.assertEqual(sessions[0]["attributes"], "DOLBY ATMOS")
        self.assertEqual(sessions[0]["show_seat_number"], "Y")

    def test_filters_show_type_and_time_before_seat_lookup(self):
        session = extract_show_sessions(self.html, "20260717")[0]

        self.assertTrue(session_matches_filters(session, {
            "show_types": ["dolby"],
            "showtimes": {"after": "18:00", "before": "23:00"},
        }))
        self.assertFalse(session_matches_filters(session, {
            "show_types": ["imax"],
            "showtimes": {},
        }))


if __name__ == "__main__":
    unittest.main()