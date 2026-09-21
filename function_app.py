import os
import json
import logging
import urllib.request
import urllib.error
import azure.functions as func

app = func.FunctionApp()

# =========================
# DEFAULT CONFIGURATION
# =========================

DEFAULT_CINEMA_ID = "1052"
DEFAULT_MOVIE_NAME = "The Odyssey"
DEFAULT_AUDITORIUM_NAME = "IMAX"
DEFAULT_RATIO_THRESHOLD = 0.016

DEFAULT_MOVIE_PAGE_URL = (
    "https://www.cinemacity.cz/films/odyssea/7268s2r"
    "#/buy-tickets-by-cinema?in-cinema={cinema_id}&at-date={date_str}"
)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

STATE_FILE = "cinema_state.json"


# =========================
# CONFIGURATION
# =========================

def get_target_dates() -> list[str]:
    raw = os.environ.get("TARGET_DATES", "").strip()

    if not raw:
        return ["2026-08-08", "2026-08-09"]

    try:
        parsed = json.loads(raw)

        if isinstance(parsed, list):
            return [str(d).strip() for d in parsed]

    except Exception:
        pass

    return [d.strip() for d in raw.split(",") if d.strip()]


def get_allowed_showtimes() -> dict[str, list[str]]:
    """
    {} means all showtimes are allowed.
    """

    raw = os.environ.get("ALLOWED_SHOWTIMES", "").strip()

    if not raw:
        return {
            "2026-08-08": ["16:40", "20:30"],
            "2026-08-09": ["09:00", "12:50", "16:40"]
        }

    try:
        parsed = json.loads(raw)

        if isinstance(parsed, dict):
            return {
                k: [str(t).strip() for t in v]
                for k, v in parsed.items()
            }

    except Exception as e:
        logging.warning(
            f"Could not parse ALLOWED_SHOWTIMES: {e}. "
            "Allowing all showtimes."
        )

    return {}


# =========================
# STATE
# =========================

def load_state() -> dict:
    """
    Loads the state saved by GitHub Actions cache.
    """

    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)

    except Exception as e:
        logging.warning(f"Could not load state: {e}")

    return {}


def save_state(state: dict) -> None:
    """
    Saves current availability state.
    """

    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                state,
                f,
                ensure_ascii=False,
                indent=2
            )

        logging.info("Availability state saved successfully.")

    except Exception as e:
        logging.error(f"Could not save state: {e}")


# =========================
# TELEGRAM
# =========================

def send_telegram_message(
    bot_token: str,
    chat_id: str,
    message: str
) -> bool:

    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

        payload = json.dumps({
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": True
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json"
            }
        )

        with urllib.request.urlopen(req, timeout=10) as resp:

            if resp.status == 200:
                logging.info(
                    "Telegram notification sent successfully."
                )
                return True

            logging.error(
                f"Telegram API returned HTTP status {resp.status}"
            )
            return False

    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        logging.error(
            f"Telegram HTTP error {e.code}: {error_body}"
        )
        return False

    except Exception as e:
        logging.error(
            f"Failed to send Telegram notification: {e}"
        )
        return False


def dispatch_alerts(message: str):

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if bot_token and chat_id:
        send_telegram_message(
            bot_token,
            chat_id,
            message
        )
    else:
        logging.warning(
            "Telegram notification skipped: "
            "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing."
        )


# =========================
# CINEMA CITY API
# =========================

def get_cinema_events(
    cinema_id: str,
    date_str: str,
    user_agent: str
) -> list:

    url = (
        "https://www.cinemacity.cz/"
        "cz/data-api-service/v1/quickbook/10101/"
        f"film-events/in-cinema/{cinema_id}/"
        f"at-date/{date_str}?attr=&lang=cs_CZ"
    )

    logging.info(
        f"Polling Cinema City API for {date_str}..."
    )

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/json"
        }
    )

    with urllib.request.urlopen(
        req,
        timeout=15
    ) as resp:

        if resp.status != 200:
            logging.warning(
                f"Cinema City returned HTTP {resp.status}"
            )
            return []

        raw_data = resp.read().decode("utf-8")
        data = json.loads(raw_data)

    events = data.get(
        "body",
        {}
    ).get(
        "events",
        []
    )

    logging.info(
        f"Retrieved {len(events)} total events for {date_str}."
    )

    return events


# =========================
# MAIN MONITOR
# =========================

