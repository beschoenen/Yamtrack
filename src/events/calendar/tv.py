import logging
from datetime import UTC, datetime, timedelta

import requests
from django.core.cache import cache
from django.db import transaction
from django.db.models import Prefetch
from django.utils import timezone
from simple_history.utils import bulk_create_with_history, bulk_update_with_history

from app.models import TV, Item, MediaTypes, Season, Status
from app.providers import services, tmdb
from events.models import Event

from .helpers import date_parser, resolve_episode_datetimes

logger = logging.getLogger(__name__)

# TMDB air dates carry no time and TVMaze airstamps are exact instants, so the
# two can land on either side of midnight for the same broadcast.
TVMAZE_DAY_TOLERANCE = timedelta(days=1)


def process_tv(tv_item, events_bulk):
    """Process TV item and create events for all seasons and episodes."""
    logger.info("Processing TV show: %s", tv_item)

    try:
        seasons_to_process = get_seasons_to_process(tv_item)

        if not seasons_to_process:
            logger.info("%s - No seasons need processing", tv_item)
            return

        processed_season_items = process_tv_seasons(
            tv_item,
            seasons_to_process,
            events_bulk,
        )
        reopen_completed_tv_with_new_seasons(
            tv_item,
            processed_season_items,
            events_bulk,
        )

    except services.ProviderAPIError:
        logger.warning(
            "Failed to fetch metadata for %s",
            tv_item,
        )
    except Exception:
        logger.exception("Error processing %s", tv_item)


def get_seasons_to_process(tv_item):
    """Identify which seasons of a TV show need to be processed."""
    tv_metadata = tmdb.tv(tv_item.media_id)

    if not tv_metadata.get("related", {}).get("seasons"):
        logger.warning("No seasons found for TV show: %s", tv_item)
        return []

    episode_counts = {
        season["season_number"]: season.get("max_progress")
        for season in tv_metadata["related"]["seasons"]
    }
    season_numbers = list(episode_counts)

    if not season_numbers:
        logger.warning("No valid seasons found for TV show: %s", tv_item)
        return []

    next_episode_season = tv_metadata.get("next_episode_season")

    existing_season_events = Event.objects.filter(
        item__media_id=tv_item.media_id,
        item__source=tv_item.source,
        item__media_type=MediaTypes.SEASON.value,
    ).select_related("item")

    stored_events = get_stored_events_by_season(existing_season_events)
    stale_seasons = [
        season_num
        for season_num in season_numbers
        if season_num in stored_events
        and has_stale_events(stored_events[season_num], episode_counts[season_num])
    ]

    seasons_to_process = [
        season_num
        for season_num in season_numbers
        if season_num not in stored_events
        or (next_episode_season and season_num >= next_episode_season)
        or season_num in stale_seasons
    ]

    if not seasons_to_process:
        return []

    logger.info(
        "%s - Processing %d seasons (Next episode season: %s, Stale seasons: %s)",
        tv_item,
        len(seasons_to_process),
        next_episode_season,
        stale_seasons or "none",
    )

    return seasons_to_process


def get_stored_events_by_season(season_events):
    """Group stored episode numbers and sentinel dates by season number."""
    stored_events = {}

    for event in season_events:
        if event.content_number is None:
            continue

        season = stored_events.setdefault(
            event.item.season_number,
            {"episode_numbers": set(), "has_sentinel": False},
        )
        season["episode_numbers"].add(event.content_number)
        season["has_sentinel"] = season["has_sentinel"] or event.is_max_datetime

    return stored_events


def has_stale_events(stored_season, episode_count):
    """Return True when stored events no longer match the provider episodes.

    Seasons keep their events forever once written, so an old reload can leave
    missing, phantom or undated episodes behind (issue #2). Sentinel dates hide
    an episode from the released counts, so those seasons are retried until the
    provider publishes a real date.
    """
    if stored_season["has_sentinel"]:
        return True

    if not episode_count:
        return False

    return stored_season["episode_numbers"] != set(range(1, episode_count + 1))


def process_tv_seasons(tv_item, seasons_to_process, events_bulk):
    """Process specific seasons of a TV show."""
    process_seasons_data = tmdb.tv_with_seasons(
        tv_item.media_id,
        seasons_to_process,
    )
    processed_season_items = []

    for season_number in seasons_to_process:
        season_key = f"season/{season_number}"
        if season_key not in process_seasons_data:
            logger.warning(
                "Season %s data not found for %s",
                season_number,
                tv_item,
            )
            continue

        season_metadata = process_seasons_data[season_key]

        season_item, _ = Item.objects.get_or_create(
            media_id=tv_item.media_id,
            source=tv_item.source,
            media_type=MediaTypes.SEASON.value,
            season_number=season_number,
            defaults={
                "title": tv_item.title,
                "image": season_metadata["image"],
            },
        )

        processed_season_items.append(season_item)
        process_season_episodes(season_item, season_metadata, events_bulk)

    return processed_season_items


