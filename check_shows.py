import os
import re
import json
import base64
import smtplib
import ssl
import datetime
import hashlib
import logging
import time
import random
import yaml
from html import escape, unescape
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from logging.handlers import RotatingFileHandler
from pathlib import Path

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from curl_cffi import CurlMime, requests

# --- Logging ---
LOG_FILE = Path(__file__).parent / "bms.log"
log = logging.getLogger("bms")


def configure_logging():
    if log.handlers:
        return

    log.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    handlers = [
        RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3),
        logging.StreamHandler(),
    ]
    for handler in handlers:
        handler.setFormatter(formatter)
        log.addHandler(handler)
    log.propagate = False

# --- Load config ---
CONFIG_FILE = Path(__file__).parent / "config.yml"


class ConfigError(ValueError):
    pass


class FetchError(RuntimeError):
    pass


def normalize_list(value, field_name):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return [value]
    raise ConfigError(f"{field_name} must be a value or list")


def normalize_dates(value, field_name):
    dates = []
    for raw_date in normalize_list(value, field_name):
        date = str(raw_date).strip()
        if not re.fullmatch(r"\d{8}", date):
            raise ConfigError(f"{field_name} contains invalid date {date!r}; use YYYYMMDD")
        try:
            datetime.datetime.strptime(date, "%Y%m%d")
        except ValueError as exc:
            raise ConfigError(f"{field_name} contains invalid date {date!r}") from exc
        if date not in dates:
            dates.append(date)
    return dates


def normalize_theatres(value, field_name):
    theatres = []
    for raw_theatre in normalize_list(value, field_name):
        if not isinstance(raw_theatre, str):
            raise ConfigError(f"{field_name} entries must be strings")
        theatre = raw_theatre.strip()
        if theatre and theatre not in theatres:
            theatres.append(theatre)
    return theatres


def normalize_filters(value, field_name, dates):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{field_name} must be a mapping")

    unknown = set(value) - {"show_types", "showtimes", "seats"}
    if unknown:
        raise ConfigError(f"{field_name} contains unknown fields: {', '.join(sorted(unknown))}")

    filters = {}
    show_types = normalize_theatres(value.get("show_types"), f"{field_name}.show_types")
    if show_types:
        filters["show_types"] = show_types

    raw_showtimes = value.get("showtimes")
    if raw_showtimes is not None:
        if not isinstance(raw_showtimes, dict):
            raise ConfigError(f"{field_name}.showtimes must be a mapping")
        unknown_times = set(raw_showtimes) - {"after", "before"}
        if unknown_times:
            raise ConfigError(
                f"{field_name}.showtimes contains unknown fields: {', '.join(sorted(unknown_times))}"
            )
        showtimes = {}
        for key in ("after", "before"):
            raw_time = raw_showtimes.get(key)
            if raw_time is None:
                continue
            if not isinstance(raw_time, str):
                raise ConfigError(f"{field_name}.showtimes.{key} must use HH:MM")
            try:
                time_to_minutes(raw_time)
            except ValueError as exc:
                raise ConfigError(f"{field_name}.showtimes.{key} must use HH:MM") from exc
            showtimes[key] = raw_time
        if showtimes:
            filters["showtimes"] = showtimes

    raw_seats = value.get("seats")
    if raw_seats is not None:
        if not dates:
            raise ConfigError(f"{field_name}.seats requires explicit dates")
        if not isinstance(raw_seats, dict):
            raise ConfigError(f"{field_name}.seats must be a mapping")
        unknown_seats = set(raw_seats) - {"position", "count", "categories"}
        if unknown_seats:
            raise ConfigError(
                f"{field_name}.seats contains unknown fields: {', '.join(sorted(unknown_seats))}"
            )
        position = raw_seats.get("position", "backmost")
        if position != "backmost":
            raise ConfigError(f"{field_name}.seats.position must be 'backmost'")
        count = raw_seats.get("count", 1)
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 6:
            raise ConfigError(f"{field_name}.seats.count must be an integer from 1 to 6")
        filters["seats"] = {
            "position": position,
            "count": count,
            "categories": normalize_theatres(
                raw_seats.get("categories"),
                f"{field_name}.seats.categories",
            ),
        }

    if filters and not dates:
        raise ConfigError(f"{field_name} requires explicit dates")
    return filters


