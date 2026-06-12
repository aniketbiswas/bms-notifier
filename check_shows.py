import os
import re
import json
import smtplib
import datetime
import hashlib
import logging
import time
import random
import yaml
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

from curl_cffi import requests

# --- Logging ---
LOG_FILE = Path(__file__).parent / "bms.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("bms")

# --- Load config ---
CONFIG_FILE = Path(__file__).parent / "config.yml"
STATE_FILE = Path(__file__).parent / ".last_state"


def load_config():
    """Load config from config.yml, with env var overrides."""
    config = {}
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            config = yaml.safe_load(f) or {}

    # Use env var only when non-empty; on scheduled GitHub Actions runs the
    # workflow injects CITY="" (empty), which must NOT override config.yml.
    city = os.getenv("CITY") or config.get("city", "")

    # Build movies list — support both old single-movie and new multi-movie format
    env_movie = os.getenv("MOVIE", "")
    if env_movie:
        # Env var override: single movie
        dates = [d.strip() for d in os.getenv("TARGET_DATE", "").split(",") if d.strip()]
        movies = [{
            "name": env_movie,
            "dates": dates,
            "theatres": [t.strip() for t in os.getenv("THEATRES", "").split(",") if t.strip()],
        }]
    elif "movies" in config:
        # Multi-movie format
        movies = []
        for m in config["movies"]:
            # Support both "date" (string) and "dates" (array)
            dates = m.get("dates", [])
            if not dates and m.get("date"):
                dates = [m["date"]]
            movies.append({
                "name": m.get("name", ""),
                "dates": [str(d) for d in dates],
                "theatres": m.get("theatres", []),
            })
    elif "movie" in config:
        # Old single-movie format (backwards compatible)
        dates = config.get("dates", [])
        if not dates and config.get("date"):
            dates = [config["date"]]
        movies = [{
            "name": config.get("movie", ""),
            "dates": [str(d) for d in dates],
            "theatres": config.get("theatres", []),
        }]
    else:
        movies = []

    return {
        "city": city,
        "movies": movies,
        # `or` (not getenv default) so empty env vars fall back to the defaults
        "smtp_server": os.getenv("SMTP_SERVER") or "smtp.gmail.com",
        "smtp_port": int(os.getenv("SMTP_PORT") or "587"),
        "smtp_user": os.getenv("SMTP_USER", ""),
        "smtp_password": os.getenv("SMTP_PASSWORD", ""),
        "notify_email": os.getenv("NOTIFY_EMAIL", ""),
    }


# --- BMS Functions ---

# Recent, realistic browser fingerprints. We rotate through these across retries
# so a Cloudflare block on one TLS/JA3 signature can be retried with a different
# one — the BMS edge intermittently 403s datacenter IPs (e.g. GitHub Actions)
# and a fresh fingerprint is what occasionally slips through.
IMPERSONATE_TARGETS = [
    "chrome131", "chrome136", "chrome142", "chrome146",
    "edge101", "firefox144", "safari180",
]

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


def new_session(target=None):
    """Create a curl_cffi session impersonating a (random) recent browser."""
    return requests.Session(impersonate=target or random.choice(IMPERSONATE_TARGETS))


def get_session():
    return new_session()


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
            if r.status_code == 200:
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

    log.error(f"All {max_retries} attempts failed")
    return None


def slugify(name):
    """Convert movie name to URL slug."""
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')


def discover_movie(session, movie_name, city_slug):
    """Find the movie on BMS and return its URL and primary event code."""
    url = f"https://in.bookmyshow.com/explore/movies-{city_slug}"
    log.info(f"Searching for '{movie_name}' in {city_slug}...")

    # Route through fetch_page so Cloudflare 403s get retried with a ~1-min
    # cooldown instead of giving up on the first block.
    html = fetch_page(url)
    if not html:
        log.error("Movies page unavailable after retries")
        return None

    scripts = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
    search_term = movie_name.lower()

    for s in scripts:
        try:
            data = json.loads(s)
            if isinstance(data, dict) and data.get("@type") == "ItemList":
                for item in data.get("itemListElement", []):
                    name = item.get("name", "").lower()
                    if search_term in name or name in search_term:
                        movie_url = item.get("url", "")
                        ec_match = re.search(r'(ET\d+)', movie_url)
                        slug_match = re.search(r'/movies/([^/]+)/', movie_url)
                        log.info(f"Found: {item.get('name')} -> {movie_url}")
                        return {
                            "name": item.get("name"),
                            "url": movie_url,
                            "event_code": ec_match.group(1) if ec_match else None,
                            "slug": slug_match.group(1) if slug_match else slugify(movie_name),
                        }
        except (json.JSONDecodeError, TypeError):
            continue

    return None


