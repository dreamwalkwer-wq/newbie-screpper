import re
import time
import math
from io import BytesIO

import streamlit as st
import pandas as pd
import requests
import feedparser
from langdetect import detect, DetectorFactory, LangDetectException

DetectorFactory.seed = 0
VALID_COUNTRY_RE = re.compile(r"^[a-z]{2}$", re.IGNORECASE)

st.set_page_config(
    page_title="App Store отзывы по app_id",
    page_icon="📱",
    layout="wide"
)

def init_session_state():
    if "logs" not in st.session_state:
        st.session_state.logs = []
    if "df" not in st.session_state:
        st.session_state.df = None

def log(msg: str):
    st.session_state.logs.append(str(msg))

init_session_state()

def validate_inputs(app_id, country, how_many, language_filter):
    try:
        app_id = int(str(app_id).strip())
    except Exception:
        raise ValueError(f"Неверный app_id: {app_id}. Должен быть числом.")

    if app_id <= 0:
        raise ValueError("app_id должен быть положительным целым.")

    if not isinstance(country, str) or not VALID_COUNTRY_RE.match(country.strip()):
        raise ValueError(f"Неверный country: {country}. Используйте 2-буквенный код, например 'ru', 'us', 'de'.")
    country = country.strip().lower()

    try:
        how_many = int(how_many)
    except Exception:
        raise ValueError(f"Неверный how_many: {how_many}. Должен быть целым числом.")
    if how_many <= 0:
        raise ValueError("how_many должен быть > 0.")

    if language_filter is not None and language_filter != "":
        language_filter = str(language_filter).strip().lower()
        if not language_filter:
            language_filter = None
    else:
        language_filter = None

    return app_id, country, how_many, language_filter

def safe_text(x):
    if x is None:
        return None
    x = str(x).replace("\r\n", "\n").replace("\r", "\n")
    x = re.sub(r"\s+", " ", x).strip()
    return x if x else None

def safe_date(x):
    try:
        return pd.to_datetime(x, errors="coerce", utc=True)
    except Exception:
        return pd.NaT

def detect_language(text):
    if text is None or not str(text).strip():
        return None, "empty_text"
    text = str(text).strip()
    if len(text) < 3:
        return None, "too_short"
    try:
        return detect(text), None
    except LangDetectException as e:
        return None, f"LangDetectException: {e}"
    except Exception as e:
        return None, f"Unexpected detection error: {e}"

def ensure_schema(df):
    cols = [
        "app_id", "country", "source", "date", "rating", "review",
        "review_id", "author", "title", "version", "language_detected", "review_url"
    ]
    for c in cols:
        if c not in df.columns:
            df[c] = None
    return df[cols]

def build_rss_urls(app_id, country, page):
    return [
        f"https://itunes.apple.com/{country}/rss/customerreviews/page={page}/id={app_id}/sortBy=mostRecent/xml",
        f"https://itunes.apple.com/{country}/rss/customerreviews/id={app_id}/sortBy=mostRecent/page={page}/xml",
    ]

def parse_rss_entries(feed, app_id, country, source_url):
    rows = []
    entries = getattr(feed, "entries", []) or []

    for entry in entries:
        review_text = None
        if entry.get("content"):
            try:
                review_text = entry["content"][0].get("value")
            except Exception:
                review_text = None
        if not review_text:
            review_text = entry.get("summary")

        author = entry.get("author")
        if not author and isinstance(entry.get("author_detail"), dict):
            author = entry["author_detail"].get("name")

        rating = entry.get("im_rating") or entry.get("rating") or entry.get("itunes_rating")
        version = entry.get("im_version") or entry.get("version") or entry.get("itunes_version")
        date_val = entry.get("updated") or entry.get("published")

        rows.append({
            "app_id": app_id,
            "country": country,
            "source": "apple_rss",
            "date": safe_date(date_val),
            "rating": rating,
            "review": safe_text(review_text),
            "review_id": entry.get("id"),
            "author": author,
            "title": safe_text(entry.get("title")),
            "version": version,
            "language_detected": None,
            "review_url": entry.get("link") or source_url,
        })

    return rows

def fetch_reviews_rss(app_id, country, how_many, timeout=30):
    log("[SOURCE] Используем Apple RSS customer reviews feed.")
    log("[SOURCE][INFO] RSS может вернуть меньше отзывов, чем запрошено, потому что Apple отдаёт только ограниченное число последних отзывов.")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari"
    })

    all_rows = []
    seen = set()
    max_pages = max(10, math.ceil(how_many / 50) + 5)

    for page in range(1, max_pages + 1):
        if len(all_rows) >= how_many:
            break

        log(f"[RSS] Page {page}")
        page_ok = False
        last_error = None

        for url in build_rss_urls(app_id, country, page):
            try:
                log(f"[RSS] GET {url}")
                r = session.get(url, timeout=timeout)
                log(f"[RSS] HTTP {r.status_code}")

                if r.status_code == 404:
                    last_error = f"404 Not Found: {url}"
                    continue
                if r.status_code >= 400:
                    last_error = f"HTTP {r.status_code}: {url}"
                    continue
                if not r.text.strip():
                    last_error = f"Empty response body: {url}"
                    continue

                try:
                    feed = feedparser.parse(r.text)
                except Exception as e:
                    last_error = f"RSS parsing failed: {e}"
                    continue

                entries = getattr(feed, "entries", []) or []
                log(f"[RSS] Entries found: {len(entries)}")
                if not entries:
                    last_error = f"No entries on page {page}"
                    continue

                rows = parse_rss_entries(feed, app_id, country, url)
                added = 0

                for row in rows:
                    rid = row.get("review_id")
                    dedupe_key = rid or (
                        str(row.get("author")),
                        str(row.get("title")),
                        str(row.get("review")),
                        str(row.get("date")),
                        str(row.get("rating"))
                    )
                    if dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)
                    all_rows.append(row)
                    added += 1
                    if len(all_rows) >= how_many:
                        break

                log(f"[RSS] Added from page {page}: {added}")
                page_ok = True
                break

            except 
