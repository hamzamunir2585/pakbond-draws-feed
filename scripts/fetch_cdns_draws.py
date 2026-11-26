#!/usr/bin/env python3
"""
Fetches National Savings Pakistan (CDNS) prize bond draw pages and official .txt
files from https://savings.gov.pk/download-draws/ (and linked denomination pages),
validates 6-digit winning numbers across all 6 active denominations, maintains a
strict 6-year rolling window across both yearly shards (`draws_<year>.json`) and
per-draw delta files (`draws/<id>.json`), and emits `manifest.json` with SHA-256
hashes so mobile clients only download changed draws after first-time bootstrap.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    import certifi
except ImportError:
    certifi = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parent.parent
INTERMEDIATES_PEM_PATH = Path(__file__).resolve().parent / "cdns_intermediates.pem"
MANIFEST_PATH = ROOT / "manifest.json"
SCHEDULE_PATH = ROOT / "schedule.json"
DRAWS_DIR = ROOT / "draws"
STATE_PATH = ROOT / "feed_state.json"

CDNS_DOWNLOAD_HUB = "https://savings.gov.pk/download-draws/"
SUPPORTED_DENOMINATIONS = {100, 200, 750, 1500, 25000, 40000}

MIN_HEALTHY_THIRD = {
    100: 800,
    200: 1600,
    750: 1200,
    1500: 1200,
    25000: 450,
    40000: 450,
}

# Known official reference points for quarterly SBP draw number inference
KNOWN_DRAW_BASELINES = {
    100: (56, dt.date(2026, 11, 16)),
    200: (107, dt.date(2026, 9, 15)),
    750: (108, dt.date(2026, 10, 15)),
    1500: (108, dt.date(2026, 11, 16)),
    25000: (23, dt.date(2026, 9, 10)),
    40000: (38, dt.date(2026, 9, 10)),
}

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    )
}

RS_AMOUNT_RE = re.compile(
    r"(?:(?:Rs\.?|PKR)\s*[0-9,]+(?:\s*/-)?|[0-9][0-9,]*\s*(?:PKR|Rs\.?|/-))",
    re.IGNORECASE,
)
ORDINAL_SUFFIX_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\b", re.IGNORECASE)
KNOWN_DRAW_CITIES = [
    "Dera Ghazi Khan",
    "Muzaffarabad",
    "Rawalpindi",
    "Faisalabad",
    "Bahawalpur",
    "Gujranwala",
    "Hyderabad",
    "Islamabad",
    "Peshawar",
    "Sargodha",
    "Abbottabad",
    "Karachi",
    "Sialkot",
    "Larkana",
    "Lahore",
    "Multan",
    "Quetta",
    "Sukkur",
    "Mirpur",
    "Mardan",
]
KNOWN_DENOM_PAGES = [
    (100, "https://savings.gov.pk/rs-100-prize-bond-draw/"),
    (100, "https://savings.gov.pk/rs-100-draws/"),
    (200, "https://savings.gov.pk/rs-200-prize-bond-draw/"),
    (200, "https://savings.gov.pk/rs-200-draws/"),
    (750, "https://savings.gov.pk/rs-750-prize-bond-draw/"),
    (750, "https://savings.gov.pk/rs-750-draws/"),
    (1500, "https://savings.gov.pk/rs-1500-prize-bond-draw/"),
    (1500, "https://savings.gov.pk/rs-1500-draws/"),
    (25000, "https://savings.gov.pk/premium-prize-bond-rs-25000/"),
    (25000, "https://savings.gov.pk/rs-25000-draws/"),
    (25000, "https://savings.gov.pk/rs-25000-prize-bond-draw/"),
    (40000, "https://savings.gov.pk/premium-prize-bond-rs-40000/"),
    (40000, "https://savings.gov.pk/rs-40000-draws/"),
    (40000, "https://savings.gov.pk/rs-40000-prize-bond-draw/"),
]


def build_http_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(REQUEST_HEADERS)
    base_ca: str | None = certifi.where() if certifi is not None else None
    if not base_ca or not Path(base_ca).exists():
        sys_ca = Path("/etc/ssl/certs/ca-certificates.crt")
        base_ca = str(sys_ca) if sys_ca.exists() else None
    if base_ca and INTERMEDIATES_PEM_PATH.exists():
        bundle_path = Path(tempfile.gettempdir()) / "pakbond_cdns_ca_bundle.pem"
        combined = Path(base_ca).read_text(encoding="utf-8") + "\n" + INTERMEDIATES_PEM_PATH.read_text(encoding="utf-8")
        bundle_path.write_text(combined, encoding="utf-8")
        session.verify = str(bundle_path)
    return session


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def six_year_cutoff_date(now: dt.datetime) -> dt.date:
    try:
        return now.date().replace(year=now.year - 6)
    except ValueError:
        return now.date().replace(year=now.year - 6, day=28)


def sha256_short(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    scheme = "https" if parsed.scheme in ("http", "https") else parsed.scheme
    netloc = parsed.netloc.lower()
    if netloc == "www.savings.gov.pk":
        netloc = "savings.gov.pk"
    return parsed._replace(scheme=scheme, netloc=netloc, fragment="").geturl()


def parse_iso_or_display_date(raw: str) -> dt.date | None:
    cleaned = ORDINAL_SUFFIX_RE.sub(r"\1", raw)
    cleaned = re.sub(r"[,]", " ", cleaned)
    text = re.sub(r"\s+", " ", cleaned).strip().rstrip(".;")
    if not text:
        return None
    for fmt in (
        "%Y-%m-%d",
        "%d %b %Y",
        "%d %B %Y",
        "%d-%m-%Y",
        "%d-%m-%y",
        "%d-%b-%Y",
        "%d-%B-%Y",
        "%d/%m/%Y",
        "%d/%m/%y",
        "%d.%m.%Y",
        "%d.%m.%y",
        "%d %m %Y",
    ):
        try:
            parsed = dt.datetime.strptime(text, fmt).date()
            if 1990 <= parsed.year <= 2100:
                return parsed
        except ValueError:
            continue
    return None


def validate_six_digit_list(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for item in values:
        token = str(item).strip()
        if re.fullmatch(r"\d{6}", token) and token not in seen:
            seen.add(token)
            cleaned.append(token)
    return cleaned


def is_healthy_draw(entry: dict[str, Any]) -> bool:
    denom = int(entry.get("denomination", 0))
    first_c = len(entry.get("first", []))
    second_c = len(entry.get("second", []))
    third_c = len(entry.get("third", []))
    if denom not in SUPPORTED_DENOMINATIONS or first_c == 0 or first_c > 3:
        return False
    expected_second = 5 if denom in (200, 25000) else 3
    if second_c < expected_second:
        return False
    return third_c >= MIN_HEALTHY_THIRD.get(denom, 400)


def load_existing_year_shards(cutoff: dt.date) -> dict[int, dict[tuple[int, int], dict[str, Any]]]:
    shards: dict[int, dict[tuple[int, int], dict[str, Any]]] = {}
    for path in ROOT.glob("draws_*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for entry in payload.get("draws", []):
            denom = int(entry.get("denomination", 0))
            draw_no = int(entry.get("drawNo", 0))
            parsed_date = parse_iso_or_display_date(str(entry.get("date", "")))
            if denom not in SUPPORTED_DENOMINATIONS or draw_no <= 0 or parsed_date is None:
                continue
            if parsed_date < cutoff:
                continue
            year = parsed_date.year
            entry["id"] = f"d_{draw_no}_{denom}"
            entry["date"] = parsed_date.isoformat()
            entry["first"] = validate_six_digit_list(entry.get("first", []))
            entry["second"] = validate_six_digit_list(entry.get("second", []))
            entry["third"] = validate_six_digit_list(entry.get("third", []))
            shards.setdefault(year, {})[(denom, draw_no)] = entry
    return shards


def load_schedule_entries(cutoff: dt.date) -> list[dict[str, Any]]:
    if not SCHEDULE_PATH.exists():
        return []
    try:
        sched_obj = json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))
        items = []
        for item in sched_obj.get("schedule", []):
            denom = int(item.get("denomination", 0))
            draw_no = int(item.get("drawNo", 0))
            d_date = parse_iso_or_display_date(str(item.get("date", "")))
            if denom in SUPPORTED_DENOMINATIONS and draw_no > 0 and (d_date is None or d_date >= cutoff):
                items.append(item)
        return items
    except Exception:
        return []


def load_url_state() -> dict[str, str]:
    if not STATE_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("completedUrls"), dict):
            return {str(k): str(v) for k, v in data["completedUrls"].items()}
    except Exception:
        pass
    return {}


def detect_denomination(text: str) -> int | None:
    for m in re.finditer(r"Rs\.?\s*([0-9,]{3,7})", text, re.IGNORECASE):
        val = int(m.group(1).replace(",", ""))
        if val in SUPPORTED_DENOMINATIONS:
            return val
    normalized = text.replace(",", "")
    for denom in sorted(SUPPORTED_DENOMINATIONS, reverse=True):
        if re.search(rf"(?<!\d){denom}(?!\d|\s*(?:st|nd|rd|th)\b)", normalized, re.IGNORECASE):
            return denom
    return None


def extract_link_date_hint(anchor_text: str, url: str) -> dt.date | None:
    for source in (anchor_text, url.rsplit("/", 1)[-1]):
        m = re.search(r"(\d{1,2}[./_-]\d{1,2}[./_-](?:20\d{2}|\d{2}))", source)
        if m:
            parsed = parse_iso_or_display_date(m.group(1).replace("_", "-"))
            if parsed:
                return parsed
        m_compact = re.search(r"(?<!\d)(\d{2})(\d{2})(\d{2})(?!\d)", source)
        if m_compact:
            dd, mm, yy = int(m_compact.group(1)), int(m_compact.group(2)), int(m_compact.group(3))
            if 1 <= dd <= 31 and 1 <= mm <= 12:
                try:
                    return dt.date(2000 + yy, mm, dd)
                except ValueError:
                    pass
    return None


QUARTER_OFFSETS = {
    100: 8051,
    200: 7999,
    750: 7999,
    1500: 7999,
    25000: 8083,
    40000: 8068,
}


def infer_draw_no_from_schedule(
    denom: int,
    draw_date: dt.date,
    schedule_items: list[dict[str, Any]],
) -> int | None:
    for item in schedule_items:
        if int(item.get("denomination", 0)) != denom:
            continue
        s_date = parse_iso_or_display_date(str(item.get("date", "")))
        if s_date and abs((s_date - draw_date).days) <= 6:
            return int(item.get("drawNo", 0))

    offset = QUARTER_OFFSETS.get(denom)
    if offset is None:
        return None
    quarter_total = draw_date.year * 4 + ((draw_date.month - 1) // 3)
    inferred = quarter_total - offset
    return inferred if 1 <= inferred <= 999 else None


def discover_cdns_result_links(
    cutoff: dt.date,
    schedule_items: list[dict[str, Any]],
    session: requests.Session | None = None,
) -> list[tuple[int, str, dt.date | None]]:
    """Crawls savings.gov.pk hub, denomination pages, pagination, and deterministic recent URLs."""
    http = session or build_http_session()
    found: dict[str, tuple[int, dt.date | None]] = {}
    visited_pages: set[str] = set()
    logged_errors = 0
    page_queue: list[tuple[str, int, int | None]] = [(normalize_url(CDNS_DOWNLOAD_HUB), 0, None)]
    for k_denom, k_url in KNOWN_DENOM_PAGES:
        page_queue.append((normalize_url(k_url), 1, k_denom))

    # Add deterministic URL guesses for due schedule items not yet announced
    today = utc_now().date()
    for item in schedule_items:
        denom = int(item.get("denomination", 0))
        d_date = parse_iso_or_display_date(str(item.get("date", "")))
        if denom in SUPPORTED_DENOMINATIONS and d_date and cutoff <= d_date <= today + dt.timedelta(days=1):
            yyyy = d_date.strftime("%Y")
            mm = d_date.strftime("%m")
            dd = d_date.strftime("%d")
            yy = d_date.strftime("%y")
            guesses = [
                f"https://savings.gov.pk/wp-content/uploads/{yyyy}/{mm}/{denom}-{dd}-{mm}-{yy}.txt",
                f"https://savings.gov.pk/wp-content/uploads/{yyyy}/{mm}/{denom}_{dd}{mm}{yy}_Txt.txt",
                f"https://savings.gov.pk/wp-content/uploads/{yyyy}/{mm}/{dd}-{mm}-{yyyy}-Rs.{denom}.txt",
            ]
            for g_url in guesses:
                found.setdefault(normalize_url(g_url), (denom, d_date))

    max_pages = 64
    while page_queue and len(visited_pages) < max_pages:
        page_url, depth, hint_denom = page_queue.pop(0)
        if page_url in visited_pages or depth > 2:
            continue
        visited_pages.add(page_url)
        try:
            resp = http.get(page_url, timeout=25)
            if resp.status_code != 200 or resp.content.startswith(b"%PDF"):
                if resp.status_code not in (200, 404) and logged_errors < 5:
                    print(f"[crawl] HTTP {resp.status_code} on {page_url}")
                    logged_errors += 1
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception as exc:
            if logged_errors < 5:
                print(f"[crawl] Error fetching {page_url}: {type(exc).__name__}: {exc}")
                logged_errors += 1
            continue

        for anchor in soup.select("a[href]"):
            href = normalize_url(urljoin(page_url, anchor["href"].strip()))
            if "savings.gov.pk" not in href:
                continue
            anchor_text = anchor.get_text(" ", strip=True)
            label = f"{anchor_text} {href}"
            lower_href = href.lower()
            lower_text = anchor_text.lower()
            is_wpdm_txt = (
                ("wpdmdl=" in lower_href or "download=" in lower_href)
                and ("txt" in lower_text or "text" in lower_text)
                and not lower_href.endswith(".pdf")
            )
            if ".txt" in lower_href or is_wpdm_txt:
                denom = detect_denomination(label) or hint_denom
                if denom:
                    hint_date = extract_link_date_hint(anchor_text, href)
                    if hint_date and hint_date < cutoff - dt.timedelta(days=31):
                        continue
                    found.setdefault(href, (denom, hint_date))
            elif (
                depth < 2
                and any(
                    k in lower_href
                    for k in ("prize-bond", "draw-list", "download-draws", "draws", "premium", "/page/")
                )
                and not lower_href.endswith((".pdf", ".jpg", ".png", ".zip"))
                and href not in visited_pages
            ):
                next_denom = detect_denomination(label) or hint_denom
                page_queue.append((href, depth + 1, next_denom))

    items = [(denom, url, h_date) for url, (denom, h_date) in found.items()]
    items.sort(key=lambda x: (x[2] or dt.date(1970, 1, 1), x[1]), reverse=True)
    return items[:350]


def repair_tier_counts_if_needed(
    denom: int,
    first_raw: list[str],
    second_raw: list[str],
    third_raw: list[str],
) -> tuple[list[str], list[str], list[str]]:
    second_count = 5 if denom in (200, 25000) else 3
    f_unique = validate_six_digit_list(first_raw)
    expected_first = 1 if (denom == 25000 and not second_raw and len(f_unique) == 4) else (2 if denom == 25000 else 1)
    f_set = set(f_unique)
    s_unique = [n for n in validate_six_digit_list(second_raw) if n not in f_set]

    if len(f_unique) > expected_first and not s_unique:
        first_clean = f_unique[:expected_first]
        overflow = f_unique[expected_first:]
        if not third_raw and len(overflow) > 20:
            second_clean = overflow[:second_count]
            third_clean = overflow[second_count:]
        else:
            second_clean = overflow
            upper_set = set(first_clean) | set(second_clean)
            third_clean = [n for n in validate_six_digit_list(third_raw) if n not in upper_set]
        return first_clean, second_clean, third_clean

    first_clean = (
        f_unique[:expected_first]
        if (len(f_unique) > expected_first and len(s_unique) >= second_count)
        else f_unique
    )
    upper_set = set(first_clean) | set(s_unique)
    t_unique = [n for n in validate_six_digit_list(third_raw) if n not in upper_set]
    if not t_unique and len(s_unique) > 20:
        return first_clean, s_unique[:second_count], s_unique[second_count:]
    return first_clean, s_unique, t_unique


def extract_text_draw_entry(
    hint_denom: int,
    source_url: str,
    schedule_items: list[dict[str, Any]],
    session: requests.Session | None = None,
) -> dict[str, Any] | None:
    http = session or build_http_session()
    urls_to_try = [source_url]
    if "://savings.gov.pk/" in source_url:
        urls_to_try.append(source_url.replace("://savings.gov.pk/", "://www.savings.gov.pk/", 1))

    best_partial: dict[str, Any] | None = None
    for candidate_url in urls_to_try:
        try:
            resp = http.get(candidate_url, timeout=25)
            if resp.status_code != 200 or resp.content.startswith(b"%PDF"):
                continue
            parsed = parse_draw_text_content(resp.text, hint_denom, candidate_url, schedule_items)
            if parsed is not None:
                if is_healthy_draw(parsed):
                    return parsed
                if best_partial is None:
                    best_partial = parsed
        except Exception:
            continue
    return best_partial


ANY_DATE_IN_LINE_RE = re.compile(
    r"\b(?:\d{1,2}(?:st|nd|rd|th)?[./-]\d{1,2}[./-]\d{2,4}|\d{4}-\d{2}-\d{2}|"
    r"\d{1,2}(?:st|nd|rd|th)?\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*,?\s+\d{4})\b",
    re.IGNORECASE,
)


def parse_draw_text_content(
    text: str,
    hint_denom: int,
    source_url: str,
    schedule_items: list[dict[str, Any]],
) -> dict[str, Any] | None:

    header = text[:3000]
    denom = detect_denomination(header) or hint_denom
    if denom not in SUPPORTED_DENOMINATIONS:
        return None

    date_match = re.search(
        r"(\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}-\d{2}-\d{2}|"
        r"\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9},?\s+\d{4})",
        header,
        re.IGNORECASE,
    )
    parsed_date = parse_iso_or_display_date(date_match.group(1)) if date_match else None
    if parsed_date is None:
        parsed_date = extract_link_date_hint("", source_url)
    if parsed_date is None:
        return None

    draw_match = re.search(
        r"(?:Draw\s*(?:No\.?|Number|#)?\s*[:\-]?\s*(\d{1,4})(?:st|nd|rd|th)?|"
        r"\b(\d{1,4})(?:st|nd|rd|th)\s*(?:COMMON\s+|REGULAR\s+|PREMIUM\s+)?DRAW\b)",
        header,
        re.IGNORECASE,
    )
    if draw_match:
        draw_no = int(draw_match.group(1) or draw_match.group(2))
    else:
        draw_no = infer_draw_no_from_schedule(denom, parsed_date, schedule_items) or 0
    if draw_no <= 0 or draw_no == denom:
        return None

    section = "HEADER"
    first_raw: list[str] = []
    second_raw: list[str] = []
    third_raw: list[str] = []
    fallback_raw: list[str] = []

    inline_marker_re = re.compile(
        r"\b(?:(first|1st)|(second|2nd)|(third|3rd)|([3-9]\d{2}|[1-9]\d{3})\s+prizes?)\b",
        re.IGNORECASE,
    )
    stop_section_re = re.compile(
        r"\b(?:previous\s+draws?|next\s+draws?|recent\s+draws?|other\s+draws?|"
        r"prize\s+bond\s+schedule|draw\s+schedule|related\s+posts?|leave\s+a\s+reply|"
        r"post\s+a\s+comment|comments?\s*\(\d+\))\b",
        re.IGNORECASE,
    )
    stop_first_prize_re = re.compile(r"\b(?:first|1st)\s+pri[zc]e\b", re.IGNORECASE)
    line_draw_no_re = re.compile(
        r"(?:\bDraw\s*(?:No\.?|Number|#)\s*[:\-]?\s*(\d{1,3})\b(?!\d)|"
        r"\b(\d{1,3})(?:st|nd|rd|th)\s+(?:COMMON\s+|REGULAR\s+|PREMIUM\s+)?DRAW\b)",
        re.IGNORECASE,
    )

    for idx, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()

        if len(third_raw) >= 20:
            if stop_section_re.search(lower):
                break
            if (
                first_raw
                and len(stripped) <= 100
                and stop_first_prize_re.search(lower)
                and "third" not in lower
                and "3rd" not in lower
            ):
                break
            if len(stripped) <= 140:
                later_draw_m = line_draw_no_re.search(stripped)
                if later_draw_m:
                    later_no = int(later_draw_m.group(1) or later_draw_m.group(2))
                    if later_no != draw_no and later_no != denom and 1 <= later_no <= 300:
                        break

        is_meta = lower.startswith(("draw", "date", "series", "page", "total", "contd", "held", "venue")) or any(
            k in lower for k in ("tel:", "telephone", "uan", "+92", "http://", "https://", "www.", "quorum", "committee")
        )

        allow_backwards_switch = section != "THIRD" or not third_raw
        markers = [] if (is_meta or not allow_backwards_switch) else list(inline_marker_re.finditer(stripped))
        if len(markers) >= 2:
            for m_idx, marker in enumerate(markers):
                end_pos = markers[m_idx + 1].start() if m_idx + 1 < len(markers) else len(stripped)
                seg_text = stripped[marker.start() : end_pos]
                if marker.group(1):
                    section = "FIRST"
                elif marker.group(2):
                    section = "SECOND"
                else:
                    section = "THIRD"
                seg_tokens = re.findall(r"(?<!\d)(\d{6})(?!\d)", RS_AMOUNT_RE.sub(" ", seg_text))
                if section == "FIRST":
                    first_raw.extend(seg_tokens)
                elif section == "SECOND":
                    second_raw.extend(seg_tokens)
                elif section == "THIRD":
                    third_raw.extend(seg_tokens)
            continue

        has_first = bool(re.search(r"\b(?:first|1st)\b", lower))
        has_second = bool(re.search(r"\b(?:second|2nd)\b", lower))
        has_third = bool(re.search(r"\b(?:third|3rd)\b", lower))
        if not is_meta:
            if allow_backwards_switch and re.search(r"\b(?:first|1st)\b|\b1\s+pri[zc]e\s+of\b", lower) and not has_second and not has_third:
                section = "FIRST"
            elif allow_backwards_switch and re.search(r"\b(?:second|2nd)\b|\b[35]\s+pri[zc]es?\s+of\b", lower) and not has_first and not has_third:
                section = "SECOND"
            elif re.search(r"\b(?:third|3rd)\b|\b(?:[3-9]\d{2}|[1-9]\d{3})\s+pri[zc]es?\b", lower) or (
                section == "SECOND" and re.search(r"\bpri[zc]es?\s+of\s+(?:rs|pkr)\b", lower) and not has_first and not has_second
            ):
                section = "THIRD"

        sanitized = RS_AMOUNT_RE.sub(" ", stripped)
        tokens = [] if is_meta else re.findall(r"(?<!\d)(\d{6})(?!\d)", sanitized)
        # Recover 4-5 digit zero-stripped serials on clean 1st/2nd lines (matching CdnsDrawTxtParser.kt)
        if (
            not tokens
            and not is_meta
            and section in ("FIRST", "SECOND")
            and "prize" not in lower
            and "price" not in lower
            and "rs" not in lower
            and "pkr" not in lower
            and not ANY_DATE_IN_LINE_RE.search(sanitized)
        ):
            short_tokens = re.findall(r"(?<!\d)(\d{4,5})(?!\d)", sanitized)
            tokens = [t.zfill(6) for t in short_tokens if int(t) > 0]

        if section == "FIRST":
            first_raw.extend(tokens)
        elif section == "SECOND":
            second_raw.extend(tokens)
        elif section == "THIRD":
            third_raw.extend(tokens)
        elif idx >= 3 and not is_meta:
            fallback_raw.extend(tokens)

    if first_raw:
        first_clean, second_clean, third_clean = repair_tier_counts_if_needed(denom, first_raw, second_raw, third_raw)
    else:
        combined = validate_six_digit_list(fallback_raw + second_raw + third_raw)
        if len(combined) < 10:
            return None
        second_count = 5 if denom in (200, 25000) else 3
        first_clean = combined[:1]
        second_clean = combined[1 : 1 + second_count]
        third_clean = combined[1 + second_count :]

    if not first_clean or (len(first_clean) + len(second_clean) + len(third_clean)) < 4:
        return None

    city = "Pakistan"
    header_city_slice = text[:1600]
    for known_city in KNOWN_DRAW_CITIES:
        if re.search(rf"\b{re.escape(known_city)}\b", header_city_slice, re.IGNORECASE):
            city = known_city
            break
    if city == "Pakistan":
        city_match = re.search(r"(?:SBP\s+BSC|held\s+at|city)\s*[:\-]?\s*([A-Za-z.\- ]{3,30})", header_city_slice, re.IGNORECASE)
        if city_match:
            candidate = city_match.group(1).strip()
            if "state bank" not in candidate.lower() and "pakistan" not in candidate.lower():
                city = candidate.title()

    return {
        "id": f"d_{draw_no}_{denom}",
        "drawNo": draw_no,
        "denomination": denom,
        "date": parsed_date.isoformat(),
        "city": city,
        "status": "Announced",
        "first": first_clean,
        "second": second_clean,
        "third": third_clean,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Update 6-year prize bond draw feed for Pak Bond Tracker.")
    parser.add_argument("--force-full", action="store_true", help="Re-crawl even previously healthy URLs.")
    args = parser.parse_args()

    now = utc_now()
    cutoff = six_year_cutoff_date(now)
    min_year = cutoff.year

    shards = load_existing_year_shards(cutoff)
    schedule_items = load_schedule_entries(cutoff)
    completed_url_map = {} if args.force_full else load_url_state()

    healthy_draw_ids = {
        entry["id"]
        for yr_map in shards.values()
        for entry in yr_map.values()
        if is_healthy_draw(entry)
    }
    healthy_date_pairs = {
        (int(entry["denomination"]), str(entry["date"]))
        for yr_map in shards.values()
        for entry in yr_map.values()
        if is_healthy_draw(entry)
    }

    http_session = build_http_session()
    discovered_links = discover_cdns_result_links(cutoff, schedule_items, session=http_session)
    partial_rejected = 0
    healthy_saved = 0
    print(f"[sync] Discovered {len(discovered_links)} candidate draw links (cutoff >= {cutoff.isoformat()})")

    for denom, asset_url, hint_date in discovered_links:
        if not args.force_full:
            mapped_id = completed_url_map.get(asset_url)
            if mapped_id and mapped_id in healthy_draw_ids:
                continue
            if hint_date and (denom, hint_date.isoformat()) in healthy_date_pairs:
                continue

        parsed = extract_text_draw_entry(denom, asset_url, schedule_items, session=http_session)
        if parsed is None:
            continue
        # Never publish incomplete / half-scraped draws to the GitHub feed
        if not is_healthy_draw(parsed):
            partial_rejected += 1
            continue
        d_date = parse_iso_or_display_date(parsed["date"])
        if d_date is None or d_date < cutoff:
            continue

        key = (parsed["denomination"], parsed["drawNo"])
        existing = shards.get(d_date.year, {}).get(key)
        if existing:
            existing_total = len(existing.get("first", [])) + len(existing.get("second", [])) + len(existing.get("third", []))
            parsed_total = len(parsed["first"]) + len(parsed["second"]) + len(parsed["third"])
            if is_healthy_draw(existing) and existing_total > parsed_total:
                continue

        shards.setdefault(d_date.year, {})[key] = parsed
        completed_url_map[asset_url] = parsed["id"]
        healthy_draw_ids.add(parsed["id"])
        healthy_date_pairs.add((int(parsed["denomination"]), str(parsed["date"])))
        healthy_saved += 1

    total_healthy = sum(len(yr_map) for yr, yr_map in shards.items() if yr >= min_year)
    print(f"[sync] Newly parsed healthy={healthy_saved}, partial_rejected={partial_rejected}, total_6y_draws={total_healthy}")
    if args.force_full and total_healthy == 0:
        raise SystemExit("ERROR: Zero healthy draws crawled from savings.gov.pk; aborting before commit.")

    fresh_threshold = now.date() - dt.timedelta(days=45)
    written_year_files: dict[str, str] = {}
    year_hashes: dict[str, str] = {}
    draw_files_index: dict[str, dict[str, Any]] = {}
    active_draw_filenames: set[str] = set()

    DRAWS_DIR.mkdir(parents=True, exist_ok=True)

    for stale_path in ROOT.glob("draws_*.json"):
        match = re.fullmatch(r"draws_(\d{4})\.json", stale_path.name)
        if match and int(match.group(1)) < min_year:
            stale_path.unlink(missing_ok=True)

    for year in sorted(shards.keys()):
        if year < min_year:
            continue
        entries = list(shards[year].values())
        for entry in entries:
            d_date = parse_iso_or_display_date(entry["date"])
            if d_date and d_date < fresh_threshold and entry.get("status") == "Announced":
                entry["status"] = "Past"
        entries.sort(key=lambda item: (item["date"], item["drawNo"]), reverse=True)

        # Write per-draw delta JSON files (`draws/<id>.json`) for single-draw updates
        for entry in entries:
            draw_hash = sha256_short(entry)
            draw_filename = f"{entry['id']}.json"
            active_draw_filenames.add(draw_filename)
            draw_rel_url = f"draws/{draw_filename}"
            draw_path = DRAWS_DIR / draw_filename

            should_write_draw = True
            if draw_path.exists():
                try:
                    old_draw = json.loads(draw_path.read_text(encoding="utf-8"))
                    if sha256_short(old_draw) == draw_hash:
                        should_write_draw = False
                except Exception:
                    pass
            if should_write_draw:
                draw_path.write_text(json.dumps(entry, indent=2) + "\n", encoding="utf-8")

            draw_files_index[entry["id"]] = {
                "url": draw_rel_url,
                "hash": draw_hash,
                "year": year,
                "denomination": entry["denomination"],
                "drawNo": entry["drawNo"],
                "status": entry["status"],
            }

        year_hash = sha256_short(entries)
        year_hashes[str(year)] = year_hash
        filename = f"draws_{year}.json"
        written_year_files[str(year)] = filename

        year_path = ROOT / filename
        write_year = True
        if year_path.exists():
            try:
                old_yr = json.loads(year_path.read_text(encoding="utf-8"))
                if sha256_short(old_yr.get("draws", [])) == year_hash:
                    write_year = False
            except Exception:
                pass
        if write_year:
            out_obj = {
                "year": year,
                "updatedAtUtc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "draws": entries,
            }
            year_path.write_text(json.dumps(out_obj, indent=2) + "\n", encoding="utf-8")

    # Prune expired >6y per-draw files
    for draw_file in DRAWS_DIR.glob("d_*.json"):
        if draw_file.name not in active_draw_filenames:
            draw_file.unlink(missing_ok=True)

    schedule_hash = ""
    if not SCHEDULE_PATH.exists():
        default_sched = {
            "updatedAtUtc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "schedule": [],
        }
        SCHEDULE_PATH.write_text(json.dumps(default_sched, indent=2) + "\n", encoding="utf-8")
        schedule_hash = sha256_short([])
    else:
        try:
            sched_obj = json.loads(SCHEDULE_PATH.read_text(encoding="utf-8-sig"))
            upcoming_items = []
            for item in sched_obj.get("schedule", []):
                denom = int(item.get("denomination", 0))
                draw_no = int(item.get("drawNo", 0))
                d_date = parse_iso_or_display_date(str(item.get("date", "")))
                already_published = any((denom, draw_no) in yr_map for yr_map in shards.values())
                if not already_published and (d_date is None or d_date >= cutoff):
                    upcoming_items.append(item)
            schedule_hash = sha256_short(upcoming_items)
            old_sched_hash = sha256_short(sched_obj.get("schedule", []))
            if old_sched_hash != schedule_hash:
                sched_obj["updatedAtUtc"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
                sched_obj["schedule"] = upcoming_items
                SCHEDULE_PATH.write_text(json.dumps(sched_obj, indent=2) + "\n", encoding="utf-8")
        except Exception:
            pass

    STATE_PATH.write_text(
        json.dumps({"completedUrls": dict(sorted(completed_url_map.items()))}, indent=2) + "\n",
        encoding="utf-8",
    )

    existing_manifest: dict[str, Any] = {}
    if MANIFEST_PATH.exists():
        try:
            existing_manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    content_unchanged = (
        existing_manifest.get("scheduleHash") == schedule_hash
        and existing_manifest.get("yearHashes") == year_hashes
        and existing_manifest.get("drawFiles") == draw_files_index
    )
    if content_unchanged and existing_manifest.get("version"):
        return

    existing_version = str(existing_manifest.get("version", ""))
    today_prefix = now.strftime("%Y.%m.%d")
    if existing_version.startswith(today_prefix + "."):
        try:
            build_num = int(existing_version.split(".")[-1]) + 1
        except ValueError:
            build_num = 1
    else:
        build_num = 1

    manifest_obj = {
        "version": f"{today_prefix}.{build_num}",
        "updatedAtUtc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "retentionYears": 6,
        "scheduleUrl": "schedule.json",
        "scheduleHash": schedule_hash,
        "yearFiles": written_year_files,
        "yearHashes": year_hashes,
        "drawFiles": draw_files_index,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest_obj, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
