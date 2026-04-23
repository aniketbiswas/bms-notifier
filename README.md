# BookMyShow Show Notifier 🎬

Get notified the moment your preferred theatres add shows for a movie on BookMyShow.

**How it works:** Checks BMS every 15 minutes for new showtimes at your chosen theatres. When shows are added, you get an email instantly.

## Quick Start

### 1. Fork this repo

Click **Fork** on GitHub to create your own copy.

### 2. Edit `config.yml`

```yaml
city: "Hyderabad"              # Your city

movies:
  - name: "Project Hail Mary"  # Movie name as shown on BMS
    dates:                     # Single or multiple dates (YYYYMMDD)
      - "20260425"             # Friday
      - "20260426"             # Saturday
      - "20260427"             # Sunday
    theatres:                  # Optional — omit to watch all theatres
      - "AMB Cinemas"
      - "Allu Cinemas"

  - name: "Michael"
    dates:
      - "20260427"
    theatres:
      - "PVR"
      - "INOX"
```
  - "Allu Cinemas"
```

### 3. Set up email notifications

#### Gmail Setup (Free)

1. **Enable 2-Factor Authentication** on your Google account
   - Go to [myaccount.google.com/security](https://myaccount.google.com/security)
   - Turn on **2-Step Verification**

2. **Generate an App Password**
   - Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
   - Select **Mail** → **Generate**
   - Copy the 16-character password (e.g. `abcd efgh ijkl mnop`)

3. **Add GitHub Secrets**
   - Go to your forked repo → **Settings** → **Secrets and variables** → **Actions**
   - Add these secrets:

   | Secret | Value |
   |--------|-------|
   | `SMTP_SERVER` | `smtp.gmail.com` |
   | `SMTP_PORT` | `587` |
   | `SMTP_USER` | Your Gmail address (e.g. `you@gmail.com`) |
   | `SMTP_PASSWORD` | The App Password from step 2 |
   | `NOTIFY_EMAIL` | Email to receive alerts (can be same as SMTP_USER) |

### 4. Enable the workflow

- Go to **Actions** tab → **Check BookMyShow Shows** → **Enable workflow**
- It runs every 15 minutes automatically
- You can also click **Run workflow** to test manually

### 5. Done!

You'll receive an email like this when shows are added:

> **🎬 Project Hail Mary — NEW shows!**
>
> **AMB Cinemas: Gachibowli**
> 10:25 PM
>
> 👉 Book on BookMyShow

## How It Works

1. **Discovers** the movie on BMS using the listing page
2. **Finds** all format/language variants (event codes) for that movie
3. **Checks** each variant for your target date
4. **Matches** against your preferred theatres
5. **Notifies** you only when something new appears (no duplicate emails)

## Run Locally (Optional)

```bash
# Install dependencies
pip install -r requirements.txt

# Run once
python check_shows.py

# Run with email
SMTP_USER='you@gmail.com' SMTP_PASSWORD='your-app-password' NOTIFY_EMAIL='you@gmail.com' python check_shows.py

# Set up cron (every 15 min)
crontab -e
# Add: */15 * * * * cd /path/to/bms-notifier && SMTP_USER='...' SMTP_PASSWORD='...' NOTIFY_EMAIL='...' python3 check_shows.py >> bms.log 2>&1
```

## Manual Trigger with Custom Inputs

You can override `config.yml` when triggering manually from the Actions UI:

1. Go to **Actions** → **Check BookMyShow Shows** → **Run workflow**
2. Fill in: Movie, City, Date, Theatres
3. These override `config.yml` for that run only

## Disable When Done

Once you've booked your tickets:

```bash
# GitHub Actions
gh workflow disable check-shows.yml

# Local cron
crontab -r
```

Or go to **Actions** → **Check BookMyShow Shows** → **⋯** → **Disable workflow**

## Limitations

- BMS may intermittently block GitHub Actions IPs (Cloudflare). The script retries up to 5 times.
- Date-specific showtime data is accurate, but exact show times may only appear in the Redux JSON for some formats.
- Running locally from a residential IP is more reliable than GitHub Actions.