def discover_event_codes(session, movie_url, movie_name):
    """Get event codes specific to this movie only (not other movies on the page)."""
    log.info("Discovering event codes...")
    # Route through fetch_page so 403s get the retry + ~1-min cooldown.
    html = fetch_page(movie_url)
    if not html:
        return []

    movie_slug = re.search(r'/movies/([^/]+)/', movie_url)
    slug = movie_slug.group(1) if movie_slug else slugify(movie_name)

    # Method 1: Find codes linked with this movie's slug in URLs
    slug_codes = re.findall(rf'{re.escape(slug)}[^"]*?(ET\d{{8,}})', html, re.IGNORECASE)

    # Method 2: Find eventCode fields in Redux JSON
    json_codes = re.findall(r'"eventCode":"(ET\d{8,})"', html)

    # Combine and deduplicate
    all_codes = list(dict.fromkeys(slug_codes + json_codes))
    log.info(f"Found {len(all_codes)} movie-specific event codes")
    return all_codes


def filter_valid_codes(session, all_codes, movie_name, movie_slug, city_slug, target_date):
    """Filter event codes to only those that belong to this movie and have data for target date."""
    valid_codes = []
    search_term = movie_name.lower().split()[0]

    for i, code in enumerate(all_codes):
        if i > 0:
            delay = random.uniform(1, 3)
            time.sleep(delay)

        url = f"https://in.bookmyshow.com/movies/{city_slug}/{movie_slug}/buytickets/{code}/{target_date}"
        try:
            session = new_session()
            r = session.get(url, headers=BROWSER_HEADERS, timeout=15)
            if r.status_code != 200:
                continue

            # Verify movie title
            title_match = re.search(r'<title>(.*?)</title>', r.text, re.IGNORECASE)
            if title_match and search_term not in title_match.group(1).lower():
                continue

            # Verify showDate matches target
            show_dates = re.findall(r'"showDate":"(\d{8})"', r.text)
            if show_dates and show_dates[0] == target_date:
                valid_codes.append(code)
        except Exception:
            continue

    log.info(f"Validated {len(valid_codes)} codes for {target_date}")
    return valid_codes


def check_showtimes(city, movie_entry, event_codes, movie_slug):
    """Check all event codes for showtimes at preferred theatres."""
    city_slug = slugify(city)
    target_date = movie_entry["date"]
    theatres = [t.strip().lower() for t in movie_entry.get("theatres", []) if t.strip()]
    watch_all = len(theatres) == 0  # No theatres specified = watch all
    matched = {}

    for i, code in enumerate(event_codes):
        if i > 0:
            delay = random.uniform(55, 65)
            log.info(f"Waiting {delay:.0f}s before next request...")
            time.sleep(delay)

        url = f"https://in.bookmyshow.com/movies/{city_slug}/{movie_slug}/buytickets/{code}/{target_date}"
        html = fetch_page(url)
        if not html:
            log.error(f"{code}: Failed to fetch")
            continue

        # Verify movie
        search_term = movie_entry["name"].lower().split()[0]
        title_match = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE)
        if title_match and search_term not in title_match.group(1).lower():
            log.warning(f"{code}: Wrong movie")
            continue

        # Verify date
        show_dates = re.findall(r'"showDate":"(\d{8})"', html)
        if show_dates and show_dates[0] != target_date:
            log.info(f"{code}: Data is for {show_dates[0]}, not {target_date} — skipping")
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
            delay = random.uniform(55, 65)
            log.info(f"Waiting {delay:.0f}s before next request...")
            time.sleep(delay)

        # No date in the URL — BMS redirects to the earliest open date if booking is live
        url = f"https://in.bookmyshow.com/movies/{city_slug}/{movie_slug}/buytickets/{code}"
        html = fetch_page(url)
        if not html:
            log.error(f"{code}: Failed to fetch")
            continue

        # Verify movie
        search_term = movie_entry["name"].lower().split()[0]
        title_match = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE)
        if title_match and search_term not in title_match.group(1).lower():
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