def load_config():
    """Load config from config.yml, with env var overrides."""
    config = {}
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            config = yaml.safe_load(f) or {}
    if not isinstance(config, dict):
        raise ConfigError("config.yml must contain a YAML mapping")

    # Use env var only when non-empty; on scheduled GitHub Actions runs the
    # workflow injects CITY="" (empty), which must NOT override config.yml.
    city = os.getenv("CITY") or config.get("city", "")
    if not isinstance(city, str) or not city.strip():
        raise ConfigError("city is required and must be a string")
    city = city.strip()

    # Build movies list — support both old single-movie and new multi-movie format
    env_movie = os.getenv("MOVIE", "")
    if env_movie:
        raw_movies = [{
            "name": env_movie.strip(),
            "dates": [d.strip() for d in os.getenv("TARGET_DATE", "").split(",") if d.strip()],
            "theatres": [t.strip() for t in os.getenv("THEATRES", "").split(",") if t.strip()],
        }]
    elif "movies" in config:
        raw_movies = config["movies"]
    elif "movie" in config:
        raw_movies = [{
            "name": config.get("movie", ""),
            "dates": config.get("dates") or config.get("date", []),
            "theatres": config.get("theatres", []),
        }]
    else:
        raw_movies = []

    if not isinstance(raw_movies, list):
        raise ConfigError("movies must be a list")
    if not raw_movies:
        raise ConfigError("at least one movie is required")

    movies = []
    for index, movie in enumerate(raw_movies):
        field_prefix = f"movies[{index}]"
        if not isinstance(movie, dict):
            raise ConfigError(f"{field_prefix} must be a mapping")

        name = movie.get("name", "")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"{field_prefix}.name is required and must be a string")

        raw_dates = movie.get("dates")
        if not raw_dates and movie.get("date"):
            raw_dates = movie["date"]
        dates = normalize_dates(raw_dates, f"{field_prefix}.dates")
        movies.append({
            "name": name.strip(),
            "dates": dates,
            "theatres": normalize_theatres(movie.get("theatres"), f"{field_prefix}.theatres"),
            "filters": normalize_filters(movie.get("filters"), f"{field_prefix}.filters", dates),
        })

    raw_smtp_port = os.getenv("SMTP_PORT") or "587"
    try:
        smtp_port = int(raw_smtp_port)
    except ValueError as exc:
        raise ConfigError(f"SMTP_PORT must be an integer, got {raw_smtp_port!r}") from exc
    if not 1 <= smtp_port <= 65535:
        raise ConfigError("SMTP_PORT must be between 1 and 65535")

    return {
        "city": city,
        "movies": movies,
        # `or` (not getenv default) so empty env vars fall back to the defaults
        "smtp_server": os.getenv("SMTP_SERVER") or "smtp.gmail.com",
        "smtp_port": smtp_port,
        "smtp_user": os.getenv("SMTP_USER", ""),
        "smtp_password": os.getenv("SMTP_PASSWORD", ""),
        "notify_email": os.getenv("NOTIFY_EMAIL", ""),
    }


# --- BMS Functions ---

# Recent, realistic browser fingerprints. We rotate through these across retries
# so a Cloudflare block on one TLS/JA3 signature can be retried with a different
# one — the BMS edge intermittently 403s datacenter IPs (e.g. GitHub Actions)
# and a fresh fingerprint is what occasionally slips through. Ordered with the
# fingerprints observed to pass BMS's edge most reliably first.
IMPERSONATE_TARGETS = [
    "chrome136", "chrome142", "chrome146", "chrome131",
    "edge101", "firefox144", "safari180",
]

# BookMyShow ships this client-side key in its web bundle to decode the private
# GETSEATLAYOUT response. The endpoint and key are undocumented and may change.
SEAT_LAYOUT_ENCRYPTION_KEY = b"kYp3s6v9y$B&E)H+MbQeThWmZq4t7w!z"
SEAT_LAYOUT_ENDPOINT = "https://services-in.bookmyshow.com/doTrans.aspx"

# Headers a real browser sends for a top-level navigation. curl_cffi handles the
# TLS fingerprint and UA, but these make the request look fully browser-like.
BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Referer": "https://in.bookmyshow.com/",
}


def fetch_page(url, max_retries=8):
    """Fetch a BMS page, retrying past intermittent Cloudflare 403s.

    BMS's Cloudflare intermittently blocks datacenter IPs (like GitHub Actions)
    with a 403 regardless of how browser-like the request is — the block is
    IP-reputation based and probabilistic, not rate based. So each attempt
    rotates to a different browser fingerprint with a fresh session (that's what
    occasionally gets through), and delays are kept short since waiting longer
    does nothing for an IP block.
    """
    for attempt in range(1, max_retries + 1):
        target = IMPERSONATE_TARGETS[(attempt - 1) % len(IMPERSONATE_TARGETS)]
        rate_limited = False
        try:
            session = requests.Session(impersonate=target)
            r = session.get(url, headers=BROWSER_HEADERS, timeout=20)
            if r.status_code == 200 and r.text:
                return r.text
            log.warning(f"Attempt {attempt}/{max_retries} [{target}]: HTTP {r.status_code}")
            rate_limited = r.status_code == 429
        except Exception as e:
            log.warning(f"Attempt {attempt}/{max_retries} [{target}]: {e}")

        if attempt < max_retries:
            # IP block isn't time-based → retry quickly with a new fingerprint.
            # A 429 is a genuine rate-limit, so pause a bit longer for that.
            delay = random.uniform(15, 25) if rate_limited else random.uniform(4, 8)
            time.sleep(delay)

    raise FetchError(f"All {max_retries} attempts failed for {url}")


