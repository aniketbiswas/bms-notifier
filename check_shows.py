import os
import re
import json
import smtplib
import datetime
import hashlib
import logging
import time
import random
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

from curl_cffi import requests

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

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

MOVIE_NAME = "Project Hail Mary"
MOVIE_SLUG = "project-hail-mary"
REGION_SLUG = "hyderabad"
TARGET_DATE = os.getenv("TARGET_DATE", "20260426")  # Sunday April 26

# Known event codes for Project Hail Mary in Hyderabad
# ET00451760 = English 2D (AMB shows here)
# ET00492371 = English DOLBY CINEMA (ALLU shows here)
EVENT_CODES = os.getenv("EVENT_CODES", "ET00451760,ET00492371").split(",")

PREFERRED_THEATRES = ["amb cinemas", "allu cinemas"]

SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", "")

STATE_FILE = Path(__file__).parent / ".last_state"


def fetch_page(url, max_retries=5):
    """Fetch a BMS page with retries."""
    for attempt in range(1, max_retries + 1):
        try:
            session = requests.Session(impersonate="chrome")
            r = session.get(url, timeout=20)
            if r.status_code == 200:
                return r.text
            log.warning(f"Attempt {attempt}/{max_retries}: HTTP {r.status_code}")
        except Exception as e:
            log.warning(f"Attempt {attempt}/{max_retries}: {e}")

        if attempt < max_retries:
            delay = random.uniform(8, 12)
            log.info(f"Retrying in {delay:.0f}s...")
            time.sleep(delay)

    log.error(f"All {max_retries} attempts failed for {url.split('/')[-2]}")
    return None


def check_showtimes():
    matched = {}

    for i, code in enumerate(EVENT_CODES):
        code = code.strip()
        if i > 0:
            delay = random.uniform(55, 65)
            log.info(f"Waiting {delay:.0f}s before next request...")
            time.sleep(delay)
        url = f"https://in.bookmyshow.com/movies/{REGION_SLUG}/{MOVIE_SLUG}/buytickets/{code}/{TARGET_DATE}"

        html = fetch_page(url)
        if not html:
            log.error(f"{code}: Failed to fetch")
            continue

        # Verify it's Project Hail Mary
        title_match = re.search(r'<title>(.*?)</title>', html, re.IGNORECASE)
        if title_match and "project" not in title_match.group(1).lower():
            log.warning(f"{code}: Wrong movie")
            continue

        # Verify the showDate matches our target date
        # BMS sometimes returns data for the nearest available date instead
        show_dates = re.findall(r'"showDate":"(\d{8})"', html)
        if show_dates and show_dates[0] != TARGET_DATE:
            log.info(f"{code}: Data is for {show_dates[0]}, not {TARGET_DATE} — skipping")
            continue

        # Extract date-accurate venue data from Redux JSON in HTML
        venues = re.findall(r'"venueName":"([^"]+)"', html)
        unique_venues = list(dict.fromkeys(venues))

        # Extract showtimes per venue from the same JSON
        # Pattern: venueName followed by showTime entries
        for venue in unique_venues:
            if any(p in venue.lower() for p in PREFERRED_THEATRES):
                # Find showtimes for this venue
                # Look for the venue block and extract times
                venue_pattern = re.escape(venue)
                # Find all showTime values near this venue
                venue_blocks = re.findall(
                    rf'"venueName":"{venue_pattern}".*?(?="venueName"|$)',
                    html, re.DOTALL
                )
                times = set()
                for block in venue_blocks:
                    found_times = re.findall(r'"showTime":"(\d{2}:\d{2})"', block)
                    for t in found_times:
                        h, m = int(t[:2]), t[3:]
                        suffix = "AM" if h < 12 else "PM"
                        h12 = h % 12 or 12
                        times.add(f"{h12:02d}:{m} {suffix}")

                if venue not in matched:
                    matched[venue] = set()
                matched[venue].update(times)
                log.info(f"{code}: {venue} -> {', '.join(sorted(times)) if times else '(times in JS only)'}")

        # Log if no preferred theatres found
        if not any(any(p in v.lower() for p in PREFERRED_THEATRES) for v in unique_venues):
            log.info(f"{code}: {len(unique_venues)} theatres, no AMB/ALLU")

    return {k: sorted(v) for k, v in matched.items()}


def send_email(subject, body):
    if not all([SMTP_USER, SMTP_PASSWORD, NOTIFY_EMAIL]):
        log.warning(f"Email not configured. SUBJECT: {subject}")
        return False

    msg = MIMEMultipart()
    msg["From"] = SMTP_USER
    msg["To"] = NOTIFY_EMAIL
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "html"))

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, NOTIFY_EMAIL, msg.as_string())
        log.info("Email sent!")
        return True
    except Exception as e:
        log.error(f"Email failed: {e}")
        return False


def main():
    display_date = f"{TARGET_DATE[6:8]}/{TARGET_DATE[4:6]}/{TARGET_DATE[:4]}"
    now = datetime.datetime.now().strftime("%H:%M:%S")

    log.info(f"Checking Project Hail Mary for {display_date}...")
    log.info(f"Codes: {', '.join(EVENT_CODES)}")

    matched = check_showtimes()

    if not matched:
        log.info(f"No shows at AMB/ALLU for {display_date} yet.")
        if STATE_FILE.exists():
            STATE_FILE.unlink()
        return

    # Check if anything changed
    current = json.dumps(matched, sort_keys=True)
    current_hash = hashlib.md5(current.encode()).hexdigest()

    if STATE_FILE.exists() and STATE_FILE.read_text().strip() == current_hash:
        log.info("No changes since last check.")
        return

    STATE_FILE.write_text(current_hash)

    for theatre, times in matched.items():
        log.info(f"✓ NEW: {theatre}: {', '.join(times) if times else 'Show added!'}")


    theatre_html = ""
    for theatre, times in matched.items():
        time_str = ', '.join(times) if times else 'Show added — check BookMyShow for times'
        theatre_html += f"<p><strong>{theatre}</strong><br>{time_str}</p>"

    send_email(
        f"🎬 Project Hail Mary — NEW shows at AMB/ALLU! ({display_date})",
        f"""
        <h2>🎬 Project Hail Mary — shows for {display_date}!</h2>
        {theatre_html}
        <p><a href="https://in.bookmyshow.com/hyderabad/movies/project-hail-mary/ET00451760">👉 Book on BookMyShow</a></p>
        """
    )


if __name__ == "__main__":
    main()
