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
        raise ValueError(
            f"Неверный country: {country}. Используйте 2-буквенный код, например 'ru', 'us', 'de'."
        )
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
        "app_id",
        "country",
        "source",
        "date",
        "rating",
        "review",
        "review_id",
        "author",
        "title",
        "version",
        "language_detected",
        "review_url",
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

        rows.append(
            {
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
            }
        )

    return rows


def fetch_reviews_rss(app_id, country, how_many, timeout=30):
    log("[SOURCE] Используем Apple RSS customer reviews feed.")
    log("[SOURCE][INFO] RSS может вернуть меньше отзывов, чем запрошено, потому что Apple отдаёт только ограниченное число последних отзывов.")

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari"
        }
    )

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
                response = session.get(url, timeout=timeout)
                log(f"[RSS] HTTP {response.status_code}")

                if response.status_code == 404:
                    last_error = f"404 Not Found: {url}"
                    continue

                if response.status_code >= 400:
                    last_error = f"HTTP {response.status_code}: {url}"
                    continue

                if not response.text.strip():
                    last_error = f"Empty response body: {url}"
                    continue

                try:
                    feed = feedparser.parse(response.text)
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
                        str(row.get("rating")),
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

            except requests.exceptions.RequestException as e:
                last_error = f"Network error: {e}"
                log(f"[RSS][WARNING] {last_error}")
            except Exception as e:
                last_error = f"Unexpected error: {e}"
                log(f"[RSS][WARNING] {last_error}")

        if not page_ok:
            log(f"[RSS][INFO] Останавливаемся на странице {page}. Причина: {last_error}")
            break

        time.sleep(0.4)

    df = pd.DataFrame(all_rows)
    df = ensure_schema(df)

    if df.empty:
        raise RuntimeError(
            "RSS вернул 0 отзывов. Проверьте app_id, country, наличие публичных отзывов или доступность RSS."
        )

    log(f"[SOURCE] RSS собрал отзывов: {len(df)}")
    return df


def apply_language_filter(df, language_filter):
    if df.empty:
        return df

    if language_filter is None:
        log("[LANG] language_filter не задан. Фильтрация по языку не выполняется.")
        return df

    log(f"[LANG] Применяем язык: {language_filter}")
    langs = []
    failed = 0

    for i, text in enumerate(df["review"].tolist(), start=1):
        lang, err = detect_language(text)
        langs.append(lang)

        if err is not None:
            failed += 1

        if i % 100 == 0:
            log(f"[LANG] Обработано {i} отзывов...")

    df = df.copy()
    df["language_detected"] = langs

    before = len(df)
    unclassified = df["language_detected"].isna().sum()
    df = df[df["language_detected"] == language_filter].copy()
    after = len(df)

    log(f"[LANG] До фильтра: {before}")
    log(f"[LANG] Не классифицировано: {unclassified}")
    log(f"[LANG] Ошибки детекции/короткие тексты: {failed}")
    log(f"[LANG] После фильтра '{language_filter}': {after}")

    if after == 0:
        log("[LANG][WARNING] Ни один отзыв не прошёл фильтр по языку.")

    return df


def deduplicate(df):
    if df.empty:
        return df, 0

    before = len(df)
    work = df.copy()

    work["review_norm"] = work["review"].fillna("").astype(str).str.strip().str.lower()
    work["author_norm"] = work["author"].fillna("").astype(str).str.strip().str.lower()
    work["title_norm"] = work["title"].fillna("").astype(str).str.strip().str.lower()
    work["date_norm"] = pd.to_datetime(work["date"], errors="coerce", utc=True).astype(str)

    has_id = work["review_id"].notna() & (work["review_id"].astype(str).str.strip() != "")
    with_id = work[has_id].drop_duplicates(subset=["review_id"], keep="first")
    without_id = work[~has_id].drop_duplicates(
        subset=["author_norm", "title_norm", "review_norm", "rating", "date_norm"],
        keep="first",
    )

    result = pd.concat([with_id, without_id], ignore_index=True)
    removed = before - len(result)
    result = result.drop(
        columns=["review_norm", "author_norm", "title_norm", "date_norm"],
        errors="ignore",
    )

    return result, removed


def fix_mojibake(text):
    if pd.isna(text):
        return text

    s = str(text)

    try:
        fixed = s.encode("latin-1", errors="ignore").decode("utf-8", errors="ignore")
        fixed = fixed.strip()
        return fixed if fixed else s
    except Exception:
        return s


