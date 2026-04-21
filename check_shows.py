import os
import sys
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests

MOVIE_NAME = os.getenv("MOVIE_NAME", "Project Hail Mary")
CITY = os.getenv("CITY", "Hyderabad")
REGION_CODE = os.getenv("REGION_CODE", "HYDR")
REGION_SLUG = os.getenv("REGION_SLUG", "hyderabad")

SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", "")

BMS_API_URL = "https://in.bookmyshow.com/api/explore/v1/discover/movie"
BMS_SHOWTIMES_URL = "https://in.bookmyshow.com/api/explore/v1/showtimes/movie"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://in.bookmyshow.com",
    "Referer": "https://in.bookmyshow.com/",
}


def search_movie():
    """Search for the movie on BookMyShow in the given city."""
    params = {
        "region": REGION_CODE,
        "slug": REGION_SLUG,
        "language": "en",
        "format": "json",
    }

    try:
        resp = requests.get(BMS_API_URL, headers=HEADERS, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"Error fetching movie list: {e}")
        return None

    # Search through the response for our movie
    movies = []
    if isinstance(data, dict):
        # Try common response structures
        for key in ["data", "movies", "results", "childEvents"]:
            if key in data:
                items = data[key]
                if isinstance(items, list):
                    movies.extend(items)
                elif isinstance(items, dict) and "data" in items:
                    movies.extend(items.get("data", []))

    search_term = MOVIE_NAME.lower()
    for movie in movies:
        title = movie.get("EventTitle", movie.get("title", movie.get("name", ""))).lower()
        if search_term in title or title in search_term:
            print(f"Found movie: {movie.get('EventTitle', movie.get('title', title))}")
            return movie

    return None


def check_showtimes(movie):
    """Check if showtimes are available for the movie."""
    event_code = movie.get("EventCode", movie.get("code", movie.get("id", "")))
    event_group = movie.get("EventGroup", "ET00000000")

    if not event_code:
        print("No event code found for movie")
        return None

    # Try to fetch showtimes
    params = {
        "region": REGION_CODE,
        "slug": REGION_SLUG,
        "eventCode": event_code,
        "language": "en",
        "format": "json",
    }

    try:
        resp = requests.get(BMS_SHOWTIMES_URL, headers=HEADERS, params=params, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            return data
    except Exception as e:
        print(f"Error checking showtimes: {e}")

    # Fallback: check the buytickets page
    slug = movie.get("EventURL", movie.get("slug", MOVIE_NAME.lower().replace(" ", "-")))
    buytickets_url = f"https://in.bookmyshow.com/{REGION_SLUG}/movies/{slug}/{event_group}"

    try:
        resp = requests.get(buytickets_url, headers=HEADERS, timeout=30)
        if resp.status_code == 200 and "book tickets" in resp.text.lower():
            return {"url": buytickets_url, "available": True}
    except Exception as e:
        print(f"Error checking buytickets page: {e}")

    return None


def check_direct_search():
    """Directly search BMS for the movie as a fallback."""
    search_url = "https://in.bookmyshow.com/api/explore/v1/search"
    params = {
        "region": REGION_CODE,
        "q": MOVIE_NAME,
        "language": "en",
        "format": "json",
    }

    try:
        resp = requests.get(search_url, headers=HEADERS, params=params, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            results = data if isinstance(data, list) else data.get("data", data.get("results", []))
            if isinstance(results, list):
                for item in results:
                    title = item.get("EventTitle", item.get("title", item.get("name", ""))).lower()
                    if MOVIE_NAME.lower() in title:
                        return item
    except Exception as e:
        print(f"Error in direct search: {e}")

    return None


def send_email(subject, body):
    """Send email notification."""
    if not all([SMTP_USER, SMTP_PASSWORD, NOTIFY_EMAIL]):
        print("Email credentials not configured. Skipping email.")
        print(f"SUBJECT: {subject}")
        print(f"BODY: {body}")
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
        print("Email sent successfully!")
        return True
    except Exception as e:
        print(f"Failed to send email: {e}")
        return False


def main():
    print(f"Checking BookMyShow for '{MOVIE_NAME}' in {CITY}...")

    # Try API search first
    movie = search_movie()

    # Fallback to direct search
    if not movie:
        print("Movie not found in listings, trying direct search...")
        movie = check_direct_search()

    if not movie:
        print(f"'{MOVIE_NAME}' not yet listed on BookMyShow in {CITY}.")
        sys.exit(0)

    print(f"Movie found on BookMyShow!")
    showtimes = check_showtimes(movie)

    event_title = movie.get("EventTitle", movie.get("title", MOVIE_NAME))
    slug = movie.get("EventURL", movie.get("slug", MOVIE_NAME.lower().replace(" ", "-")))
    event_code = movie.get("EventGroup", movie.get("EventCode", ""))
    movie_url = f"https://in.bookmyshow.com/{REGION_SLUG}/movies/{slug}/{event_code}"

    if showtimes:
        subject = f"🎬 {event_title} - Shows Available in {CITY}!"
        body = f"""
        <h2>🎬 {event_title} is now showing in {CITY}!</h2>
        <p>Shows are available on BookMyShow.</p>
        <p><a href="{movie_url}">👉 Book Tickets Now</a></p>
        <p>Hurry before they sell out!</p>
        """
        send_email(subject, body)
        print("SHOWS AVAILABLE! Notification sent.")
    else:
        subject = f"🎬 {event_title} - Listed in {CITY} (No shows yet)"
        body = f"""
        <h2>🎬 {event_title} is listed on BookMyShow in {CITY}</h2>
        <p>The movie page is up but showtimes may not be available yet.</p>
        <p><a href="{movie_url}">👉 Check BookMyShow</a></p>
        """
        send_email(subject, body)
        print("Movie listed but no confirmed showtimes yet. Notification sent.")


if __name__ == "__main__":
    main()
