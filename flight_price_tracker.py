"""
Sastaticket Flight Price Logger
--------------------------------
Calls the (unofficial, undocumented) Sastaticket flight search endpoint
for today plus the next few days (no hardcoded date — computed fresh
every run), reads the Server-Sent Events (SSE) stream it returns,
decodes each event (base64 + zlib-compressed JSON), and logs the
cheapest flight found per date — price and airline name — to a local
price history so you can track changes over time. Optionally emails you
when any of those dates' prices drop.

IMPORTANT NOTES:
- This hits an internal endpoint used by sastaticket.pk's own website.
  It is NOT a public/documented API. The response shape, auth requirements,
  or availability could change at any time without notice.
- Keep usage light (e.g. once a day). Don't hammer it with frequent requests.
- No raw response data is saved to disk — only the extracted cheapest
  price + airline name per run, in price_history.json.

USAGE:
    pip install requests
    python flight_price_tracker.py
    (edit ORIGIN/DESTINATION/DATE_RANGE_DAYS in the CONFIG section below)

Re-run this daily (manually, via cron, or Task Scheduler) to build up a
price history in price_history.json and price_log.txt.
"""

import json
import uuid
import os
import sys
import base64
import zlib
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timezone, timedelta

import requests

# ============================================================
# CONFIG - edit these for your route
# ============================================================
ORIGIN = "KHI"
DESTINATION = "ISB"
CABIN_CLASS_CODE = "Y"
CABIN_CLASS_LABEL = "Economy"
NUM_ADULT = 1
NUM_CHILD = 0
NUM_INFANT = 0

# Instead of a fixed date, this searches "today" plus the next N-1 days,
# computed fresh on every run — so you never have to edit a hardcoded date.
DATE_RANGE_DAYS = 3   # e.g. 3 = today, tomorrow, day after tomorrow

# "Today" is computed using this UTC offset so it matches your local day
# even when the script runs on a server in a different timezone (like
# GitHub Actions, which runs in UTC). 5 = Pakistan Standard Time (PKT).
LOCAL_UTC_OFFSET_HOURS = 5

# This looked like a persistent per-browser analytics id in the sample
# payload you captured. Keeping it fixed is probably fine for a personal
# script, but if requests start failing, try generating a fresh one.
DEVICE_ID = "GA1.2.275300831.1783856425"

API_URL = "https://sse-green.sastaticket.pk/api/v2/flights/search"

HISTORY_FILE = "price_history.json"
LOG_FILE = "price_log.txt"

# ---- Email notification config ----
# Set these as environment variables (e.g. GitHub Actions secrets) —
# never hardcode a real password in this file. If using Gmail, EMAIL_PASSWORD
# must be a 16-character "App Password" (Google Account -> Security ->
# 2-Step Verification -> App passwords), not your normal login password.
SMTP_SERVER = os.environ.get("SMTP_SERVER") or "smtp.gmail.com"
SMTP_PORT = int(os.environ.get("SMTP_PORT") or "587")
EMAIL_SENDER = os.environ.get("EMAIL_SENDER")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
EMAIL_RECIPIENT = os.environ.get("EMAIL_RECIPIENT")

# If True, only sends an email when the price actually dropped.
# If False, sends an email every run (drop, rise, unchanged, or first check).
NOTIFY_ONLY_ON_DROP = True

# ============================================================


def now_pkt_str():
    """Current time formatted in PKT (Pakistan Standard Time), e.g. '2026-07-13 04:48 PM PKT'."""
    pkt_now = datetime.now(timezone.utc) + timedelta(hours=LOCAL_UTC_OFFSET_HOURS)
    return pkt_now.strftime("%Y-%m-%d %I:%M %p PKT")


def get_search_dates():
    """
    Returns a list of YYYY-MM-DD strings: today (in the local timezone
    defined by LOCAL_UTC_OFFSET_HOURS) through today + DATE_RANGE_DAYS - 1.
    Computed fresh every run, so nothing is ever hardcoded.
    """
    local_now = datetime.now(timezone.utc) + timedelta(hours=LOCAL_UTC_OFFSET_HOURS)
    today = local_now.date()
    return [(today + timedelta(days=i)).isoformat() for i in range(DATE_RANGE_DAYS)]