def reopen_completed_tv_with_new_seasons(tv_item, season_items, events_bulk):
    """Reopen completed TV entries and create planning seasons when needed."""
    eligible_season_items = [
        season_item
        for season_item in season_items
        if season_item.season_number and season_item.season_number > 0
    ]
    season_item_map = {
        season_item.season_number: season_item for season_item in eligible_season_items
    }
    if not season_item_map:
        logger.info(
            "%s - No processed seasons eligible for completed-TV reopening",
            tv_item,
        )
        return

    # Only future season events should reopen a completed show; processed
    # past-only seasons can exist when local season tracking was incomplete.
    future_season_numbers = {
        event.item.season_number
        for event in events_bulk
        if event.item in eligible_season_items and event.datetime >= timezone.now()
    }
    if not future_season_numbers:
        logger.info(
            "%s - Processed seasons have no future events; "
            "skipping completed-TV reopening",
            tv_item,
        )
        return

    sorted_season_numbers = sorted(future_season_numbers)
    completed_tvs = list(
        TV.objects.filter(
            item=tv_item,
            status=Status.COMPLETED.value,
        )
        .select_related("user")
        .prefetch_related(
            Prefetch(
                "seasons",
                queryset=Season.objects.select_related("item"),
            ),
        ),
    )
    if not completed_tvs:
        logger.info("%s - No completed TV entries to reopen", tv_item)
        return

    logger.info(
        "%s - Checking %d completed TV entries against discovered seasons %s",
        tv_item,
        len(completed_tvs),
        sorted_season_numbers,
    )

    seasons_by_tv_id = {}
    tvs_to_update = []

    for tv in completed_tvs:
        existing_season_numbers = {
            season.item.season_number
            for season in tv.seasons.all()
            if season.item.season_number and season.item.season_number > 0
        }
        missing_season_numbers = [
            season_number
            for season_number in sorted_season_numbers
            if season_number not in existing_season_numbers
        ]

        if not missing_season_numbers:
            logger.info(
                "%s - User %s already tracks all discovered seasons",
                tv_item,
                tv.user,
            )
            continue

        logger.info(
            "%s - Reopening completed TV for user %s; new seasons: %s",
            tv_item,
            tv.user,
            missing_season_numbers,
        )
        seasons_by_tv_id[tv.id] = [
            Season(
                item=season_item_map[season_number],
                related_tv=tv,
                user=tv.user,
                status=Status.PLANNING.value,
            )
            for season_number in missing_season_numbers
        ]
        tv.status = Status.IN_PROGRESS.value
        tvs_to_update.append(tv)

    if not seasons_by_tv_id:
        logger.info("%s - No completed TV entries required reopening", tv_item)
        return

    with transaction.atomic():
        for tv in tvs_to_update:
            bulk_create_with_history(
                seasons_by_tv_id[tv.id],
                Season,
                default_user=tv.user,
            )
            bulk_update_with_history(
                [tv],
                TV,
                ["status"],
                default_user=tv.user,
            )
            logger.info(
                "%s - Reopened TV for user %s and created %d planning seasons",
                tv_item,
                tv.user,
                len(seasons_by_tv_id[tv.id]),
            )


def process_season_episodes(item, metadata, events_bulk):
    """Process episodes for a season and add them to events_bulk."""
    tvmaze_map = {}
    if metadata.get("tvdb_id"):
        logger.info(
            "%s - TVDB ID found, fetching TVMaze episode data",
            item,
        )
        tvmaze_map = get_tvmaze_episode_map(metadata["tvdb_id"])
    else:
        logger.warning(
            "%s - No TVDB ID found, skipping TVMaze episode data",
            item,
        )

    if not metadata.get("episodes"):
        logger.warning("%s - No episodes found in metadata", item)
        return

    season_number = metadata["season_number"]
    episode_datetimes = {
        episode["episode_number"]: get_episode_datetime(
            episode,
            season_number,
            episode["episode_number"],
            tvmaze_map,
        )
        for episode in metadata["episodes"]
    }
    resolved_datetimes = resolve_episode_datetimes(
        episode_datetimes,
        timezone.now(),
    )

    for episode_number, episode_datetime in resolved_datetimes.items():
        events_bulk.append(
            Event(
                item=item,
                content_number=episode_number,
                datetime=episode_datetime,
            ),
        )


def get_episode_datetime(episode, season_number, episode_number, tvmaze_map):
    """Return the known air datetime for an episode, or None when unknown."""
    tmdb_datetime = get_tmdb_datetime(episode, season_number, episode_number)
    tvmaze_datetime = get_tvmaze_datetime(
        tvmaze_map,
        season_number,
        episode_number,
        tmdb_datetime,
    )

    # TVMaze only adds a precise air time, TMDB decides the day
    return tvmaze_datetime or tmdb_datetime


