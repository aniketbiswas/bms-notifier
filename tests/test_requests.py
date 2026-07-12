import json
import unittest
from unittest.mock import Mock, patch

import check_shows


class FetchPageTests(unittest.TestCase):
    @patch("check_shows.time.sleep")
    @patch("check_shows.requests.Session")
    def test_exhausted_retries_raise_fetch_error(self, session_class, _sleep):
        response = Mock(status_code=503)
        session_class.return_value.get.return_value = response

        with self.assertRaises(check_shows.FetchError):
            check_shows.fetch_page("https://example.test", max_retries=2)

        self.assertEqual(session_class.call_count, 2)


class ShowtimeTests(unittest.TestCase):
    def test_title_matching_uses_the_full_expected_title(self):
        self.assertTrue(
            check_shows.movie_title_matches(
                "The Odyssey",
                "The Odyssey Movie Tickets and Showtimes | BookMyShow",
            )
        )
        self.assertFalse(
            check_shows.movie_title_matches(
                "The Odyssey",
                "The Amateur Movie Tickets and Showtimes | BookMyShow",
            )
        )

    @patch("check_shows.fetch_page")
    def test_showtime_page_is_fetched_and_parsed_once(self, fetch_page):
        fetch_page.return_value = """
            <title>The Odyssey Movie Tickets and Showtimes | BookMyShow</title>
            <script>
              {"showDate":"20260725","venueName":"Cinema A","showTime":"10:00"}
            </script>
        """

        matched = check_shows.check_showtimes(
            "Hyderabad",
            {"name": "The Odyssey", "date": "20260725", "theatres": ["Cinema A"]},
            ["ET12345678"],
            "the-odyssey",
        )

        self.assertEqual(matched, {"Cinema A": ["10:00"]})
        fetch_page.assert_called_once()

    @patch("check_shows.requests.Session")
    @patch("check_shows.fetch_seat_layout")
    @patch("check_shows.fetch_page")
    def test_filters_sessions_and_reports_a_backmost_seat(
        self,
        fetch_page,
        fetch_seat_layout,
        _session_class,
    ):
        state = {
            "showtimesByEvent": {
                "showDates": {
                    "20260717": {
                        "primaryStatic": {
                            "data": {
                                "venues": {
                                    "AMBH": {
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
                                                            "categories": [],
                                                        },
                                                    }
                                                ],
                                            }
                                        ]
                                    }
                                ]
                            }
                        },
                    }
                }
            }
        }
        fetch_page.return_value = (
            "<title>The Odyssey Movie Tickets | BookMyShow</title>"
            f"<script>window.__INITIAL_STATE__ = {json.dumps(state)};</script>"
        )
        fetch_seat_layout.return_value = check_shows.parse_seat_layout(
            "GOLD:B:0000000002:2:N:0|LOUNGERS:C:0000000003:3:N:0||"
            "1:Q:C1006+6|2:P:B1001+1"
        )

        matched = check_shows.check_showtimes(
            "Hyderabad",
            {
                "name": "The Odyssey",
                "date": "20260717",
                "theatres": ["AMB Cinemas"],
                "filters": {
                    "show_types": ["Dolby"],
                    "showtimes": {"after": "18:00"},
                    "seats": {
                        "position": "backmost",
                        "count": 1,
                        "categories": [],
                    },
                },
            },
            ["ET00452034"],
            "the-odyssey",
        )

        self.assertEqual(matched, {
            "AMB Cinemas: Gachibowli": [
                "08:10 PM · DOLBY ATMOS · LOUNGERS row Q · Q6"
            ]
        })
        fetch_seat_layout.assert_called_once()


class DiscoveryTests(unittest.TestCase):
        @patch("check_shows.fetch_page")
        def test_exact_movie_name_wins_over_ambiguous_substring(self, fetch_page):
                fetch_page.return_value = """
                        <script type="application/ld+json">
                            {
                                "@type": "ItemList",
                                "itemListElement": [
                                    {
                                        "name": "The Odyssey: An IMAX Preview",
                                        "url": "https://in.bookmyshow.com/movies/hyderabad/the-odyssey-preview/ET99999999"
                                    },
                                    {
                                        "name": "The Odyssey",
                                        "url": "https://in.bookmyshow.com/movies/hyderabad/the-odyssey/ET11111111"
                                    }
                                ]
                            }
                        </script>
                """

                movie = check_shows.discover_movie("The Odyssey", "hyderabad")

                self.assertEqual(movie["event_code"], "ET11111111")
                self.assertEqual(movie["slug"], "the-odyssey")

        @patch("check_shows.fetch_page")
        def test_event_codes_are_scoped_to_the_selected_movie_slug(self, fetch_page):
                fetch_page.return_value = """
                        <a href="/movies/hyderabad/the-odyssey/buytickets/ET11111111">Odyssey</a>
                        <script>{"eventCode":"ET99999999","name":"Recommended Movie"}</script>
                """

                codes = check_shows.discover_event_codes(
                        "https://in.bookmyshow.com/movies/hyderabad/the-odyssey/ET11111111",
                        "The Odyssey",
                )

                self.assertEqual(codes, ["ET11111111"])


if __name__ == "__main__":
    unittest.main()