def apply_mojibake_fix(df):
    df_fixed = df.copy()

    for col in ["review", "author", "title"]:
        if col in df_fixed.columns:
            df_fixed[col] = df_fixed[col].apply(fix_mojibake)

    return df_fixed


def dataframe_to_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")


def dataframe_to_xlsx_bytes(df: pd.DataFrame) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="reviews")
    output.seek(0)
    return output.getvalue()


def fetch_appstore_reviews(app_id, country="us", how_many=100, language_filter=None):
    st.session_state.logs = []

    log("=" * 80)
    log("[START] fetch_appstore_reviews")
    log(f"[INPUT] app_id={app_id}, country={country}, how_many={how_many}, language_filter={language_filter}")

    app_id, country, how_many, language_filter = validate_inputs(
        app_id, country, how_many, language_filter
    )

    df = fetch_reviews_rss(app_id, country, how_many)

    log(f"[POST] Raw fetched reviews: {len(df)}")
    df = apply_language_filter(df, language_filter)

    if df.empty:
        raise RuntimeError("После применения language_filter не осталось ни одного отзыва.")

    df, removed = deduplicate(df)
    log(f"[POST] Дубликатов удалено: {removed}")

    df["date"] = pd.to_datetime(df["date"], errors="coerce", utc=True)
    df["rating"] = pd.to_numeric(df["rating"], errors="coerce")
    df = df[df["review"].notna() & (df["review"].astype(str).str.strip() != "")].copy()

    if df.empty:
        raise RuntimeError("После чистки текста отзывов не осталось строк.")

    df = df.sort_values(by="date", ascending=False, na_position="last").reset_index(drop=True)
    log("[POST] Отсортировано по дате по убыванию.")

    df["date"] = df["date"].dt.strftime("%Y-%m-%d %H:%M:%S%z")
    df_fixed = apply_mojibake_fix(df)

    log("[POST] Попытка исправить крякозябры в полях review/author/title.")
    log(f"[RESULT] Итоговых строк: {len(df_fixed)}")
    log("[DONE] Успешное завершение.")
    log("=" * 80)

    return df_fixed


st.title("📱 Scraper отзывов App Store по app_id (RSS)")

st.markdown(
    "Введите `app_id`, страну и желаемое число отзывов. "
    "Сервис использует Apple RSS feed и может вернуть меньше отзывов, чем запрошено."
)

col1, col2, col3, col4 = st.columns(4)

with col1:
    app_id_input = st.text_input("App ID (число)", value="570060128")

with col2:
    country_input = st.text_input("Страна (2-буквенный код)", value="ru")

with col3:
    how_many_input = st.number_input(
        "Сколько отзывов собрать",
        min_value=1,
        max_value=1000,
        value=100,
    )

with col4:
    language_filter_input = st.text_input(
        "Фильтр по языку (например, ru, en, de)",
        value="",
    )

run_button = st.button("Собрать отзывы и подготовить файлы")

if run_button:
    try:
        df_result = fetch_appstore_reviews(
            app_id=app_id_input,
            country=country_input,
            how_many=how_many_input,
            language_filter=language_filter_input or None,
        )
        st.session_state.df = df_result
        st.success(f"Готово! Получено {len(df_result)} отзывов.")
    except Exception as e:
        st.error(f"Ошибка: {e}")

if st.session_state.df is not None:
    df_result = st.session_state.df

    st.subheader("Предпросмотр данных")
    st.dataframe(df_result.head(20))

    csv_bytes = dataframe_to_csv_bytes(df_result)
    xlsx_bytes = dataframe_to_xlsx_bytes(df_result)

    col_dl1, col_dl2 = st.columns(2)

    with col_dl1:
        st.download_button(
            label="⬇️ Скачать CSV",
            data=csv_bytes,
            file_name=f"appstore_reviews_{app_id_input}_{country_input}_{language_filter_input or 'all'}.csv",
            mime="text/csv",
        )

    with col_dl2:
        st.download_button(
            label="⬇️ Скачать Excel (.xlsx)",
            data=xlsx_bytes,
            file_name=f"appstore_reviews_{app_id_input}_{country_input}_{language_filter_input or 'all'}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

st.subheader("Логи выполнения")
st.text_area(
    label="Подробные логи",
    value="\n".join(st.session_state.logs),
    height=320,
)