@app.timer_trigger(
    schedule="%MONITOR_SCHEDULE%",
    arg_name="myTimer",
    run_on_startup=False
)
def imax_ticket_monitor(
    myTimer: func.TimerRequest
) -> None:

    cinema_id = os.environ.get(
        "CINEMA_ID",
        DEFAULT_CINEMA_ID
    )

    movie_name = os.environ.get(
        "MOVIE_NAME",
        DEFAULT_MOVIE_NAME
    )

    auditorium_filter = os.environ.get(
        "AUDITORIUM_NAME",
        DEFAULT_AUDITORIUM_NAME
    )

    movie_page_url_template = os.environ.get(
        "MOVIE_PAGE_URL",
        DEFAULT_MOVIE_PAGE_URL
    )

    user_agent = os.environ.get(
        "USER_AGENT",
        DEFAULT_USER_AGENT
    )

    try:
        threshold_env = os.environ.get(
            "RATIO_THRESHOLD"
        )

        threshold = (
            float(threshold_env)
            if threshold_env
            else DEFAULT_RATIO_THRESHOLD
        )

    except ValueError:
        threshold = DEFAULT_RATIO_THRESHOLD

    target_dates = get_target_dates()
    allowed_showtimes_map = get_allowed_showtimes()

    previous_state = load_state()
    current_state = {}

    newly_available = []
    newly_unavailable = []
    still_available = []

    logging.info(
        f"Starting IMAX monitor for {movie_name}"
    )

    for date_str in target_dates:

        try:
            events = get_cinema_events(
                cinema_id,
                date_str,
                user_agent
            )

        except urllib.error.URLError as e:
            logging.error(
                f"Network error for {date_str}: {e}"
            )
            continue

        except Exception as e:
            logging.error(
                f"Unexpected error for {date_str}: {e}"
            )
            continue

        allowed_times = allowed_showtimes_map.get(
            date_str,
            []
        )

        for ev in events:

            auditorium = ev.get(
                "auditoriumTinyName",
                ""
            )

            if auditorium_filter and auditorium != auditorium_filter:
                continue

            event_dt = ev.get(
                "eventDateTime",
                ""
            )

            if "T" in event_dt:
                time_part = event_dt.split(
                    "T"
                )[-1][:5]
            else:
                time_part = ""

            if (
                allowed_times
                and not any(
                    time_part.startswith(t)
                    for t in allowed_times
                )
            ):
                continue

            availability_ratio = float(
                ev.get(
                    "availabilityRatio",
                    0.0
                ) or 0.0
            )

            event_id = str(
                ev.get(
                    "id",
                    f"{date_str}_{time_part}"
                )
            )

            launch_link = (
                ev.get("bookingRouterLaunchLink")
                or ev.get("bookingLink")
                or f"https://tickets.cinemacity.cz/api/order/{event_id}"
            )

            movie_page_link = (
                movie_page_url_template.format(
                    cinema_id=cinema_id,
                    date_str=date_str
                )
            )

            available = (
                availability_ratio > threshold
            )

            current_state[event_id] = {
                "date": date_str,
                "time": time_part,
                "available": available,
                "availability_ratio": availability_ratio,
                "launch_link": launch_link,
                "movie_page_link": movie_page_link,
                "auditorium": auditorium
            }

            old = previous_state.get(event_id)

            logging.info(
                f"{date_str} {time_part} | "
                f"availability ratio: "
                f"{availability_ratio:.4f} | "
                f"available: {available}"
            )

            # First time we ever see this showtime
            if old is None:

                if available:
                    newly_available.append(
                        current_state[event_id]
                    )

            # Previously unavailable -> available
            elif (
                not old.get("available", False)
                and available
            ):

                newly_available.append(
                    current_state[event_id]
                )

            # Previously available -> unavailable
            elif (
                old.get("available", False)
                and not available
            ):

                newly_unavailable.append(
                    current_state[event_id]
                )

            elif available:

                still_available.append(
                    current_state[event_id]
                )

    # =========================
    # BUILD TELEGRAM MESSAGE
    # =========================

    message_parts = []

    # New tickets / showtimes
    if newly_available:

        message_parts.append(
            "🎬 *IMAX – VSTUPENKY JSOU DOSTUPNÉ!*"
        )

        message_parts.append("")

        grouped = {}

        for ev in newly_available:
            grouped.setdefault(
                ev["date"],
                []
            ).append(ev)

        for date_str in sorted(grouped):

            message_parts.append(
                f"📅 *{date_str}*"
            )

            for ev in sorted(
                grouped[date_str],
                key=lambda x: x["time"]
            ):

                message_parts.append(
                f"🕐 {ev['time']}"
                )

            message_parts.append("")

    # Sold out
    if newly_unavailable:

        message_parts.append(
            "🔴 *PROJEKCE SE VYPRODALA*"
        )

        message_parts.append("")

        for ev in newly_unavailable:

            message_parts.append(
                f"📅 {ev['date']} "
                f"🕐 *{ev['time']}*"
            )

        message_parts.append("")

    # Send only when something changed
    if message_parts:

        message_parts.append(
            "🎬 Cinema City Flora – IMAX"
        )

        alert_message = "\n".join(
            message_parts
        )

        logging.info(
            "Sending Telegram notification."
        )

        dispatch_alerts(
            alert_message
        )

    else:

        logging.info(
            "No availability changes detected. "
            "No Telegram message sent."
        )

    # Save current state
    save_state(
        current_state
    )
