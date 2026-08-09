import datetime
from unittest.mock import MagicMock, patch

import requests
from django.core.cache import cache
from django.test import TestCase

from app.models import TV, Item, Season, Status
from events.calendar.helpers import date_parser
from events.calendar.main import fetch_releases
from events.calendar.tv import (
    get_episode_datetime,
    get_seasons_to_process,
    get_tvmaze_episode_map,
    get_tvmaze_response,
    process_season_episodes,
    process_tv,
)
from events.models import Event, SentinelDatetime
from events.tests.calendar.utils import CalendarFixturesMixin


class CalendarTVTests(CalendarFixturesMixin, TestCase):
    """Test TV calendar processing."""

    @patch("events.calendar.tv.tmdb.tv")
    @patch("events.calendar.tv.tmdb.tv_with_seasons")
    @patch("events.calendar.tv.get_tvmaze_episode_map")
    def test_process_tv_season(
        self,
        mock_get_tvmaze_episode_map,
        mock_tv_with_seasons,
        mock_tv,
    ):
        """Test processing for a TV season."""
        mock_tv.return_value = {
            "related": {
                "seasons": [
                    {"season_number": 1, "episodes": [1, 2, 3]},
                    {"season_number": 2, "episodes": [1, 2]},
                    {"season_number": 3, "episodes": [1]},
                ],
            },
            "next_episode_season": 2,
        }

        mock_tv_with_seasons.return_value = {
            "season/1": {
                "image": "http://example.com/season1.jpg",
                "season_number": 1,
                "episodes": [
                    {"episode_number": 1, "air_date": "2008-01-20"},
                    {"episode_number": 2, "air_date": "2008-01-27"},
                    {"episode_number": 3, "air_date": "2008-02-03"},
                ],
                "tvdb_id": "81189",
            },
            "season/2": {
                "image": "http://example.com/season2.jpg",
                "season_number": 2,
                "episodes": [
                    {"episode_number": 1, "air_date": "2009-01-20"},
                    {"episode_number": 2, "air_date": "2009-01-27"},
                ],
                "tvdb_id": "81189",
            },
            "season/3": {
                "image": "http://example.com/season3.jpg",
                "season_number": 3,
                "episodes": [
                    {"episode_number": 1, "air_date": "2010-01-20"},
                ],
                "tvdb_id": "81189",
            },
        }

        mock_get_tvmaze_episode_map.return_value = {
            "1": ["2008-01-20T22:00:00+00:00"],
            "2": ["2008-01-27T22:00:00+00:00"],
            "3": ["2008-02-03T22:00:00+00:00"],
        }

        events_bulk = []
        process_tv(self.tv_item, events_bulk)

        self.assertEqual(len(events_bulk), 6)
        self.assertEqual(events_bulk[0].item, self.season_item)
        self.assertEqual(events_bulk[0].content_number, 1)

        expected_date = datetime.datetime.fromisoformat("2008-01-20T22:00:00+00:00")
        self.assertEqual(events_bulk[0].datetime, expected_date)

    @patch("events.calendar.tv.get_tvmaze_episode_map")
    @patch("events.calendar.tv.tmdb.tv_with_seasons")
    @patch("events.calendar.tv.tmdb.tv")
    def test_process_tv_reopens_completed_show_with_new_season_as_planning(
        self,
        mock_tv,
        mock_tv_with_seasons,
        mock_get_tvmaze_episode_map,
    ):
        """Completed TV should reopen and create the discovered season as planning."""
        TV.objects.filter(item=self.tv_item, user=self.user).update(
            status=Status.COMPLETED.value,
        )
        Season.objects.filter(item=self.season_item, user=self.user).update(
            status=Status.COMPLETED.value,
        )
        Event.objects.create(
            item=self.season_item,
            content_number=1,
            datetime=date_parser("2008-01-20"),
        )

        mock_tv.return_value = {
            "related": {
                "seasons": [
                    {"season_number": 1, "episodes": [1]},
                    {"season_number": 2, "episodes": [1]},
                ],
            },
            "next_episode_season": 2,
        }
        mock_tv_with_seasons.return_value = {
            "season/2": {
                "image": "http://example.com/season2.jpg",
                "season_number": 2,
                "episodes": [
                    {"episode_number": 1, "air_date": "2027-01-20"},
                ],
                "tvdb_id": "81189",
            },
        }
        mock_get_tvmaze_episode_map.return_value = {}

        events_bulk = []
        process_tv(self.tv_item, events_bulk)

        season_two_item = Item.objects.get(
            media_id=self.tv_item.media_id,
            source=self.tv_item.source,
            media_type=self.season_item.media_type,
            season_number=2,
        )
        season_two = Season.objects.get(item=season_two_item, user=self.user)
        tv = TV.objects.get(item=self.tv_item, user=self.user)

        self.assertEqual(tv.status, Status.IN_PROGRESS.value)
        self.assertEqual(season_two.status, Status.PLANNING.value)
        self.assertEqual(len(events_bulk), 1)

    @patch("events.calendar.tv.get_tvmaze_episode_map")
    @patch("events.calendar.tv.tmdb.tv_with_seasons")
    @patch("events.calendar.tv.tmdb.tv")
    def test_process_tv_does_not_reopen_completed_show_for_past_only_season(
        self,
        mock_tv,
        mock_tv_with_seasons,
        mock_get_tvmaze_episode_map,
    ):
        """Past-only seasons should not reopen a completed TV entry."""
        TV.objects.filter(item=self.tv_item, user=self.user).update(
            status=Status.COMPLETED.value,
        )
        Season.objects.filter(item=self.season_item, user=self.user).update(
            status=Status.COMPLETED.value,
        )
        Event.objects.create(
            item=self.season_item,
            content_number=1,
            datetime=date_parser("2008-01-20"),
        )

        mock_tv.return_value = {
            "related": {
                "seasons": [
                    {"season_number": 1, "episodes": [1]},
                    {"season_number": 2, "episodes": [1]},
                ],
            },
            "next_episode_season": 2,
        }
        mock_tv_with_seasons.return_value = {
            "season/2": {
                "image": "http://example.com/season2.jpg",
                "season_number": 2,
                "episodes": [
                    {"episode_number": 1, "air_date": "2010-01-20"},
                ],
                "tvdb_id": "81189",
            },
        }
        mock_get_tvmaze_episode_map.return_value = {}

        events_bulk = []
        process_tv(self.tv_item, events_bulk)

        tv = TV.objects.get(item=self.tv_item, user=self.user)
        self.assertEqual(tv.status, Status.COMPLETED.value)
        self.assertFalse(
            Season.objects.filter(
                item__media_id=self.tv_item.media_id,
                item__source=self.tv_item.source,
                item__season_number=2,
                user=self.user,
            ).exists(),
        )
        self.assertEqual(len(events_bulk), 1)

    @patch("events.calendar.tv.services.api_request")
    def test_get_tvmaze_episode_map(self, mock_api_request):
        """Test get_tvmaze_episode_map function."""
        cache.clear()

        mock_api_request.side_effect = [
            {"id": 12345},
            {
                "_embedded": {
                    "episodes": [
                        {
                            "season": 1,
                            "number": 1,
                            "airstamp": "2008-01-20T22:00:00+00:00",
                            "airtime": "22:00",
                        },
                        {
                            "season": 1,
                            "number": 2,
                            "airstamp": "2008-01-27T22:00:00+00:00",
                            "airtime": "22:00",
                        },
                    ],
                },
            },
        ]

        result = get_tvmaze_episode_map("81189")

        self.assertEqual(
            result,
            {
                "1": ["2008-01-20T22:00:00+00:00"],
                "2": ["2008-01-27T22:00:00+00:00"],
            },
        )

        cached_result = cache.get("tvmaze_map_v3_81189")
        self.assertEqual(cached_result, result)

        mock_api_request.reset_mock()
        get_tvmaze_episode_map("81189")
        mock_api_request.assert_not_called()

    @patch("events.calendar.tv.services.api_request")
    def test_get_tvmaze_episode_map_skips_episodes_without_air_time(
        self,
        mock_api_request,
    ):
        """Airstamps standing in for an unknown air time are left out.

        TVMaze still builds an airstamp when it has no air time, placing it at
        midday in the network timezone, which is not a real broadcast time.
        """
        cache.clear()

        mock_api_request.side_effect = [
            {"id": 87753},
            {
                "_embedded": {
                    "episodes": [
                        {
                            "season": 1,
                            "number": 1,
                            "airstamp": "2026-07-27T16:00:00+00:00",
                            "airtime": "",
                        },
                        {
                            "season": 1,
                            "number": 2,
                            "airstamp": "2026-08-03T20:00:00+00:00",
                            "airtime": "16:00",
                        },
                    ],
                },
            },
        ]

        result = get_tvmaze_episode_map("468000")

        self.assertEqual(result, {"2": ["2026-08-03T20:00:00+00:00"]})

    def test_get_episode_datetime_keeps_tmdb_day_without_tvmaze_air_time(self):
        """Episodes TVMaze cannot time keep the TMDB day and no air time."""
        result = get_episode_datetime(
            {"air_date": "2026-07-27"},
            season_number=1,
            episode_number=1,
            tvmaze_map={},
        )

        self.assertEqual(result, date_parser("2026-07-27"))
        self.assertTrue(Event(datetime=result).is_sentinel_time)

    @patch("events.calendar.tv.services.api_request")
    def test_get_tvmaze_episode_map_lookup_failure(self, mock_api_request):
        """Test get_tvmaze_episode_map when lookup fails."""
        cache.clear()
        mock_api_request.return_value = None

        result = get_tvmaze_episode_map("invalid_id")

        self.assertEqual(result, {})
        mock_api_request.assert_called_once()

    def test_get_episode_datetime_falls_back_to_tmdb_air_date(self):
        """TMDB dates should be used when TVMaze has no timestamp."""
        result = get_episode_datetime(
            {"air_date": "2025-01-31"},
            season_number=1,
            episode_number=2,
            tvmaze_map={},
        )

        self.assertEqual(result, date_parser("2025-01-31"))

    def test_get_episode_datetime_uses_tvmaze_time_for_matching_day(self):
        """TVMaze refines the TMDB date with its exact air time."""
        result = get_episode_datetime(
            {"air_date": "2025-01-31"},
            season_number=1,
            episode_number=2,
            tvmaze_map={"2": ["2025-01-31T22:00:00+00:00"]},
        )

        self.assertEqual(
            result,
            datetime.datetime.fromisoformat("2025-01-31T22:00:00+00:00"),
        )

    def test_get_episode_datetime_accepts_tvmaze_across_midnight(self):
        """A late night broadcast landing on the next UTC day is still used."""
        result = get_episode_datetime(
            {"air_date": "2025-01-31"},
            season_number=1,
            episode_number=2,
            tvmaze_map={"2": ["2025-02-01T01:00:00+00:00"]},
        )

        self.assertEqual(
            result,
            datetime.datetime.fromisoformat("2025-02-01T01:00:00+00:00"),
        )

    def test_get_episode_datetime_ignores_mismatched_tvmaze_season(self):
        """Season numbering mismatches must not override the TMDB date (#1).

        TMDB season 11 of Futurama airs in 2026, while TVMaze season 11 is the
        2023 revival, so the lookup hits a completely different season.
        """
        result = get_episode_datetime(
            {"air_date": "2026-08-03"},
            season_number=11,
            episode_number=1,
            tvmaze_map={"1": ["2023-07-24T02:00:00+00:00"]},
        )

        self.assertEqual(result, date_parser("2026-08-03"))

    def test_get_episode_datetime_keeps_air_time_across_season_numbering(self):
        """The air time survives a numbering mismatch (#5).

        TMDB season 11 of Futurama is TVMaze season 14, so the right airstamp
        sits under another season number alongside a 2023 one for the same
        episode number.
        """
        result = get_episode_datetime(
            {"air_date": "2026-08-03"},
            season_number=11,
            episode_number=1,
            tvmaze_map={
                "1": [
                    "2023-07-24T02:00:00+00:00",
                    "2026-08-04T02:00:00+00:00",
                ],
            },
        )

        self.assertEqual(
            result,
            datetime.datetime.fromisoformat("2026-08-04T02:00:00+00:00"),
        )

    def test_get_episode_datetime_separates_episodes_airing_the_same_day(self):
        """Two episodes on one day each keep their own air time."""
        tvmaze_map = {
            "1": ["2026-08-04T02:00:00+00:00"],
            "2": ["2026-08-04T02:30:00+00:00"],
        }

        first = get_episode_datetime(
            {"air_date": "2026-08-03"},
            season_number=11,
            episode_number=1,
            tvmaze_map=tvmaze_map,
        )
        second = get_episode_datetime(
            {"air_date": "2026-08-03"},
            season_number=11,
            episode_number=2,
            tvmaze_map=tvmaze_map,
        )

        self.assertEqual(
            first,
            datetime.datetime.fromisoformat("2026-08-04T02:00:00+00:00"),
        )
        self.assertEqual(
            second,
            datetime.datetime.fromisoformat("2026-08-04T02:30:00+00:00"),
        )

    @patch("events.calendar.tv.get_tvmaze_episode_map")
    def test_process_season_episodes_keeps_real_air_times(
        self,
        mock_get_tvmaze_episode_map,
    ):
        """A renumbered season no longer falls back to the sentinel time."""
        mock_get_tvmaze_episode_map.return_value = {
            "1": [
                "2023-07-24T02:00:00+00:00",
                "2026-08-04T02:00:00+00:00",
            ],
            "2": [
                "2023-07-31T02:00:00+00:00",
                "2026-08-04T02:30:00+00:00",
            ],
        }

        events_bulk = []
        process_season_episodes(
            self.season_item,
            {
                "season_number": 11,
                "episodes": [
                    {"episode_number": 1, "air_date": "2026-08-03"},
                    {"episode_number": 2, "air_date": "2026-08-03"},
                ],
                "tvdb_id": "73871",
            },
            events_bulk,
        )

        by_number = {event.content_number: event for event in events_bulk}
        self.assertFalse(by_number[1].is_sentinel_time)
        self.assertFalse(by_number[2].is_sentinel_time)
        self.assertEqual(
            by_number[1].datetime,
            datetime.datetime.fromisoformat("2026-08-04T02:00:00+00:00"),
        )
        self.assertEqual(
            by_number[2].datetime,
            datetime.datetime.fromisoformat("2026-08-04T02:30:00+00:00"),
        )

    def test_get_episode_datetime_ignores_tvmaze_without_tmdb_date(self):
        """An unverifiable TVMaze stamp is dropped rather than trusted."""
        result = get_episode_datetime(
            {"air_date": ""},
            season_number=11,
            episode_number=1,
            tvmaze_map={"1": ["2023-07-24T02:00:00+00:00"]},
        )

        self.assertIsNone(result)

    def test_get_episode_datetime_ignores_invalid_tvmaze_airstamp(self):
        """A malformed airstamp falls back to the TMDB date."""
        result = get_episode_datetime(
            {"air_date": "2025-01-31"},
            season_number=1,
            episode_number=2,
            tvmaze_map={"2": ["not-a-timestamp"]},
        )

        self.assertEqual(result, date_parser("2025-01-31"))

    @patch("events.calendar.tv.get_tvmaze_episode_map")
    def test_process_season_episodes_keeps_upcoming_episodes_unreleased(
        self,
        mock_get_tvmaze_episode_map,
    ):
        """Mismatched TVMaze dates must not mark upcoming episodes as aired."""
        mock_get_tvmaze_episode_map.return_value = {
            "1": ["2023-07-24T02:00:00+00:00"],
            "2": ["2023-07-31T02:00:00+00:00"],
        }

        events_bulk = []
        process_season_episodes(
            self.season_item,
            {
                "season_number": 11,
                "episodes": [
                    {"episode_number": 1, "air_date": "2026-08-03"},
                    {"episode_number": 2, "air_date": "2099-08-10"},
                ],
                "tvdb_id": "73871",
            },
            events_bulk,
        )

        by_number = {event.content_number: event for event in events_bulk}
        self.assertEqual(by_number[1].datetime, date_parser("2026-08-03"))
        self.assertEqual(by_number[2].datetime, date_parser("2099-08-10"))

    def test_get_episode_datetime_returns_none_for_invalid_date(self):
        """Invalid or missing episode dates should resolve to None (unknown)."""
        result = get_episode_datetime(
            {"air_date": "not-a-date"},
            season_number=1,
            episode_number=2,
            tvmaze_map={},
        )

        self.assertIsNone(result)

    @patch("events.calendar.tv.services.api_request")
    def test_get_tvmaze_response_returns_empty_on_not_found(self, mock_api_request):
        """A 404 from the TVMaze lookup should be tolerated."""
        response = MagicMock()
        response.status_code = requests.codes.not_found
        response.text = "missing"
        mock_api_request.side_effect = requests.exceptions.HTTPError(response=response)

        self.assertEqual(get_tvmaze_response("999"), {})

    @patch("events.calendar.tv.tmdb.tv")
    def test_get_seasons_to_process_returns_empty_when_no_seasons(self, mock_tv):
        """TV metadata without seasons should short-circuit processing."""
        mock_tv.return_value = {"related": {"seasons": []}}

        self.assertEqual(get_seasons_to_process(self.tv_item), [])

    def _store_season_events(self, episode_numbers, sentinel_numbers=()):
        """Create stored events for the fixture season."""
        for episode_number in episode_numbers:
            datetime_value = (
                SentinelDatetime.max_datetime()
                if episode_number in sentinel_numbers
                else date_parser("2026-02-16")
            )
            Event.objects.create(
                item=self.season_item,
                content_number=episode_number,
                datetime=datetime_value,
            )

    @patch("events.calendar.tv.tmdb.tv")
    def test_get_seasons_to_process_skips_complete_past_season(self, mock_tv):
        """A season matching the provider episode list is left alone."""
        mock_tv.return_value = {
            "related": {"seasons": [{"season_number": 1, "max_progress": 3}]},
            "next_episode_season": 2,
        }
        self._store_season_events([1, 2, 3])

        self.assertEqual(get_seasons_to_process(self.tv_item), [])

    @patch("events.calendar.tv.tmdb.tv")
    def test_get_seasons_to_process_reprocesses_season_with_sentinel(self, mock_tv):
        """Undated stored episodes must be retried, not kept forever (#2).

        The Simpsons season 37 kept a sentinel row for episode 15 and a phantom
        row for episode 16 while the provider had real dates for 15 episodes.
        """
        mock_tv.return_value = {
            "related": {"seasons": [{"season_number": 1, "max_progress": 3}]},
            "next_episode_season": 2,
        }
        self._store_season_events([1, 2, 3], sentinel_numbers=[3])

        self.assertEqual(get_seasons_to_process(self.tv_item), [1])

    @patch("events.calendar.tv.tmdb.tv")
    def test_get_seasons_to_process_reprocesses_season_with_missing_episode(
        self,
        mock_tv,
    ):
        """A gap in the stored episode numbers marks the season stale."""
        mock_tv.return_value = {
            "related": {"seasons": [{"season_number": 1, "max_progress": 3}]},
            "next_episode_season": 2,
        }
        self._store_season_events([1, 3])

        self.assertEqual(get_seasons_to_process(self.tv_item), [1])

    @patch("events.calendar.tv.tmdb.tv")
    def test_get_seasons_to_process_reprocesses_season_with_phantom_episode(
        self,
        mock_tv,
    ):
        """Stored episodes that no longer exist upstream mark the season stale."""
        mock_tv.return_value = {
            "related": {"seasons": [{"season_number": 1, "max_progress": 3}]},
            "next_episode_season": 2,
        }
        self._store_season_events([1, 2, 3, 4])

        self.assertEqual(get_seasons_to_process(self.tv_item), [1])

    @patch("events.calendar.tv.tmdb.tv")
    def test_get_seasons_to_process_keeps_season_without_episode_count(self, mock_tv):
        """Without a provider episode count the stored events are trusted."""
        mock_tv.return_value = {
            "related": {"seasons": [{"season_number": 1}]},
            "next_episode_season": 2,
        }
        self._store_season_events([1, 2, 3])

        self.assertEqual(get_seasons_to_process(self.tv_item), [])

    @patch("events.calendar.tv.get_tvmaze_episode_map")
    @patch("events.calendar.tv.tmdb.tv_with_seasons")
    @patch("events.calendar.tv.tmdb.tv")
    def test_reload_repairs_stale_season_events(
        self,
        mock_tv,
        mock_tv_with_seasons,
        mock_get_tvmaze_episode_map,
    ):
        """A reload rebuilds a stale season and drops its phantom episodes (#2)."""
        mock_tv.return_value = {
            "related": {"seasons": [{"season_number": 1, "max_progress": 3}]},
            "next_episode_season": 2,
        }
        mock_tv_with_seasons.return_value = {
            "season/1": {
                "image": "http://example.com/season1.jpg",
                "season_number": 1,
                "episodes": [
                    {"episode_number": 1, "air_date": "2026-02-02"},
                    {"episode_number": 2, "air_date": "2026-02-09"},
                    {"episode_number": 3, "air_date": "2026-02-16"},
                ],
            },
        }
        mock_get_tvmaze_episode_map.return_value = {}

        # Missing episode 2, undated episode 3 and a phantom episode 4
        self._store_season_events([1, 3, 4], sentinel_numbers=[3, 4])

        fetch_releases(items_to_process=[self.tv_item])

        events = Event.objects.filter(item=self.season_item)
        self.assertEqual(
            {event.content_number: event.datetime for event in events},
            {
                1: date_parser("2026-02-02"),
                2: date_parser("2026-02-09"),
                3: date_parser("2026-02-16"),
            },
        )

    @patch("events.calendar.tv.get_seasons_to_process")
    def test_process_tv_returns_when_no_seasons_need_processing(
        self,
        mock_get_seasons_to_process,
    ):
        """process_tv should stop cleanly when there is nothing new to fetch."""
        mock_get_seasons_to_process.return_value = []

        events_bulk = []
        process_tv(self.tv_item, events_bulk)

        self.assertEqual(events_bulk, [])

    def test_process_season_episodes_handles_missing_tvdb_and_episodes(self):
        """A season without TVDB data or episodes should not add events."""
        events_bulk = []
        process_season_episodes(
            self.season_item,
            {
                "season_number": 1,
                "episodes": [],
            },
            events_bulk,
        )

        self.assertEqual(events_bulk, [])

    def test_process_season_episodes_marks_trailing_undated_as_unreleased(self):
        """Undated episodes with no later aired episode get the sentinel (#884)."""
        events_bulk = []
        process_season_episodes(
            self.season_item,
            {
                "season_number": 1,
                "episodes": [
                    {"episode_number": 1, "air_date": "2008-01-20"},
                    {"episode_number": 2, "air_date": "2099-01-20"},
                    {"episode_number": 3, "air_date": ""},
                ],
            },
            events_bulk,
        )

        by_number = {event.content_number: event for event in events_bulk}
        self.assertEqual(by_number[1].datetime, date_parser("2008-01-20"))
        self.assertEqual(by_number[2].datetime, date_parser("2099-01-20"))
        self.assertTrue(by_number[3].is_max_datetime)

    def test_process_season_episodes_assumes_undated_aired_when_later_episode_aired(
        self,
    ):
        """An undated episode followed by an aired one is assumed aired (#884)."""
        events_bulk = []
        process_season_episodes(
            self.season_item,
            {
                "season_number": 1,
                "episodes": [
                    {"episode_number": 1, "air_date": ""},
                    {"episode_number": 2, "air_date": "2008-01-20"},
                ],
            },
            events_bulk,
        )

        by_number = {event.content_number: event for event in events_bulk}
        self.assertFalse(by_number[1].is_max_datetime)
        self.assertEqual(by_number[1].datetime, date_parser("2008-01-20"))

    @patch("events.calendar.tv.services.api_request")
    def test_get_tvmaze_response_returns_empty_when_lookup_has_no_id(
        self,
        mock_api_request,
    ):
        """Lookup responses without a TVMaze id should return an empty mapping."""
        mock_api_request.return_value = {"name": "Breaking Bad"}

        self.assertEqual(get_tvmaze_response("81189"), {})

    @patch("events.calendar.tv.services.api_request")
    def test_get_tvmaze_response_returns_empty_when_episode_fetch_fails(
        self,
        mock_api_request,
    ):
        """Episode fetch errors after a successful lookup should be tolerated."""
        response = MagicMock()
        response.status_code = 500
        response.text = "boom"
        mock_api_request.side_effect = [
            {"id": 12345},
            requests.exceptions.HTTPError(response=response),
        ]

        self.assertEqual(get_tvmaze_response("81189"), {})