def get_tmdb_datetime(episode, season_number, episode_number):
    """Return the TMDB air datetime for an episode, or None when unknown."""
    if not episode["air_date"]:
        return None

    try:
        return date_parser(episode["air_date"])
    except ValueError:
        logger.warning(
            "Invalid air date for S%sE%s from TMDB: %s",
            season_number,
            episode_number,
            episode["air_date"],
        )
        return None


def get_tvmaze_datetime(tvmaze_map, season_number, episode_number, tmdb_datetime):
    """Return the TVMaze airstamp that refines the TMDB air date.

    The map is keyed by episode number alone because TVMaze numbers seasons
    differently from TMDB for some shows (issue #1). The candidates are
    matched on the day instead, so an airstamp from another season cannot be
    picked up while the precise air time of the right one is kept (issue #5).
    """
    if tmdb_datetime is None:
        return None

    for airstamp in tvmaze_map.get(str(episode_number), []):
        tvmaze_datetime = parse_airstamp(airstamp, season_number, episode_number)

        if tvmaze_datetime is None:
            continue

        if abs(tvmaze_datetime - tmdb_datetime) <= TVMAZE_DAY_TOLERANCE:
            return tvmaze_datetime

    logger.debug(
        "No TVMaze air time matching TMDB %s for S%sE%s",
        tmdb_datetime.date(),
        season_number,
        episode_number,
    )
    return None


def parse_airstamp(airstamp, season_number, episode_number):
    """Return an aware datetime for a TVMaze airstamp, or None when invalid."""
    try:
        tvmaze_datetime = datetime.fromisoformat(airstamp)
    except ValueError:
        logger.warning(
            "Invalid airstamp for S%sE%s from TVMaze: %s",
            season_number,
            episode_number,
            airstamp,
        )
        return None

    if timezone.is_naive(tvmaze_datetime):
        tvmaze_datetime = tvmaze_datetime.replace(tzinfo=UTC)

    return tvmaze_datetime


def get_tvmaze_episode_map(tvdb_id):
    """Fetch and process episode data from TVMaze using TVDB ID with caching.

    Episodes are grouped by episode number, holding every airstamp TVMaze has
    for that number across its own seasons. The caller picks the one matching
    the TMDB air date, so the two numbering schemes never have to line up.

    Only episodes with a known air time are kept. TVMaze still builds an
    airstamp when it has none, placing it at midday in the network timezone,
    which would otherwise be stored as if it were the real broadcast time.
    """
    # Versioned, the cached contents changed with the airtime requirement
    cache_key = f"tvmaze_map_v3_{tvdb_id}"
    cached_map = cache.get(cache_key)

    if cached_map:
        logger.info("%s - Using cached TVMaze episode map", tvdb_id)
        return cached_map

    show_response = get_tvmaze_response(tvdb_id)
    tvmaze_map = {}

    if show_response:
        episodes = show_response["_embedded"]["episodes"]

        for episode in episodes:
            episode_num = episode.get("number")
            airstamp = episode.get("airstamp")
            if episode_num is not None and airstamp and episode.get("airtime"):
                tvmaze_map.setdefault(str(episode_num), []).append(airstamp)

    cache.set(cache_key, tvmaze_map)
    logger.info(
        "%s - Cached TVMaze episode map with %d entries",
        tvdb_id,
        len(tvmaze_map),
    )

    return tvmaze_map


def get_tvmaze_response(tvdb_id):
    """Fetch episode data from TVMaze using TVDB ID."""
    lookup_url = f"https://api.tvmaze.com/lookup/shows?thetvdb={tvdb_id}"
    try:
        lookup_response = services.api_request("TVMaze", "GET", lookup_url)
    except requests.exceptions.HTTPError as err:
        if err.response.status_code == requests.codes.not_found:
            logger.warning(
                "TVMaze lookup failed for TVDB ID %s - %s",
                tvdb_id,
                err.response.text,
            )
        else:
            logger.warning(
                "%s - TVMaze lookup error: %s",
                tvdb_id,
                err.response.text,
            )
        lookup_response = {}

    if not lookup_response:
        logger.warning("%s - No TVMaze lookup response for TVDB ID", tvdb_id)
        return {}

    tvmaze_id = lookup_response.get("id")

    if not tvmaze_id:
        logger.warning("%s - TVMaze ID not found for TVDB ID", tvdb_id)
        return {}

    show_url = f"https://api.tvmaze.com/shows/{tvmaze_id}?embed=episodes"

    try:
        return services.api_request("TVMaze", "GET", show_url)
    except requests.exceptions.HTTPError:
        return {}