def send_email(config, subject, body):
    if not all([config["smtp_user"], config["smtp_password"], config["notify_email"]]):
        log.warning(f"Email not configured. SUBJECT: {subject}")
        return False

    msg = MIMEMultipart()
    msg["From"] = config["smtp_user"]
    msg["To"] = config["notify_email"]
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "html"))

    try:
        with smtplib.SMTP(config["smtp_server"], config["smtp_port"]) as server:
            server.starttls()
            server.login(config["smtp_user"], config["smtp_password"])
            server.sendmail(config["smtp_user"], config["notify_email"], msg.as_string())
        log.info("Email sent!")
        return True
    except Exception as e:
        log.error(f"Email failed: {e}")
        return False


def main():
    config = load_config()

    if not config["city"]:
        log.error("Missing config: city is required. Edit config.yml")
        return
    if not config["movies"]:
        log.error("Missing config: at least one movie is required. Edit config.yml")
        return

    city = config["city"]
    city_slug = slugify(city)
    session = get_session()

    for movie_entry in config["movies"]:
        movie_name = movie_entry.get("name", "")
        dates = movie_entry.get("dates", [])
        theatres = movie_entry.get("theatres", [])

        if not movie_name:
            log.error(f"Skipping entry — name is required: {movie_entry}")
            continue

        # Step 1: Find the movie (once per movie, reuse across dates)
        movie = discover_movie(session, movie_name, city_slug)
        if not movie:
            log.info(f"'{movie_name}' not listed on BookMyShow in {city}.")
            continue

        # Step 2: Get movie-specific event codes (once per movie)
        all_codes = discover_event_codes(session, movie["url"], movie_name)
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
            available = check_availability(city, avail_entry, all_codes, movie["slug"])

            if not available:
                log.info("No shows open yet.")
                if state_file.exists():
                    state_file.unlink()
                continue

            current = json.dumps(available, sort_keys=True)
            current_hash = hashlib.md5(current.encode()).hexdigest()

            if state_file.exists() and state_file.read_text().strip() == current_hash:
                log.info("No changes since last check.")
                continue

            state_file.write_text(current_hash)

            date_html = ""
            for d in sorted(available):
                disp = f"{d[6:8]}/{d[4:6]}/{d[:4]}"
                venues = available[d]
                venue_str = ', '.join(venues) if venues else 'Booking open — check BookMyShow for theatres'
                log.info(f"✓ NEW: {disp}: {venue_str}")
                date_html += f"<p><strong>{disp}</strong><br>{venue_str}</p>"

            send_email(
                config,
                f"🎬 {movie_name} — booking is open!",
                f"""
                <h2>🎬 {movie_name} — shows are now open!</h2>
                {date_html}
                <p><a href="{movie['url']}">👉 Book on BookMyShow</a></p>
                """
            )
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

            # Pre-filter for target date
            date_entry = {"name": movie_name, "date": target_date, "theatres": theatres}
            valid_codes = filter_valid_codes(
                session, all_codes, movie_name, movie["slug"], city_slug, target_date
            )
            if not valid_codes:
                log.info(f"No shows for {display_date} yet (0/{len(all_codes)} codes have data).")
                if state_file.exists():
                    state_file.unlink()
                continue

            # Check showtimes at preferred theatres
            log.info(f"Checking {len(valid_codes)} valid codes (of {len(all_codes)} total)...")
            matched = check_showtimes(city, date_entry, valid_codes, movie["slug"])

            if not matched:
                log.info(f"No shows at your theatres for {display_date} yet.")
                if state_file.exists():
                    state_file.unlink()
                continue

            # Check for changes
            current = json.dumps(matched, sort_keys=True)
            current_hash = hashlib.md5(current.encode()).hexdigest()

            if state_file.exists() and state_file.read_text().strip() == current_hash:
                log.info("No changes since last check.")
                continue

            state_file.write_text(current_hash)

            for theatre, times in matched.items():
                log.info(f"✓ NEW: {theatre}: {', '.join(times) if times else 'Show added!'}")

            theatre_html = ""
            for theatre, times in matched.items():
                time_str = ', '.join(times) if times else 'Show added — check BookMyShow for times'
                theatre_html += f"<p><strong>{theatre}</strong><br>{time_str}</p>"

            send_email(
                config,
                f"🎬 {movie_name} — NEW shows! ({display_date})",
                f"""
                <h2>🎬 {movie_name} — shows for {display_date}!</h2>
                {theatre_html}
                <p><a href="{movie['url']}">👉 Book on BookMyShow</a></p>
                """
            )


if __name__ == "__main__":
    main()