def build_payload(departure_date):
    return {
        "legs": [
            {
                "origin": [ORIGIN],
                "destination": [DESTINATION],
                "departure_date": departure_date,
            }
        ],
        "route_type": "ONEWAY",
        "cabin_class": {"code": CABIN_CLASS_CODE, "label": CABIN_CLASS_LABEL},
        "non_stop_flight": False,
        "traveler_count": {
            "num_adult": NUM_ADULT,
            "num_child": NUM_CHILD,
            "num_infant": NUM_INFANT,
        },
        "analytics_data": {
            "is_first_request": True,
            "search_id": str(uuid.uuid4()),
            "platform": "Web Browser",
            "device_id": DEVICE_ID,
        },
    }


def route_key(departure_date):
    return f"{ORIGIN}-{DESTINATION}-{departure_date}"


def fetch_sse_events(payload):
    """
    POSTs to the search endpoint and parses the text/event-stream response.
    Returns a list of parsed JSON events (raw dicts). Non-JSON events are
    kept as raw strings.
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        "Origin": "https://www.sastaticket.pk",
        "Referer": "https://www.sastaticket.pk/",
    }

    events = []
    data_buffer = []

    with requests.post(
        API_URL, headers=headers, json=payload, stream=True, timeout=60
    ) as resp:
        resp.raise_for_status()
        for raw_line in resp.iter_lines(decode_unicode=True):
            if raw_line is None:
                continue
            line = raw_line.strip()

            if line == "":
                # blank line = end of one SSE event
                if data_buffer:
                    chunk = "\n".join(data_buffer)
                    data_buffer = []
                    try:
                        events.append(json.loads(chunk))
                    except json.JSONDecodeError:
                        events.append({"_raw_unparsed": chunk})
                continue

            if line.startswith("data:"):
                data_buffer.append(line[len("data:"):].strip())
            # ignore "event:", "id:", "retry:" lines — add handling here
            # later if you discover you need them

        # flush any trailing buffered data
        if data_buffer:
            chunk = "\n".join(data_buffer)
            try:
                events.append(json.loads(chunk))
            except json.JSONDecodeError:
                events.append({"_raw_unparsed": chunk})

    return events


def decode_event(event):
    """
    Each SSE event looks like: {"data": "<base64-encoded, zlib-compressed JSON>"}
    This decodes it back into the real payload dict. If an event doesn't
    match that shape (e.g. a plain keepalive or an already-parsed dict),
    it's returned unchanged.
    """
    if not isinstance(event, dict) or "data" not in event:
        return event

    payload = event["data"]
    if not isinstance(payload, str):
        return event

    try:
        raw = base64.b64decode(payload)
        decompressed = zlib.decompress(raw)
        return json.loads(decompressed)
    except Exception:
        # Not compressed / not base64 / not valid JSON after decompression —
        # just return the original event so nothing crashes.
        return event


def extract_flights(decoded_event):
    """
    Given a decoded event (the real search-result payload), pull out
    (price, airline_name) for every flight offer. Sastaticket's shape
    (as reverse-engineered) is:
        decoded_event["flights"] -> list of "flight groups"
            -> each group is a list of flight offers
                -> offer["meta"]["price"] = cheapest fare for that offer
                -> offer["legs"][0]["operating_airline"]["name"] = airline name
                   (falls back to offer["provider"] if that's missing)
    Returns a list of dicts: [{"price": 25000, "airline": "Airblue"}, ...]
    Walks the structure defensively so it won't crash if a field is missing.
    """
    flight_list = []
    flights = decoded_event.get("flights") if isinstance(decoded_event, dict) else None
    if not isinstance(flights, list):
        return flight_list

    for group in flights:
        if not isinstance(group, list):
            continue
        for offer in group:
            if not isinstance(offer, dict):
                continue
            price = offer.get("meta", {}).get("price")
            if not isinstance(price, (int, float)):
                continue

            airline = None
            legs = offer.get("legs")
            if isinstance(legs, list) and legs and isinstance(legs[0], dict):
                airline = legs[0].get("operating_airline", {}).get("name")
            if not airline:
                airline = offer.get("provider")

            flight_list.append({"price": price, "airline": airline or "Unknown"})

    return flight_list


def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    return {}


def save_history(history):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def append_log(line):
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    print(line)


def send_email(subject, body):
    """
    Sends an email via SMTP (defaults to Gmail's SMTP server). Silently
    skips (with a log line) if EMAIL_SENDER / EMAIL_PASSWORD / EMAIL_RECIPIENT
    aren't configured, so the script still works fine without notifications
    set up.
    """
    if not EMAIL_SENDER or not EMAIL_PASSWORD or not EMAIL_RECIPIENT:
        append_log(
            "[email] Skipped notification — EMAIL_SENDER / EMAIL_PASSWORD / "
            "EMAIL_RECIPIENT not set."
        )
        return

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECIPIENT

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=20) as server:
            server.starttls()
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, [EMAIL_RECIPIENT], msg.as_string())
        append_log(f"[email] Sent notification to {EMAIL_RECIPIENT}: {subject}")
    except Exception as e:
        append_log(f"[email] ERROR sending notification: {e}")


def process_date(departure_date):
    """
    Runs a full search + compare + history-update cycle for a single
    departure date. Returns a dict describing what happened, so main()
    can aggregate results across all searched dates into one email.
    """
    print(f"Searching {ORIGIN} -> {DESTINATION} on {departure_date} ...")
    payload = build_payload(departure_date)
    timestamp_iso = datetime.now(timezone.utc).isoformat()
    key = route_key(departure_date)

    try:
        events = fetch_sse_events(payload)
    except requests.exceptions.RequestException as e:
        append_log(f"[{timestamp_iso}] {key}: ERROR fetching data: {e}")
        return {"key": key, "date": departure_date, "status": "error", "error": str(e)}

    if not events:
        append_log(
            f"[{timestamp_iso}] {key}: No events received. The endpoint may "
            f"require different headers/auth than assumed."
        )
        return {"key": key, "date": departure_date, "status": "no_events"}

    decoded_events = [decode_event(ev) for ev in events]
    all_flights = []
    for ev in decoded_events:
        all_flights.extend(extract_flights(ev))

    history = load_history()
    history.setdefault(key, [])

    if not all_flights:
        history[key].append(
            {"timestamp": timestamp_iso, "min_price": None, "airline": None, "num_events": len(events)}
        )
        save_history(history)
        append_log(
            f"[{timestamp_iso}] {key}: received {len(events)} event(s) but found no "
            f"flight prices. Sastaticket's response structure may have changed — "
            f"update extract_flights() if this keeps happening."
        )
        return {"key": key, "date": departure_date, "status": "no_prices"}

    cheapest = min(all_flights, key=lambda f: f["price"])
    history[key].append(
        {
            "timestamp": timestamp_iso,
            "min_price": cheapest["price"],
            "airline": cheapest["airline"],
            "num_events": len(events),
        }
    )
    save_history(history)

    prev_entries = history[key][:-1]
    if not prev_entries:
        append_log(
            f"[{timestamp_iso}] {key}: lowest price found = {cheapest['price']} "
            f"({cheapest['airline']}) (first recorded run)."
        )
        return {
            "key": key, "date": departure_date, "status": "first_check",
            "price": cheapest["price"], "airline": cheapest["airline"],
        }

    prev_price = prev_entries[-1]["min_price"]
    diff = cheapest["price"] - prev_price
    trend = "DROPPED" if diff < 0 else ("rose" if diff > 0 else "unchanged")
    append_log(
        f"[{timestamp_iso}] {key}: lowest price found = {cheapest['price']} "
        f"({cheapest['airline']}) (previous: {prev_price}, {trend} by {abs(diff)})."
    )
    return {
        "key": key, "date": departure_date, "status": "compared",
        "price": cheapest["price"], "airline": cheapest["airline"],
        "prev_price": prev_price, "diff": diff, "trend": trend,
    }


def main():
    dates = get_search_dates()
    results = [process_date(d) for d in dates]
    checked_at = now_pkt_str()

    drops = [r for r in results if r.get("status") == "compared" and r["diff"] < 0]

    if drops:
        lines = []
        for r in drops:
            lines.append(
                f"{r['date']}: {r['airline']} {r['prev_price']:,.0f} -> {r['price']:,.0f} "
                f"(down {abs(r['diff']):,.0f})"
            )
        send_email(
            subject=f"✈️ Price drop: {ORIGIN}-{DESTINATION} ({len(drops)} date(s))",
            body=(
                f"Checked at {checked_at}\n\n"
                f"Price drop(s) detected:\n\n" + "\n".join(lines)
            ),
        )
    elif not NOTIFY_ONLY_ON_DROP:
        lines = []
        for r in results:
            if r.get("status") == "compared":
                lines.append(
                    f"{r['date']}: {r['airline']} at {r['price']:,.0f} "
                    f"({r['trend']}, previous {r['prev_price']:,.0f})"
                )
            elif r.get("status") == "first_check":
                lines.append(f"{r['date']}: {r['airline']} at {r['price']:,.0f} (first check)")
            else:
                lines.append(f"{r['date']}: no price data ({r.get('status')})")
        send_email(
            subject=f"Flight price update: {ORIGIN}-{DESTINATION}",
            body=f"Checked at {checked_at}\n\n" + "\n".join(lines),
        )


if __name__ == "__main__":
    main()