def slugify(name):
    """Convert movie name to URL slug."""
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')


def normalize_title(title):
    return " ".join(re.findall(r"[a-z0-9]+", title.lower()))


def movie_title_matches(expected_title, page_title):
    """Match the complete expected title as a normalized phrase."""
    expected = normalize_title(expected_title)
    actual = normalize_title(page_title)
    return bool(expected) and re.search(rf"(?:^| ){re.escape(expected)}(?: |$)", actual) is not None


def decode_seat_layout(encoded_layout):
    """Decode the AES-CBC seat layout returned by BookMyShow's web client API."""
    if "||" in encoded_layout:
        return encoded_layout

    try:
        encrypted = base64.b64decode(encoded_layout, validate=True)
        decryptor = Cipher(
            algorithms.AES(SEAT_LAYOUT_ENCRYPTION_KEY),
            modes.CBC(bytes(16)),
        ).decryptor()
        padded = decryptor.update(encrypted) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        decoded = (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise FetchError("BookMyShow seat layout could not be decoded") from exc

    if "||" not in decoded:
        raise FetchError("BookMyShow returned an unknown seat layout format")
    return decoded


def extract_initial_state(html):
    marker = "window.__INITIAL_STATE__ = "
    try:
        start = html.index(marker) + len(marker)
        state, _ = json.JSONDecoder().raw_decode(html[start:])
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("BookMyShow initial state was not found") from exc
    if not isinstance(state, dict):
        raise ValueError("BookMyShow initial state is invalid")
    return state


def iter_venue_cards(value):
    if isinstance(value, dict):
        if value.get("type") == "venue-card":
            yield value
            return
        for child in value.values():
            yield from iter_venue_cards(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_venue_cards(child)


def extract_show_sessions(html, target_date):
    """Extract venue/session records from the structured showtime page state."""
    state = extract_initial_state(html)
    date_data = (
        state.get("showtimesByEvent", {})
        .get("showDates", {})
        .get(target_date, {})
    )
    static_venues = (
        date_data.get("primaryStatic", {})
        .get("data", {})
        .get("venues", {})
    )
    widgets = (
        date_data.get("dynamic", {})
        .get("data", {})
        .get("showtimeWidgets", [])
    )

    sessions = []
    seen = set()
    for card in iter_venue_cards(widgets):
        card_data = card.get("additionalData", {})
        venue_code = card_data.get("venueCode", "")
        venue = static_venues.get(venue_code, {})
        venue_name = card_data.get("venueName") or venue.get("venueName", "")
        for show in card.get("showtimes", []):
            show_data = show.get("additionalData", {})
            session_id = str(show_data.get("sessionId", ""))
            identity = (venue_code, session_id)
            if not venue_code or not session_id or identity in seen:
                continue
            seen.add(identity)
            sessions.append({
                "venue_code": venue_code,
                "venue_name": venue_name,
                "session_id": session_id,
                "show_time": show_data.get("showTime") or show.get("title", ""),
                "show_time_code": str(show_data.get("showTimeCode", "")),
                "attributes": show_data.get("attributes") or show.get("screenAttr", ""),
                "categories": show_data.get("categories", []),
                "show_seat_number": venue.get("showSeatNo", "Y"),
            })
    return sessions


def seat_layout_form(session_info):
    fields = {
        "strAppCode": "WEB",
        "lngTransactionIdentifier": "0",
        "strCommand": "GETSEATLAYOUT",
        "strVenueCode": session_info["venue_code"],
        "strParam1": session_info["session_id"],
        "strParam2": "WEB",
        "strParam3": "",
        "strParam4": "",
        "strParam5": session_info.get("show_seat_number", "Y"),
        "strParam6": "N",
        "strParam7": "N",
        "strParam8": "",
        "strParam9": "",
        "strParam10": "",
        "strFormat": "json",
    }
    return CurlMime.from_list([
        {"name": name, "data": str(value).encode()}
        for name, value in fields.items()
    ])


def enrich_layout_categories(layout, categories):
    by_code = {}
    for category in categories:
        area_code = category.get("areaCatCode") or category.get("AreaCatCode")
        if area_code:
            by_code[str(area_code)] = category

    for area in layout["areas"].values():
        category = by_code.get(area["code"])
        if not category:
            continue
        area["name"] = category.get("priceDesc") or category.get("PriceDesc") or area["name"]
        area["price"] = category.get("curPrice") or category.get("CurPrice")
    return layout


def fetch_seat_layout(session, session_info, max_retries=3):
    """Fetch and parse one session's private BookMyShow seat layout."""
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://in.bookmyshow.com",
        "Referer": "https://in.bookmyshow.com/",
        "x-app-code": "WEB",
        "x-bms-id": f"1.{random.randrange(1_000_000_000)}.{random.randrange(10_000_000_000_000)}",
    }
    last_error = None

    for attempt in range(1, max_retries + 1):
        multipart = seat_layout_form(session_info)
        try:
            response = session.post(
                SEAT_LAYOUT_ENDPOINT,
                multipart=multipart,
                headers=headers,
                timeout=30,
            )
            if response.status_code != 200:
                raise FetchError(f"HTTP {response.status_code}")

            envelope = response.json().get("BookMyShow", {})
            if envelope.get("blnSuccess") != "true":
                raise FetchError(envelope.get("strException") or "seat layout request failed")
            encoded_layout = envelope.get("strData")
            if not isinstance(encoded_layout, str) or not encoded_layout:
                raise FetchError("seat layout response contained no data")
            layout = parse_seat_layout(decode_seat_layout(encoded_layout))
            return enrich_layout_categories(layout, session_info.get("categories", []))
        except Exception as exc:
            last_error = exc
            log.warning(
                f"Seat layout attempt {attempt}/{max_retries} failed for "
                f"{session_info['venue_code']}/{session_info['session_id']}: {exc}"
            )
        finally:
            multipart.close()

        if attempt < max_retries:
            time.sleep(random.uniform(2, 4))

    raise FetchError(
        f"Seat layout failed for {session_info['venue_code']}/{session_info['session_id']}: "
        f"{last_error}"
    )


def time_to_minutes(value):
    value = value.strip()
    if re.fullmatch(r"\d{4}", value):
        hour, minute = int(value[:2]), int(value[2:])
    elif re.fullmatch(r"\d{2}:\d{2}", value):
        hour, minute = map(int, value.split(":"))
    else:
        parsed = datetime.datetime.strptime(value.upper(), "%I:%M %p")
        hour, minute = parsed.hour, parsed.minute
    if hour > 23 or minute > 59:
        raise ValueError(f"Invalid showtime: {value}")
    return hour * 60 + minute


def session_matches_filters(session, filters):
    show_types = filters.get("show_types", [])
    attributes = session.get("attributes", "").casefold()
    if show_types and not any(show_type.casefold() in attributes for show_type in show_types):
        return False

    time_filters = filters.get("showtimes", {})
    after = time_filters.get("after")
    before = time_filters.get("before")
    if not after and not before:
        return True

    showtime = session.get("show_time_code") or session.get("show_time", "")
    show_minutes = time_to_minutes(showtime)
    after_minutes = time_to_minutes(after) if after else 0
    before_minutes = time_to_minutes(before) if before else 24 * 60 - 1
    if after_minutes <= before_minutes:
        return after_minutes <= show_minutes <= before_minutes
    return show_minutes >= after_minutes or show_minutes <= before_minutes


def parse_seat_layout(layout):
    """Parse BookMyShow's compact area/row/seat layout string."""
    try:
        area_section, row_section = layout.split("||", 1)
    except ValueError as exc:
        raise ValueError("Invalid BookMyShow seat layout") from exc

    areas = {}
    for raw_area in filter(None, area_section.split("|")):
        fields = raw_area.split(":")
        if len(fields) < 4:
            raise ValueError("Invalid BookMyShow seat category")
        name, area_id, area_code, area_number = fields[:4]
        areas[area_id] = {
            "name": name,
            "id": area_id,
            "code": area_code,
            "number": area_number,
        }

    rows = []
    for raw_row in filter(None, row_section.split("|")):
        fields = raw_row.split(":")
        if len(fields) < 2:
            raise ValueError("Invalid BookMyShow seat row")
        row_id, row_label, *raw_seats = fields
        seats = []
        for position, raw_seat in enumerate(raw_seats):
            parts = raw_seat.split("+")
            seat_code = parts[0]
            if len(seat_code) < 2 or not seat_code[1].isdigit():
                raise ValueError("Invalid BookMyShow seat token")
            display_number = parts[1] if len(parts) > 1 and parts[1] not in {"0", "00"} else None
            seats.append({
                "area_id": seat_code[0],
                "status": int(seat_code[1]),
                "seat_number": seat_code[2:],
                "display_number": display_number,
                "position": position,
            })
        rows.append({"id": row_id, "label": row_label, "seats": seats})

    return {"areas": areas, "rows": rows}


def seat_display_label(row_label, seat):
    number = seat["display_number"] or seat["seat_number"].lstrip("0")
    if not number:
        return row_label
    if number.casefold().startswith(row_label.casefold()):
        return number
    return f"{row_label}{number}"


def select_backmost_seats(layout, count=1, categories=None):
    """Select a central contiguous block from the backmost eligible physical row."""
    if count < 1:
        raise ValueError("Seat count must be at least 1")

    allowed_categories = {
        category.strip().casefold() for category in (categories or []) if category.strip()
    }
    areas = layout["areas"]

    def area_allowed(area_id):
        if not allowed_categories:
            return True
        area = areas.get(area_id, {})
        return area.get("name", "").strip().casefold() in allowed_categories

    eligible_rows = []
    for row in layout["rows"]:
        physical_seats = [
            seat for seat in row["seats"]
            if seat["status"] != 0 and area_allowed(seat["area_id"])
        ]
        if physical_seats:
            eligible_rows.append((row, physical_seats))

    if not eligible_rows:
        return None

    # BMS renders rows in payload order, then places the screen below the final row.
    row, physical_seats = eligible_rows[0]
    candidates = []
    run = []
    for seat in row["seats"]:
        is_available = seat["status"] in {1, 4} and area_allowed(seat["area_id"])
        if not is_available or (run and run[-1]["area_id"] != seat["area_id"]):
            run = []
        if is_available:
            run.append(seat)
            if len(run) >= count:
                candidates.append(run[-count:])

    if not candidates:
        return None

    row_center = (physical_seats[0]["position"] + physical_seats[-1]["position"]) / 2
    candidates.sort(key=lambda block: (
        abs(sum(seat["position"] for seat in block) / len(block) - row_center),
        block[0]["position"],
    ))
    selected = candidates[0]
    area = areas.get(selected[0]["area_id"], {})
    return {
        "row": row["label"],
        "category": area.get("name", selected[0]["area_id"]),
        "area_id": selected[0]["area_id"],
        "seats": [seat_display_label(row["label"], seat) for seat in selected],
    }


def iter_item_list_entries(value):
    if isinstance(value, dict):
        if value.get("@type") == "ItemList":
            yield from value.get("itemListElement", [])
        for child in value.values():
            yield from iter_item_list_entries(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_item_list_entries(child)


def serialize_state(state):
    return json.dumps(state, sort_keys=True, separators=(",", ":"))


def save_state(state_file, state):
    """Atomically persist a notification snapshot."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    temp_file = state_file.with_name(f"{state_file.name}.{os.getpid()}.tmp")
    try:
        temp_file.write_text(serialize_state(state))
        temp_file.replace(state_file)
    finally:
        temp_file.unlink(missing_ok=True)


def load_state(state_file, current_state):
    if not state_file.exists():
        return {}

    raw_state = state_file.read_text().strip()
    try:
        state = json.loads(raw_state)
        if isinstance(state, dict) and all(isinstance(values, list) for values in state.values()):
            return state
    except json.JSONDecodeError:
        pass

    # Migrate hashes written by older versions without repeating an unchanged alert.
    legacy_hash = hashlib.md5(serialize_state(current_state).encode()).hexdigest()
    if raw_state == legacy_hash:
        return current_state

    log.warning(f"Ignoring invalid state file: {state_file}")
    return {}


def find_additions(previous_state, current_state):
    """Return only newly added values, including newly seen empty-valued keys."""
    additions = {}
    for key, current_values in current_state.items():
        if key not in previous_state and not current_values:
            additions[key] = []
            continue

        previous_values = set(previous_state.get(key, []))
        new_values = sorted(set(current_values) - previous_values)
        if new_values:
            additions[key] = new_values
    return additions


def process_state_change(state_file, current_state, notify):
    """Notify about additions and commit state only after successful delivery."""
    previous_state = load_state(state_file, current_state)
    additions = find_additions(previous_state, current_state)
    if additions and not notify(additions):
        return False

    if not additions:
        log.info("No new additions since last check.")

    save_state(state_file, current_state)
    return True


def discover_movie(movie_name, city_slug):
    """Find the movie on BMS and return its URL and primary event code."""
    url = f"https://in.bookmyshow.com/explore/movies-{city_slug}"
    log.info(f"Searching for '{movie_name}' in {city_slug}...")

    # Route through fetch_page so Cloudflare 403s get retried with a ~1-min
    # cooldown instead of giving up on the first block.
    html = fetch_page(url)

    scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
    expected_title = normalize_title(movie_name)
    candidates = []

    for s in scripts:
        try:
            data = json.loads(s)
        except (json.JSONDecodeError, TypeError):
            continue

        for entry in iter_item_list_entries(data):
            if not isinstance(entry, dict):
                continue
            item = entry.get("item", entry)
            if not isinstance(item, dict):
                continue

            candidate_title = normalize_title(str(item.get("name", "")))
            movie_url = str(item.get("url", ""))
            if not candidate_title or not movie_url:
                continue

            if candidate_title == expected_title:
                score = 0
            elif movie_title_matches(movie_name, candidate_title):
                score = 1
            elif movie_title_matches(candidate_title, movie_name):
                score = 2
            else:
                continue
            candidates.append((score, abs(len(candidate_title) - len(expected_title)), item))

    if candidates:
        _, _, item = min(candidates, key=lambda candidate: candidate[:2])
        movie_url = str(item["url"])
        ec_match = re.search(r'(ET\d+)', movie_url)
        slug_match = re.search(r'/movies/[^/]+/([^/]+)', movie_url)
        log.info(f"Found: {item.get('name')} -> {movie_url}")
        return {
            "name": item.get("name"),
            "url": movie_url,
            "event_code": ec_match.group(1) if ec_match else None,
            "slug": slug_match.group(1) if slug_match else slugify(movie_name),
        }

    return None


def discover_event_codes(movie_url, movie_name):
    """Get event codes specific to this movie only (not other movies on the page)."""
    log.info("Discovering event codes...")
    # Route through fetch_page so 403s get the retry + ~1-min cooldown.
    html = fetch_page(movie_url)

    movie_slug = re.search(r'/movies/([^/]+)/', movie_url)
    if movie_slug:
        path_parts = movie_url.split("/movies/", 1)[1].split("/")
        slug = path_parts[1] if len(path_parts) > 1 else slugify(movie_name)
    else:
        slug = slugify(movie_name)

    primary_codes = re.findall(r'(ET\d{8,})', movie_url, re.IGNORECASE)
    slug_codes = re.findall(
        rf'{re.escape(slug)}[^\"\'<>\s]*?(ET\d{{8,}})',
        html,
        re.IGNORECASE,
    )

    all_codes = list(dict.fromkeys(primary_codes + slug_codes))
    log.info(f"Found {len(all_codes)} movie-specific event codes")
    return all_codes


def check_showtimes(city, movie_entry, event_codes, movie_slug):
    """Check all event codes for showtimes at preferred theatres."""
    city_slug = slugify(city)
    target_date = movie_entry["date"]
    theatres = [t.strip().lower() for t in movie_entry.get("theatres", []) if t.strip()]
    watch_all = len(theatres) == 0  # No theatres specified = watch all
    filters = movie_entry.get("filters", {})
    seat_filters = filters.get("seats")
    seat_session = requests.Session(impersonate=random.choice(IMPERSONATE_TARGETS)) if seat_filters else None
    seat_request_count = 0
    matched = {}

    for i, code in enumerate(event_codes):
        if i > 0:
            delay = random.uniform(4, 8)
            log.info(f"Waiting {delay:.0f}s before next request...")
            time.sleep(delay)

        url = f"https://in.bookmyshow.com/movies/{city_slug}/{movie_slug}/buytickets/{code}/{target_date}"
        html = fetch_page(url)

        # Verify movie
        title_match = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE)
        if title_match and not movie_title_matches(movie_entry["name"], title_match.group(1)):
            log.warning(f"{code}: Wrong movie")
            continue

        # Verify date
        show_dates = re.findall(r'"showDate":"(\d{8})"', html)
        if show_dates and show_dates[0] != target_date:
            log.info(f"{code}: Data is for {show_dates[0]}, not {target_date} — skipping")
            continue

        if filters:
            try:
                sessions = extract_show_sessions(html, target_date)
            except ValueError as exc:
                raise FetchError(f"{code}: structured showtime data could not be parsed") from exc

            unique_venues = list(dict.fromkeys(
                session["venue_name"] for session in sessions if session["venue_name"]
            ))
            for show in sessions:
                venue = show["venue_name"]
                if not venue or (not watch_all and not any(t in venue.lower() for t in theatres)):
                    continue
                if not session_matches_filters(show, filters):
                    continue

                details = [show["show_time"]]
                if show["attributes"]:
                    details.append(show["attributes"])

                if seat_filters:
                    if seat_request_count:
                        time.sleep(random.uniform(1, 2))
                    layout = fetch_seat_layout(seat_session, show)
                    seat_request_count += 1
                    seat_match = select_backmost_seats(
                        layout,
                        count=seat_filters["count"],
                        categories=seat_filters["categories"],
                    )
                    if not seat_match:
                        continue
                    details.extend([
                        f"{seat_match['category']} row {seat_match['row']}",
                        ", ".join(seat_match["seats"]),
                    ])

                matched.setdefault(venue, set()).add(" · ".join(details))

            for venue, results in matched.items():
                log.info(f"{code}: {venue} -> {', '.join(sorted(results))}")
            if not watch_all and not any(any(t in venue.lower() for t in theatres) for venue in unique_venues):
                log.info(f"{code}: {len(unique_venues)} theatres, none matched")
            continue

        # Extract venues and showtimes
        venue_splits = re.split(r'(?="venueName")', html)
        for block in venue_splits:
            name_match = re.search(r'"venueName":"([^"]+)"', block)
            if not name_match:
                continue
            venue = name_match.group(1)
            if not watch_all and not any(t in venue.lower() for t in theatres):
                continue

            found_times = re.findall(r'"showTime":"([^"]+)"', block)
            if found_times:
                if venue not in matched:
                    matched[venue] = set()
                matched[venue].update(found_times)

        # Check unique venues list too
        venues = re.findall(r'"venueName":"([^"]+)"', html)
        unique_venues = list(dict.fromkeys(venues))
        for venue in unique_venues:
            if watch_all or any(t in venue.lower() for t in theatres):
                if venue not in matched:
                    matched[venue] = set()

        # Log results
        for venue, times in matched.items():
            log.info(f"{code}: {venue} -> {', '.join(sorted(times)) if times else '(no times in server data)'}")

        if not watch_all and not any(any(t in v.lower() for t in theatres) for v in unique_venues):
            log.info(f"{code}: {len(unique_venues)} theatres, none matched")

    return {k: sorted(v) for k, v in matched.items()}


def check_availability(city, movie_entry, event_codes, movie_slug):
    """Check whether booking has opened for a movie (no specific date).

    Used for "notify whenever shows come up" movies. Fetches the buytickets
    URL without a date appended — BMS serves the earliest available date once
    booking is open. Returns a dict mapping show dates (YYYYMMDD) to a sorted
    list of venues that currently have shows (filtered by preferred theatres if
    any were given).
    """
    city_slug = slugify(city)
    theatres = [t.strip().lower() for t in movie_entry.get("theatres", []) if t.strip()]
    watch_all = len(theatres) == 0  # No theatres specified = watch all
    found = {}

    for i, code in enumerate(event_codes):
        if i > 0:
            delay = random.uniform(4, 8)
            log.info(f"Waiting {delay:.0f}s before next request...")
            time.sleep(delay)

        # No date in the URL — BMS redirects to the earliest open date if booking is live
        url = f"https://in.bookmyshow.com/movies/{city_slug}/{movie_slug}/buytickets/{code}"
        html = fetch_page(url)

        # Verify movie
        title_match = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE)
        if title_match and not movie_title_matches(movie_entry["name"], title_match.group(1)):
            log.warning(f"{code}: Wrong movie")
            continue

        show_dates = re.findall(r'"showDate":"(\d{8})"', html)
        if not show_dates:
            log.info(f"{code}: No shows open yet.")
            continue

        primary_date = show_dates[0]

        matched_venues = set()
        for block in re.split(r'(?="venueName")', html):
            name_match = re.search(r'"venueName":"([^"]+)"', block)
            if not name_match:
                continue
            venue = name_match.group(1)
            if not watch_all and not any(t in venue.lower() for t in theatres):
                continue
            matched_venues.add(venue)

        # Notify when watching all theatres (even if venue parsing is empty),
        # or when at least one preferred theatre has a show.
        if watch_all or matched_venues:
            found.setdefault(primary_date, set()).update(matched_venues)
            log.info(f"{code}: shows open for {primary_date}")

    return {d: sorted(v) for d, v in found.items()}


def html_to_text(body):
    text = re.sub(r"<br\s*/?>", "\n", body, flags=re.IGNORECASE)
    text = re.sub(r"</(?:p|h\d)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return unescape(text).strip()


def send_email(config, subject, body):
    if not all([config["smtp_user"], config["smtp_password"], config["notify_email"]]):
        log.warning(f"Email not configured. SUBJECT: {subject}")
        return False

    msg = MIMEMultipart("alternative")
    msg["From"] = config["smtp_user"]
    msg["To"] = config["notify_email"]
    msg["Subject"] = subject
    msg.attach(MIMEText(html_to_text(body), "plain"))
    msg.attach(MIMEText(body, "html"))

    try:
        tls_context = ssl.create_default_context()
        with smtplib.SMTP(config["smtp_server"], config["smtp_port"], timeout=30) as server:
            server.starttls(context=tls_context)
            server.login(config["smtp_user"], config["smtp_password"])
            server.sendmail(config["smtp_user"], config["notify_email"], msg.as_string())
        log.info("Email sent!")
        return True
    except Exception as e:
        log.error(f"Email failed: {e}")
        return False


def main():
    configure_logging()
    try:
        config = load_config()
    except ConfigError as exc:
        log.error(f"Invalid configuration: {exc}")
        return 2

    city = config["city"]
    city_slug = slugify(city)
    had_errors = False

    for movie_entry in config["movies"]:
        movie_name = movie_entry.get("name", "")
        dates = movie_entry.get("dates", [])
        theatres = movie_entry.get("theatres", [])
        filters = movie_entry.get("filters", {})

        if not movie_name:
            log.error(f"Skipping entry — name is required: {movie_entry}")
            continue

        # Step 1: Find the movie (once per movie, reuse across dates)
        try:
            movie = discover_movie(movie_name, city_slug)
        except FetchError as exc:
            log.error(exc)
            had_errors = True
            continue
        if not movie:
            log.info(f"'{movie_name}' not listed on BookMyShow in {city}.")
            continue

        # Step 2: Get movie-specific event codes (once per movie)
        try:
            all_codes = discover_event_codes(movie["url"], movie_name)
        except FetchError as exc:
            log.error(exc)
            had_errors = True
            continue
        if not all_codes:
            all_codes = [movie["event_code"]] if movie.get("event_code") else []
        if not all_codes:
            log.error("No event codes found.")
            continue

        # No dates given → "notify whenever shows come up" mode
        if not dates:
            log.info(f"--- {movie_name} (availability watch) ---")
            if theatres:
                log.info(f"Watching: {', '.join(theatres)}")
            else:
                log.info("Watching: all theatres")

            state_file = Path(__file__).parent / f".state_{slugify(movie_name)}_any"
            avail_entry = {"name": movie_name, "theatres": theatres}
            try:
                available = check_availability(city, avail_entry, all_codes, movie["slug"])
            except FetchError as exc:
                log.error(exc)
                had_errors = True
                continue

            if not available:
                log.info("No shows open yet.")
                save_state(state_file, {})
                continue

            def notify_availability(additions):
                date_html = ""
                for date in sorted(additions):
                    display_date = f"{date[6:8]}/{date[4:6]}/{date[:4]}"
                    venues = additions[date]
                    venue_text = ', '.join(venues) if venues else 'Booking open — check BookMyShow for theatres'
                    log.info(f"✓ NEW: {display_date}: {venue_text}")
                    venue_html = ', '.join(escape(venue) for venue in venues) if venues else venue_text
                    date_html += f"<p><strong>{display_date}</strong><br>{venue_html}</p>"

                return send_email(
                    config,
                    f"🎬 {movie_name} — booking is open!",
                    f"""
                    <h2>🎬 {escape(movie_name)} — shows are now open!</h2>
                    {date_html}
                    <p><a href="{escape(movie['url'], quote=True)}">👉 Book on BookMyShow</a></p>
                    """
                )

            if not process_state_change(state_file, available, notify_availability):
                had_errors = True
            continue

        # Step 3: Check each date
        for target_date in dates:
            target_date = str(target_date)
            display_date = f"{target_date[6:8]}/{target_date[4:6]}/{target_date[:4]}"
            state_file = Path(__file__).parent / f".state_{slugify(movie_name)}_{target_date}"

            log.info(f"--- {movie_name} ({display_date}) ---")
            if theatres:
                log.info(f"Watching: {', '.join(theatres)}")
            else:
                log.info("Watching: all theatres")

            # Fetch, validate, and parse each event code once for this date.
            date_entry = {
                "name": movie_name,
                "date": target_date,
                "theatres": theatres,
                "filters": filters,
            }
            log.info(f"Checking {len(all_codes)} event codes...")
            try:
                matched = check_showtimes(city, date_entry, all_codes, movie["slug"])
            except FetchError as exc:
                log.error(exc)
                had_errors = True
                continue

            if not matched:
                log.info(f"No shows at your theatres for {display_date} yet.")
                save_state(state_file, {})
                continue

            def notify_showtimes(additions):
                theatre_html = ""
                for theatre, times in additions.items():
                    time_text = ', '.join(times) if times else 'Show added — check BookMyShow for times'
                    log.info(f"✓ NEW: {theatre}: {time_text}")
                    safe_times = ', '.join(escape(show_time) for show_time in times) if times else time_text
                    theatre_html += f"<p><strong>{escape(theatre)}</strong><br>{safe_times}</p>"

                return send_email(
                    config,
                    f"🎬 {movie_name} — NEW shows! ({display_date})",
                    f"""
                    <h2>🎬 {escape(movie_name)} — shows for {display_date}!</h2>
                    {theatre_html}
                    <p><a href="{escape(movie['url'], quote=True)}">👉 Book on BookMyShow</a></p>
                    """
                )

            if not process_state_change(state_file, matched, notify_showtimes):
                had_errors = True

    return 1 if had_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
