# BookMyShow Notifier 🎬

Automatically checks BookMyShow for **Project Hail Mary** showtimes in **Hyderabad** and sends you an email when shows are available.

## Setup

### 1. Add GitHub Secrets

Go to **Settings → Secrets and variables → Actions** in this repo and add:

| Secret | Value |
|--------|-------|
| `SMTP_SERVER` | `smtp.gmail.com` (for Gmail) |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | Your Gmail address |
| `SMTP_PASSWORD` | Your Gmail [App Password](https://myaccount.google.com/apppasswords) |
| `NOTIFY_EMAIL` | Email to receive notifications |

> **Note:** For Gmail, you need to generate an **App Password** (not your regular password). Go to [Google App Passwords](https://myaccount.google.com/apppasswords) to create one.

### 2. Enable the Workflow

The workflow runs automatically every 15 minutes. You can also trigger it manually from the **Actions** tab.

### 3. Disable When Done

Once you've booked your tickets, disable the workflow:
- Go to **Actions** → **Check BookMyShow Shows** → **...** menu → **Disable workflow**

## Configuration

Edit the environment variables in `.github/workflows/check-shows.yml` to change:
- `MOVIE_NAME` — Movie to search for
- `CITY` — City name
- `REGION_CODE` — BMS region code (e.g., HYDR, MUMBAI, BANG)
- `REGION_SLUG` — URL slug (e.g., hyderabad, mumbai, bengaluru)
