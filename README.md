# Flight Price Tracker → Email

Checks a Sastaticket flight search once a day and emails you when the
price drops. Runs for free on GitHub's servers — nothing to host or keep
running on your own machine.

(Originally built around Telegram, but Telegram is blocked by Pakistan's
PTA, so this uses plain email instead — works everywhere, no VPN needed.)

## How it works

1. GitHub Actions wakes up once a day (cron schedule you control).
2. It runs `flight_price_tracker.py`, which calls Sastaticket's flight
   search endpoint, decodes the results, and finds the cheapest fare.
3. It compares that to the last recorded price (`price_history.json`,
   committed back into this repo each run so history persists).
4. If the price dropped, it emails you.

## One-time setup

### 1. Create an "app password" for your email account
Regular email passwords won't work for automated sending — you need an
app-specific password.

**If you use Gmail:**
1. Go to your Google Account → **Security**.
2. Turn on **2-Step Verification** if it isn't already on.
3. Go to **Security → App passwords** (search "app passwords" in the
   account settings search bar if you don't see it directly).
4. Create a new app password (name it anything, e.g. "flight tracker").
5. Google shows you a 16-character password like `abcd efgh ijkl mnop`.
   Copy it (remove the spaces) — this is your `EMAIL_PASSWORD`, **not**
   your normal Gmail password.

**If you use Outlook/Yahoo/another provider:** search "[your provider]
app password" — the concept is the same, and you'll also need that
provider's SMTP server/port instead of Gmail's (see step 3).

### 2. Create a GitHub repo
1. Go to github.com → New repository (can be private).
2. Upload all the files in this folder (`flight_price_tracker.py`,
   `requirements.txt`, `price_history.json`, `price_log.txt`, and the
   `.github/workflows/daily-price-check.yml` file — keep that folder
   structure intact).
   - Easiest way: install GitHub Desktop, or drag-and-drop files into
     the repo via the GitHub website's "Add file → Upload files" button
     (make sure `.github/workflows/daily-price-check.yml` ends up at
     that exact path).

### 3. Add your secrets
In your new repo: **Settings → Secrets and variables → Actions → New
repository secret**. Add these:
- `EMAIL_SENDER` = the email address you're sending *from* (e.g. your Gmail)
- `EMAIL_PASSWORD` = the app password from step 1
- `EMAIL_RECIPIENT` = the email address you want alerts sent *to* (can be
  the same address, or a different one — e.g. your phone's primary inbox)
- `SMTP_SERVER` = `smtp.gmail.com` (only needed if not using Gmail —
  otherwise this defaults automatically)
- `SMTP_PORT` = `587` (only needed if not using Gmail)

### 4. Edit your route
Open `flight_price_tracker.py` and edit the `CONFIG` section near the
top — `ORIGIN`, `DESTINATION`, `DEPARTURE_DATE` — for the flight you
want tracked.

### 5. Test it
Go to the **Actions** tab in your repo → "Daily flight price check" →
**Run workflow** (this uses the manual trigger built into the workflow
file, so you don't have to wait for the schedule). Check the run logs,
and check your inbox — by default, no email is sent on a flat/first run
(see `NOTIFY_ONLY_ON_DROP` below), only when a price drop is detected.

## Adjusting behavior

- **`NOTIFY_ONLY_ON_DROP`** in `flight_price_tracker.py`: set to `False`
  if you want an email every single day regardless of whether the price
  changed (useful for confirming it's still running).
- **Schedule time**: edit the `cron` line in
  `.github/workflows/daily-price-check.yml`. Cron time is always UTC.
  E.g. `"0 6 * * *"` = 06:00 UTC = 11:00 AM PKT.
- **Multiple routes**: duplicate the script's CONFIG block into a second
  script (or refactor to loop over a list of routes) if you want more
  than one route tracked — happy to build that out if useful.

## Notes and limits

- This uses an **unofficial, undocumented** Sastaticket endpoint (their
  own website's internal API). It could change or start blocking
  automated requests at any time without notice — that's the tradeoff
  of not having an official public API to use instead.
- Keep the schedule to once a day, as configured — no need to hit it
  more often, and doing so risks getting blocked.
- Most email apps (Gmail, Outlook) push a phone notification the moment
  a new email arrives, so this still gets you a real-time alert on your
  phone without needing a dedicated app.
