#!/usr/bin/env python3
"""Bay Transit Board using BART Developer Program feeds.

  https://www.bart.gov/about/developers

  GTFS            https://www.bart.gov/dev/schedules/google_transit.zip
  GTFS-RT trips   https://api.bart.gov/gtfsrt/tripupdate.aspx
  GTFS-RT alerts  https://api.bart.gov/gtfsrt/alerts.aspx
  RSS alerts      https://www.bart.gov/schedules/advisories/advisories.xml

Usage:
  fetch.py ORIG DEST [--locate]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import secrets
import signal
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from bisect import bisect_left, bisect_right
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from zoneinfo import ZoneInfo

UA = "bay-transit-board/1.3 (+https://github.com/adg-ub/omarchy-bay-transit-board)"
GTFS_URL = "https://www.bart.gov/dev/schedules/google_transit.zip"
RT_TRIPS_URL = "https://api.bart.gov/gtfsrt/tripupdate.aspx"
RT_ALERTS_URL = "https://api.bart.gov/gtfsrt/alerts.aspx"
RSS_URL = "https://www.bart.gov/schedules/advisories/advisories.xml"
TZ = ZoneInfo("America/Los_Angeles")
NEAR_METERS = 1500
STATE = Path.home() / ".local/state/omarchy/bay-transit-board"
HERE = Path(__file__).resolve().parent
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

KIB = 1024
MIB = 1024 * KIB
NETWORK_CHUNK_BYTES = 64 * KIB
MAX_GTFS_DOWNLOAD_BYTES = 16 * MIB
MAX_GTFS_ARCHIVE_ENTRIES = 64
MAX_GTFS_EXPANDED_BYTES = 64 * MIB
MAX_GTFS_ENTRY_BYTES = 32 * MIB
MAX_GTFS_COMPRESSION_RATIO = 100
MAX_GTFS_CACHE_BYTES = 64 * MIB
MAX_RT_BYTES = 16 * MIB
MAX_ALERT_BYTES = 2 * MIB
MAX_RSS_BYTES = 2 * MIB
MAX_LOCATION_BYTES = 64 * KIB
MAX_CSV_FIELD_BYTES = 64 * KIB
MAX_GTFS_ID_CHARS = 128
MAX_GTFS_NAME_CHARS = 256
MAX_GTFS_SERVICES = 10_000
MAX_GTFS_TRIPS = 25_000
MAX_STOPS_PER_TRIP = 512
MAX_PROTO_FIELDS = 50_000
MAX_PROTO_FIELD_BYTES = 2 * MIB
MAX_RT_ENTITIES = 10_000
MAX_RT_STOPS_PER_TRIP = 512
MAX_ALERT_ENTITIES = 512
MAX_ALERTS = 8
MAX_ALERT_CHARS = 2_048
MAX_RSS_ITEMS = 64
MAX_MATCHES = 2_048
MAX_LINES = 16
MAX_DEPARTURE_GROUPS = 16
MAX_TRANSFER_CANDIDATES = 50_000
MAX_TRANSFER_PAIRINGS = 10_000
MAX_TRANSFER_PAIRING_ATTEMPTS = 100_000
MAX_TRANSFER_JOURNEYS = 64
MIN_TRANSFER_SECONDS = 2 * 60
MAX_TRANSFER_SECONDS = 45 * 60
TRANSFER_SEARCH_SECONDS = 4 * 3600
MAX_OUTPUT_BYTES = 256 * KIB
MAX_ERROR_CHARS = 512
CACHE_SCHEMA_VERSION = 1
CACHE_NAME = "gtfs.json"
ARCHIVE_NAME = "google_transit.zip"
GTFS_MAX_AGE_SECONDS = 12 * 3600
GTFS_MAX_STALE_SECONDS = 7 * 24 * 3600
MAX_CLOCK_SKEW_SECONDS = 5 * 60
GTFS_RETRY_BACKOFF_SECONDS = 5 * 60
GTFS_MAX_RETRY_MARKER_SECONDS = 15 * 60

OPEN_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
OPEN_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
CREATE_FILE_FLAGS = (
    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
)

CSV_LIMITS = {
    "routes.txt": (1 * MIB, 1_000),
    "stops.txt": (2 * MIB, 5_000),
    "trips.txt": (8 * MIB, 25_000),
    "stop_times.txt": (16 * MIB, 250_000),
    "calendar.txt": (2 * MIB, 5_000),
    "calendar_dates.txt": (8 * MIB, 50_000),
}
REQUIRED_GTFS_FILES = frozenset(name for name in CSV_LIMITS if name != "calendar_dates.txt")


class FeedLimitError(ValueError):
    """A remote feed exceeded a configured safety boundary."""


class RequestDeadlineError(TimeoutError):
    """A provider request exceeded its whole-request deadline."""


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("provider URL must be an unauthenticated HTTPS URL")
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise ValueError("provider URL has an invalid port") from exc
    return parsed.scheme, parsed.hostname.lower(), port


class SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow a short HTTPS redirect chain only within the original origin."""

    max_redirections = 3
    max_repeats = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        resolved = urllib.parse.urljoin(req.full_url, newurl)
        if _origin(resolved) != _origin(req.full_url):
            raise urllib.error.HTTPError(
                req.full_url,
                code,
                "cross-origin provider redirect refused",
                headers,
                fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, resolved)


URL_OPENER = urllib.request.build_opener(SameOriginRedirectHandler())


@contextmanager
def _request_deadline(seconds: float):
    """Interrupt DNS, connect, redirects, TLS, and reads at one deadline."""
    if seconds <= 0:
        raise ValueError("request deadline must be positive")

    def expired(_signum, _frame):
        raise RequestDeadlineError(f"provider request exceeded {seconds:g}s deadline")

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            elapsed = time.monotonic() - started
            signal.setitimer(
                signal.ITIMER_REAL,
                max(0.000001, previous_timer[0] - elapsed),
                previous_timer[1],
            )


def _declared_length(resp) -> int | None:
    value = resp.headers.get("Content-Length")
    if value is None:
        return None
    try:
        length = int(value)
    except (TypeError, ValueError) as exc:
        raise FeedLimitError("invalid Content-Length") from exc
    if length < 0:
        raise FeedLimitError("negative Content-Length")
    return length


def _open_provider(url: str, *, timeout: float):
    expected_origin = _origin(url)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    response = URL_OPENER.open(req, timeout=timeout)
    final_url = response.geturl() if hasattr(response, "geturl") else url
    if _origin(final_url) != expected_origin:
        response.close()
        raise urllib.error.URLError("provider response escaped its original origin")
    return response


def _copy_response(resp, target: BinaryIO, *, max_bytes: int, label: str) -> int:
    declared = _declared_length(resp)
    if declared is not None and declared > max_bytes:
        raise FeedLimitError(f"{label} exceeds {max_bytes} bytes")
    written = 0
    while written <= max_bytes:
        remaining = max_bytes + 1 - written
        chunk = resp.read(min(NETWORK_CHUNK_BYTES, remaining))
        if not chunk:
            return written
        target.write(chunk)
        written += len(chunk)
    raise FeedLimitError(f"{label} exceeds {max_bytes} bytes")


def get_bytes(url: str, *, max_bytes: int, timeout: float = 12) -> bytes:
    payload = io.BytesIO()
    with _request_deadline(timeout):
        with _open_provider(url, timeout=timeout) as resp:
            _copy_response(resp, payload, max_bytes=max_bytes, label="response")
    return payload.getvalue()


def get_text(url: str, *, max_bytes: int, timeout: float = 12) -> str:
    return get_bytes(url, max_bytes=max_bytes, timeout=timeout).decode("utf-8", "replace")


def download_to_fd(url: str, fd: int, *, max_bytes: int, timeout: float = 20) -> int:
    with os.fdopen(os.dup(fd), "wb", closefd=True) as target:
        with _request_deadline(timeout):
            with _open_provider(url, timeout=timeout) as resp:
                written = _copy_response(
                    resp,
                    target,
                    max_bytes=max_bytes,
                    label="download",
                )
        target.flush()
        os.fsync(target.fileno())
    os.lseek(fd, 0, os.SEEK_SET)
    return written


# ---- protobuf (stdlib) -------------------------------------------------------

def _varint(buf: bytes, i: int):
    result = 0
    shift = 0
    for _ in range(10):
        if i >= len(buf):
            raise FeedLimitError("truncated protobuf varint")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            return result, i
        shift += 7
    raise FeedLimitError("oversized protobuf varint")


def _decode(buf: bytes, *, max_bytes: int = MAX_PROTO_FIELD_BYTES) -> dict[int, list]:
    if len(buf) > max_bytes:
        raise FeedLimitError("protobuf message exceeds nested-message limit")
    i = 0
    fields: dict[int, list] = {}
    n = len(buf)
    count = 0
    while i < n:
        count += 1
        if count > MAX_PROTO_FIELDS:
            raise FeedLimitError(f"protobuf message has more than {MAX_PROTO_FIELDS} fields")
        key, i = _varint(buf, i)
        field, wtype = key >> 3, key & 7
        if field == 0:
            raise FeedLimitError("protobuf contains field zero")
        if wtype == 0:
            val, i = _varint(buf, i)
        elif wtype == 1:
            if i + 8 > n:
                raise FeedLimitError("truncated fixed64 protobuf field")
            val = int.from_bytes(buf[i : i + 8], "little")
            i += 8
        elif wtype == 2:
            ln, i = _varint(buf, i)
            if ln > MAX_PROTO_FIELD_BYTES:
                raise FeedLimitError("protobuf field exceeds nested-field limit")
            if i + ln > n:
                raise FeedLimitError("truncated length-delimited protobuf field")
            val = buf[i : i + ln]
            i += ln
        elif wtype == 5:
            if i + 4 > n:
                raise FeedLimitError("truncated fixed32 protobuf field")
            val = int.from_bytes(buf[i : i + 4], "little")
            i += 4
        else:
            raise FeedLimitError(f"unsupported protobuf wire type {wtype}")
        fields.setdefault(field, []).append(val)
    return fields


def _str(fields: dict, n: int, *, max_chars: int = MAX_GTFS_NAME_CHARS) -> str:
    vals = fields.get(n) or []
    if not vals:
        return ""
    v = vals[0]
    if isinstance(v, (bytes, bytearray)) and len(v) > max_chars * 4:
        raise FeedLimitError("protobuf string exceeds configured limit")
    text = v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else str(v)
    if len(text) > max_chars:
        raise FeedLimitError("protobuf string exceeds configured limit")
    return text


def _int(fields: dict, n: int, default: int = 0) -> int:
    vals = fields.get(n) or []
    return int(vals[0]) if vals else default


def _msg(fields: dict, n: int) -> dict:
    vals = fields.get(n) or []
    if not vals:
        return {}
    v = vals[0]
    return _decode(v) if isinstance(v, (bytes, bytearray)) else {}


def _msgs(fields: dict, n: int, *, limit: int) -> list[dict]:
    values = fields.get(n) or []
    if len(values) > limit:
        raise FeedLimitError(f"protobuf field {n} has more than {limit} messages")
    out = []
    for v in values:
        if isinstance(v, (bytes, bytearray)):
            out.append(_decode(v))
    return out


def parse_trip_updates(buf: bytes) -> dict[str, dict]:
    """trip_id -> {route_id, stop_times: {stop_id: unix_departure}}"""
    root = _decode(buf, max_bytes=MAX_RT_BYTES)
    out: dict[str, dict] = {}
    for entity in _msgs(root, 2, limit=MAX_RT_ENTITIES):
        tu = _msg(entity, 3)
        if not tu:
            continue
        trip = _msg(tu, 1)
        trip_id = _str(trip, 1, max_chars=MAX_GTFS_ID_CHARS)
        if not trip_id:
            continue
        stops = {}
        # Spec puts StopTimeUpdate at field 3; BART's feed emits them at field 2.
        stop_updates = _msgs(tu, 2, limit=MAX_RT_STOPS_PER_TRIP)
        if not stop_updates:
            stop_updates = _msgs(tu, 3, limit=MAX_RT_STOPS_PER_TRIP)
        for stu in stop_updates:
            stop_id = _str(stu, 4, max_chars=MAX_GTFS_ID_CHARS)
            dep = _msg(stu, 3) or _msg(stu, 2)
            unix = _int(dep, 2)
            if stop_id and unix:
                stops[stop_id] = unix
        if trip_id:
            out[trip_id] = {
                "route_id": _str(trip, 5, max_chars=MAX_GTFS_ID_CHARS),
                "stops": stops,
            }
    return out


def _translated(fields: dict, n: int) -> str:
    ts = _msg(fields, n)
    for tr in _msgs(ts, 1, limit=32):
        text = _str(tr, 1, max_chars=MAX_ALERT_CHARS).strip()
        if text:
            return text
    return ""


def parse_alerts(buf: bytes) -> list[str]:
    root = _decode(buf, max_bytes=MAX_ALERT_BYTES)
    out = []
    for entity in _msgs(root, 2, limit=MAX_ALERT_ENTITIES):
        alert = _msg(entity, 5)
        if not alert:
            continue
        text = _translated(alert, 11) or _translated(alert, 10)
        if text and "no delays" not in text.lower():
            out.append(text)
            if len(out) >= MAX_ALERTS:
                break
    return out


def parse_rss(xml_text: str) -> list[str]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    items = root.findall(".//item")
    if len(items) > MAX_RSS_ITEMS:
        items = items[:MAX_RSS_ITEMS]
    for item in items:
        title = (item.findtext("title") or "").strip()
        desc = (item.findtext("description") or "").strip()
        text = desc or title
        if len(text) > MAX_ALERT_CHARS:
            text = text[:MAX_ALERT_CHARS]
        if text and "no delays" not in text.lower() and "has been issued" not in text.lower():
            out.append(text)
        elif desc and "no delays" not in desc.lower():
            out.append(desc[:MAX_ALERT_CHARS])
        if len(out) >= MAX_ALERTS:
            break
    return out


# ---- GTFS cache ---------------------------------------------------------------

def _validate_gtfs_archive(zf: zipfile.ZipFile) -> set[str]:
    infos = zf.infolist()
    if len(infos) > MAX_GTFS_ARCHIVE_ENTRIES:
        raise FeedLimitError(f"GTFS archive has more than {MAX_GTFS_ARCHIVE_ENTRIES} entries")

    names: set[str] = set()
    expanded = 0
    for info in infos:
        path = PurePosixPath(info.filename)
        if path.is_absolute() or ".." in path.parts:
            raise FeedLimitError(f"unsafe GTFS entry path: {info.filename}")
        if info.filename in names:
            raise FeedLimitError(f"duplicate GTFS entry: {info.filename}")
        names.add(info.filename)

        if info.flag_bits & 0x1:
            raise FeedLimitError(f"encrypted GTFS entry: {info.filename}")
        if info.file_size > MAX_GTFS_ENTRY_BYTES:
            raise FeedLimitError(f"GTFS entry exceeds {MAX_GTFS_ENTRY_BYTES} bytes: {info.filename}")
        expanded += info.file_size
        if expanded > MAX_GTFS_EXPANDED_BYTES:
            raise FeedLimitError(f"GTFS archive expands beyond {MAX_GTFS_EXPANDED_BYTES} bytes")
        if info.file_size and (
            info.compress_size == 0
            or info.file_size > info.compress_size * MAX_GTFS_COMPRESSION_RATIO
        ):
            raise FeedLimitError(f"suspicious compression ratio for GTFS entry: {info.filename}")

    missing = REQUIRED_GTFS_FILES - names
    if missing:
        raise FeedLimitError(f"GTFS archive is missing: {', '.join(sorted(missing))}")
    return names


def _csv_rows(zf: zipfile.ZipFile, name: str):
    max_bytes, max_rows = CSV_LIMITS[name]
    info = zf.getinfo(name)
    if info.file_size > max_bytes:
        raise FeedLimitError(f"{name} exceeds {max_bytes} expanded bytes")

    with zf.open(info) as fh:
        text = io.TextIOWrapper(fh, encoding="utf-8-sig", newline="")
        reader = csv.DictReader(text)
        for count, row in enumerate(reader, start=1):
            if count > max_rows:
                raise FeedLimitError(f"{name} exceeds {max_rows} data rows")
            yield row


def _field(row: dict, name: str, *, max_chars: int, required: bool = False) -> str:
    value = str(row.get(name) or "").strip()
    if required and not value:
        raise FeedLimitError(f"GTFS field {name} is required")
    if len(value) > max_chars:
        raise FeedLimitError(f"GTFS field {name} exceeds {max_chars} characters")
    return value


def _validate_hms(value: str) -> str:
    parts = value.split(":")
    if (
        len(parts) != 3
        or any(not part.isdigit() for part in parts)
        or not 0 <= int(parts[0]) <= 72
        or not 0 <= int(parts[1]) <= 59
        or not 0 <= int(parts[2]) <= 59
    ):
        raise FeedLimitError("invalid GTFS departure time")
    return value


def _zip_size(source) -> int:
    if isinstance(source, (str, os.PathLike)):
        return os.stat(source, follow_symlinks=False).st_size
    if isinstance(source, (bytes, bytearray)):
        return len(source)
    return os.fstat(source.fileno()).st_size


def parse_gtfs(source) -> dict:
    if _zip_size(source) > MAX_GTFS_DOWNLOAD_BYTES:
        raise FeedLimitError(f"GTFS download exceeds {MAX_GTFS_DOWNLOAD_BYTES} bytes")

    csv.field_size_limit(MAX_CSV_FIELD_BYTES)
    zip_source = io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source
    with zipfile.ZipFile(zip_source) as zf:
        names = _validate_gtfs_archive(zf)

        parent_of = {}
        stations = []
        for stop in _csv_rows(zf, "stops.txt"):
            sid = _field(stop, "stop_id", max_chars=MAX_GTFS_ID_CHARS, required=True)
            parent = _field(stop, "parent_station", max_chars=MAX_GTFS_ID_CHARS)
            if stop.get("location_type") == "1":
                name = _field(
                    stop,
                    "stop_name",
                    max_chars=MAX_GTFS_NAME_CHARS,
                    required=True,
                )
                stations.append({
                    "abbr": sid,
                    "name": name,
                    "lat": float(stop["stop_lat"]),
                    "lon": float(stop["stop_lon"]),
                })
                parent_of[sid] = sid
            elif parent:
                parent_of[sid] = parent
            else:
                parent_of[sid] = sid

        routes = {}
        for route in _csv_rows(zf, "routes.txt"):
            rid = _field(route, "route_id", max_chars=MAX_GTFS_ID_CHARS, required=True)
            color = _field(route, "route_color", max_chars=7).upper() or "888888"
            if color.startswith("#"):
                color = color[1:]
            if len(color) not in (3, 6) or any(ch not in "0123456789ABCDEF" for ch in color):
                color = "888888"
            short_name = _field(route, "route_short_name", max_chars=MAX_GTFS_NAME_CHARS)
            long_name = _field(route, "route_long_name", max_chars=MAX_GTFS_NAME_CHARS)
            routes[rid] = {
                "id": rid,
                "name": short_name or long_name or rid,
                "fullName": long_name,
                "hexcolor": "#" + color,
            }

        services = {}
        for row in _csv_rows(zf, "calendar.txt"):
            service_id = _field(
                row,
                "service_id",
                max_chars=MAX_GTFS_ID_CHARS,
                required=True,
            )
            services[service_id] = {
                "days": {day: row.get(day) == "1" for day in WEEKDAYS},
                "start": _field(row, "start_date", max_chars=8, required=True),
                "end": _field(row, "end_date", max_chars=8, required=True),
                "added": set(),
                "removed": set(),
            }
        if "calendar_dates.txt" in names:
            for row in _csv_rows(zf, "calendar_dates.txt"):
                sid = _field(
                    row,
                    "service_id",
                    max_chars=MAX_GTFS_ID_CHARS,
                    required=True,
                )
                rec = services.setdefault(sid, {"days": {d: False for d in WEEKDAYS}, "start": "00000000", "end": "99999999", "added": set(), "removed": set()})
                if len(services) > MAX_GTFS_SERVICES:
                    raise FeedLimitError(f"GTFS has more than {MAX_GTFS_SERVICES} services")
                service_date = _field(row, "date", max_chars=8, required=True)
                if row.get("exception_type") == "1":
                    rec["added"].add(service_date)
                else:
                    rec["removed"].add(service_date)

        times_by_trip: dict[str, list] = {}
        for row in _csv_rows(zf, "stop_times.txt"):
            trip_id = _field(
                row,
                "trip_id",
                max_chars=MAX_GTFS_ID_CHARS,
                required=True,
            )
            stop_id = _field(
                row,
                "stop_id",
                max_chars=MAX_GTFS_ID_CHARS,
                required=True,
            )
            departure = _field(row, "departure_time", max_chars=16) or _field(
                row,
                "arrival_time",
                max_chars=16,
            )
            _validate_hms(departure)
            rows = times_by_trip.setdefault(trip_id, [])
            if len(times_by_trip) > MAX_GTFS_TRIPS:
                raise FeedLimitError(f"GTFS has more than {MAX_GTFS_TRIPS} trip schedules")
            if len(rows) >= MAX_STOPS_PER_TRIP:
                raise FeedLimitError(
                    f"GTFS trip has more than {MAX_STOPS_PER_TRIP} stop times"
                )
            rows.append((
                int(row["stop_sequence"] or 0),
                parent_of.get(stop_id, stop_id),
                stop_id,
                departure,
            ))
        for trip_id, rows in times_by_trip.items():
            rows.sort()
            times_by_trip[trip_id] = [(abbr, stop_id, dep) for _, abbr, stop_id, dep in rows]

        trips = []
        for trip in _csv_rows(zf, "trips.txt"):
            trip_id = _field(
                trip,
                "trip_id",
                max_chars=MAX_GTFS_ID_CHARS,
                required=True,
            )
            stops = times_by_trip.get(trip_id) or []
            if not stops:
                continue
            trips.append({
                "id": trip_id,
                "route": _field(
                    trip,
                    "route_id",
                    max_chars=MAX_GTFS_ID_CHARS,
                    required=True,
                ),
                "service": _field(
                    trip,
                    "service_id",
                    max_chars=MAX_GTFS_ID_CHARS,
                    required=True,
                ),
                "headsign": _field(
                    trip,
                    "trip_headsign",
                    max_chars=MAX_GTFS_NAME_CHARS,
                ),
                "stops": stops,
            })

    data = {"stations": stations, "routes": routes, "services": services, "trips": trips}
    _validate_gtfs_data(data)
    return data


def _checked_text(value, *, label: str, max_chars: int) -> str:
    if not isinstance(value, str) or len(value) > max_chars:
        raise FeedLimitError(f"invalid cached GTFS {label}")
    return value


def _validate_gtfs_data(data: dict) -> None:
    if not isinstance(data, dict):
        raise FeedLimitError("cached GTFS root must be an object")
    stations = data.get("stations")
    routes = data.get("routes")
    services = data.get("services")
    trips = data.get("trips")
    if not isinstance(stations, list) or len(stations) > CSV_LIMITS["stops.txt"][1]:
        raise FeedLimitError("invalid cached GTFS stations")
    if not isinstance(routes, dict) or len(routes) > CSV_LIMITS["routes.txt"][1]:
        raise FeedLimitError("invalid cached GTFS routes")
    if not isinstance(services, dict) or len(services) > MAX_GTFS_SERVICES:
        raise FeedLimitError("invalid cached GTFS services")
    if not isinstance(trips, list) or len(trips) > MAX_GTFS_TRIPS:
        raise FeedLimitError("invalid cached GTFS trips")

    for station in stations:
        if not isinstance(station, dict):
            raise FeedLimitError("invalid cached GTFS station")
        _checked_text(
            station.get("abbr"),
            label="station id",
            max_chars=MAX_GTFS_ID_CHARS,
        )
        _checked_text(
            station.get("name"),
            label="station name",
            max_chars=MAX_GTFS_NAME_CHARS,
        )
        for key in ("lat", "lon"):
            value = station.get(key)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or (key == "lat" and not -90 <= value <= 90)
                or (key == "lon" and not -180 <= value <= 180)
            ):
                raise FeedLimitError(f"invalid cached GTFS station {key}")

    for route_id, route in routes.items():
        _checked_text(route_id, label="route id", max_chars=MAX_GTFS_ID_CHARS)
        if not isinstance(route, dict):
            raise FeedLimitError("invalid cached GTFS route")
        for key, limit in (
            ("id", MAX_GTFS_ID_CHARS),
            ("name", MAX_GTFS_NAME_CHARS),
            ("fullName", MAX_GTFS_NAME_CHARS),
            ("hexcolor", 8),
        ):
            _checked_text(route.get(key), label=f"route {key}", max_chars=limit)

    for service_id, service in services.items():
        _checked_text(service_id, label="service id", max_chars=MAX_GTFS_ID_CHARS)
        if not isinstance(service, dict) or not isinstance(service.get("days"), dict):
            raise FeedLimitError("invalid cached GTFS service")
        if any(
            not isinstance(service["days"].get(day), bool)
            for day in WEEKDAYS
        ):
            raise FeedLimitError("invalid cached GTFS service days")
        for key in ("start", "end"):
            value = _checked_text(service.get(key), label=f"service {key}", max_chars=8)
            if len(value) != 8 or not value.isdigit():
                raise FeedLimitError(f"invalid cached GTFS service {key}")
        for key in ("added", "removed"):
            values = service.get(key)
            if not isinstance(values, (list, set)) or len(values) > CSV_LIMITS["calendar_dates.txt"][1]:
                raise FeedLimitError(f"invalid cached GTFS service {key}")
            for value in values:
                text = _checked_text(value, label=f"service {key}", max_chars=8)
                if len(text) != 8 or not text.isdigit():
                    raise FeedLimitError(f"invalid cached GTFS service {key}")

    total_stops = 0
    for trip in trips:
        if not isinstance(trip, dict):
            raise FeedLimitError("invalid cached GTFS trip")
        for key in ("id", "route", "service"):
            _checked_text(
                trip.get(key),
                label=f"trip {key}",
                max_chars=MAX_GTFS_ID_CHARS,
            )
        _checked_text(
            trip.get("headsign"),
            label="trip headsign",
            max_chars=MAX_GTFS_NAME_CHARS,
        )
        stops = trip.get("stops")
        if not isinstance(stops, list) or len(stops) > MAX_STOPS_PER_TRIP:
            raise FeedLimitError("invalid cached GTFS trip stops")
        total_stops += len(stops)
        if total_stops > CSV_LIMITS["stop_times.txt"][1]:
            raise FeedLimitError("cached GTFS has too many stop times")
        for stop in stops:
            if not isinstance(stop, (list, tuple)) or len(stop) != 3:
                raise FeedLimitError("invalid cached GTFS stop time")
            _checked_text(stop[0], label="parent stop id", max_chars=MAX_GTFS_ID_CHARS)
            _checked_text(stop[1], label="stop id", max_chars=MAX_GTFS_ID_CHARS)
            departure = _checked_text(stop[2], label="departure", max_chars=16)
            _validate_hms(departure)


def _validate_directory(fd: int, *, label: str, owners, private: bool) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise FeedLimitError(f"{label} is not a directory")
    if info.st_uid not in owners:
        raise FeedLimitError(f"{label} has an unexpected owner")
    if info.st_mode & 0o022:
        raise FeedLimitError(f"{label} is writable by another user")
    if private:
        os.fchmod(fd, 0o700)


@contextmanager
def _secure_state_directory():
    """Open STATE one component at a time without following symlinks."""
    state_path = STATE.expanduser()
    home_path = Path.home()
    try:
        relative_state = state_path.relative_to(home_path)
    except ValueError as exc:
        raise FeedLimitError("state directory must remain below the home directory") from exc

    uid = os.getuid()
    fd = -1
    try:
        fd = os.open("/", OPEN_DIR_FLAGS)
        home_parts = home_path.parts[1:]
        for index, component in enumerate(home_parts):
            next_fd = os.open(component, OPEN_DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            is_home = index == len(home_parts) - 1
            _validate_directory(
                fd,
                label="home directory" if is_home else "home parent",
                owners=(uid,) if is_home else (0, uid),
                private=False,
            )

        for index, component in enumerate(relative_state.parts):
            try:
                next_fd = os.open(component, OPEN_DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(component, mode=0o700, dir_fd=fd)
                next_fd = os.open(component, OPEN_DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            _validate_directory(
                fd,
                label="state directory",
                owners=(uid,),
                private=index == len(relative_state.parts) - 1,
            )
    except OSError as exc:
        if fd >= 0:
            os.close(fd)
        raise FeedLimitError(f"unsafe state directory: {exc.strerror or exc}") from exc
    except Exception:
        if fd >= 0:
            os.close(fd)
        raise

    try:
        yield fd
    finally:
        os.close(fd)


def _safe_leaf(name: str) -> None:
    if not name or name in (".", "..") or "/" in name or "\x00" in name:
        raise FeedLimitError("invalid state filename")


def _read_fd(fd: int, *, max_bytes: int) -> bytes:
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_nlink != 1
    ):
        raise FeedLimitError("state file must be a regular file owned by the current user")
    os.fchmod(fd, 0o600)
    if info.st_size > max_bytes:
        raise FeedLimitError(f"state file exceeds {max_bytes} bytes")
    os.lseek(fd, 0, os.SEEK_SET)
    payload = bytearray()
    while len(payload) <= max_bytes:
        chunk = os.read(fd, min(NETWORK_CHUNK_BYTES, max_bytes + 1 - len(payload)))
        if not chunk:
            return bytes(payload)
        payload.extend(chunk)
    raise FeedLimitError(f"state file exceeds {max_bytes} bytes")


def _read_state_file(dir_fd: int, name: str, *, max_bytes: int):
    _safe_leaf(name)
    try:
        fd = os.open(name, OPEN_FILE_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise FeedLimitError(f"unsafe state file {name}: {exc.strerror or exc}") from exc
    try:
        payload = _read_fd(fd, max_bytes=max_bytes)
        return payload, os.fstat(fd)
    finally:
        os.close(fd)


def _exclusive_temp(dir_fd: int, target_name: str) -> tuple[str, int]:
    _safe_leaf(target_name)
    for _ in range(32):
        name = f".{target_name}.{secrets.token_hex(16)}.tmp"
        try:
            fd = os.open(name, CREATE_FILE_FLAGS, 0o600, dir_fd=dir_fd)
            return name, fd
        except FileExistsError:
            continue
    raise FeedLimitError("could not allocate an exclusive state temporary file")


def _replace_temp(
    dir_fd: int,
    temp_name: str,
    target_name: str,
    temp_fd: int,
) -> None:
    descriptor_info = os.fstat(temp_fd)
    name_info = os.stat(temp_name, dir_fd=dir_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(name_info.st_mode)
        or name_info.st_uid != os.getuid()
        or name_info.st_nlink != 1
        or (name_info.st_dev, name_info.st_ino)
        != (descriptor_info.st_dev, descriptor_info.st_ino)
    ):
        raise FeedLimitError("state temporary file changed before replacement")
    os.replace(
        temp_name,
        target_name,
        src_dir_fd=dir_fd,
        dst_dir_fd=dir_fd,
    )
    os.fsync(dir_fd)


def _atomic_write(dir_fd: int, target_name: str, payload: bytes, *, max_bytes: int) -> None:
    if len(payload) > max_bytes:
        raise FeedLimitError(f"state payload exceeds {max_bytes} bytes")
    temp_name, fd = _exclusive_temp(dir_fd, target_name)
    try:
        view = memoryview(payload)
        written = 0
        while written < len(view):
            written += os.write(fd, view[written:])
        os.fsync(fd)
        _replace_temp(dir_fd, temp_name, target_name, fd)
        os.close(fd)
        fd = -1
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temp_name, dir_fd=dir_fd)
        except FileNotFoundError:
            pass


def _cache_payload(
    data: dict,
    archive_hash: str,
    *,
    retry_after: float = 0,
) -> bytes:
    cache_data = {
        "stations": data["stations"],
        "routes": data["routes"],
        "services": {
            key: {
                **value,
                "added": sorted(value["added"]),
                "removed": sorted(value["removed"]),
            }
            for key, value in data["services"].items()
        },
        "trips": data["trips"],
    }
    envelope = {
        "schemaVersion": CACHE_SCHEMA_VERSION,
        "archiveSha256": archive_hash,
        "retryAfter": retry_after,
        "data": cache_data,
    }
    try:
        payload = json.dumps(
            envelope,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise FeedLimitError("could not serialize parsed GTFS cache") from exc
    if len(payload) > MAX_GTFS_CACHE_BYTES:
        raise FeedLimitError(f"parsed GTFS cache exceeds {MAX_GTFS_CACHE_BYTES} bytes")
    return payload


def _load_cache(payload: bytes, archive_hash: str) -> dict:
    try:
        envelope = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise FeedLimitError("invalid GTFS JSON cache") from exc
    if (
        not isinstance(envelope, dict)
        or envelope.get("schemaVersion") != CACHE_SCHEMA_VERSION
        or envelope.get("archiveSha256") != archive_hash
    ):
        raise FeedLimitError("GTFS cache does not match the current archive")
    data = envelope.get("data")
    _validate_gtfs_data(data)
    for service in data["services"].values():
        service["added"] = set(service["added"])
        service["removed"] = set(service["removed"])
    retry_after = envelope.get("retryAfter", 0)
    now = time.time()
    if (
        isinstance(retry_after, bool)
        or not isinstance(retry_after, (int, float))
        or not math.isfinite(retry_after)
        or retry_after < 0
        or retry_after > now + GTFS_MAX_RETRY_MARKER_SECONDS
    ):
        retry_after = 0
    data["_retryAfter"] = float(retry_after)
    return data


def _write_gtfs_cache(
    dir_fd: int,
    data: dict,
    archive_hash: str,
    *,
    retry_after: float = 0,
) -> None:
    _atomic_write(
        dir_fd,
        CACHE_NAME,
        _cache_payload(data, archive_hash, retry_after=retry_after),
        max_bytes=MAX_GTFS_CACHE_BYTES,
    )


def load_gtfs() -> dict:
    with _secure_state_directory() as state_fd:
        archive_record = _read_state_file(
            state_fd,
            ARCHIVE_NAME,
            max_bytes=MAX_GTFS_DOWNLOAD_BYTES,
        )
        archive_age = (
            time.time() - archive_record[1].st_mtime
            if archive_record is not None
            else None
        )
        fresh = (
            archive_age is not None
            and -MAX_CLOCK_SKEW_SECONDS <= archive_age <= GTFS_MAX_AGE_SECONDS
        )
        archive = archive_record[0] if archive_record is not None else None
        archive_hash = hashlib.sha256(archive).hexdigest() if archive is not None else ""
        cached_data = None
        if archive is not None:
            cache_record = _read_state_file(
                state_fd,
                CACHE_NAME,
                max_bytes=MAX_GTFS_CACHE_BYTES,
            )
            if cache_record is not None:
                try:
                    cached_data = _load_cache(cache_record[0], archive_hash)
                except FeedLimitError:
                    pass

        if fresh:
            if cached_data is not None:
                return cached_data
            data = parse_gtfs(archive)
            _write_gtfs_cache(state_fd, data, archive_hash)
            return data

        if (
            cached_data is not None
            and archive_age is not None
            and -MAX_CLOCK_SKEW_SECONDS <= archive_age <= GTFS_MAX_STALE_SECONDS
            and cached_data.get("_retryAfter", 0) > time.time()
        ):
            cached_data["_usingStaleArchive"] = True
            return cached_data

        temp_name, temp_fd = _exclusive_temp(state_fd, ARCHIVE_NAME)
        try:
            try:
                download_to_fd(
                    GTFS_URL,
                    temp_fd,
                    max_bytes=MAX_GTFS_DOWNLOAD_BYTES,
                    timeout=20,
                )
                archive = _read_fd(temp_fd, max_bytes=MAX_GTFS_DOWNLOAD_BYTES)
                data = parse_gtfs(archive)
            except Exception:
                if (
                    archive_record is None
                    or archive_age is None
                    or archive_age < -MAX_CLOCK_SKEW_SECONDS
                    or archive_age > GTFS_MAX_STALE_SECONDS
                ):
                    raise
                data = cached_data if cached_data is not None else parse_gtfs(archive)
                retry_after = time.time() + GTFS_RETRY_BACKOFF_SECONDS
                _write_gtfs_cache(
                    state_fd,
                    data,
                    archive_hash,
                    retry_after=retry_after,
                )
                data["_usingStaleArchive"] = True
                data["_retryAfter"] = retry_after
                return data
            archive_hash = hashlib.sha256(archive).hexdigest()
            _replace_temp(state_fd, temp_name, ARCHIVE_NAME, temp_fd)
            os.close(temp_fd)
            temp_fd = -1
            _write_gtfs_cache(state_fd, data, archive_hash)
            return data
        finally:
            if temp_fd >= 0:
                os.close(temp_fd)
            try:
                os.unlink(temp_name, dir_fd=state_fd)
            except FileNotFoundError:
                pass


def hms_seconds(value: str) -> int:
    parts = (value or "0:0:0").split(":")
    while len(parts) < 3:
        parts.append("0")
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])


def service_active(service: dict, today: date) -> bool:
    stamp = today.strftime("%Y%m%d")
    if stamp in service["removed"]:
        return False
    if stamp in service["added"]:
        return True
    if not (service["start"] <= stamp <= service["end"]):
        return False
    return bool(service["days"].get(WEEKDAYS[today.weekday()]))


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlamb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlamb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def locate_ip():
    try:
        data = json.loads(get_text(
            "https://ipinfo.io/json",
            max_bytes=MAX_LOCATION_BYTES,
            timeout=4,
        ))
        if not isinstance(data, dict):
            return None
        parts = str(data.get("loc") or "").split(",")
        if len(parts) != 2:
            return None
        lat, lon = float(parts[0]), float(parts[1])
        if (
            not math.isfinite(lat)
            or not math.isfinite(lon)
            or not -90 <= lat <= 90
            or not -180 <= lon <= 180
        ):
            return None
        return {
            "ok": True,
            "source": "ip",
            "lat": lat,
            "lon": lon,
            "label": str(data.get("city") or "Approximate location")[
                :MAX_GTFS_NAME_CHARS
            ],
        }
    except (
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
        OSError,
        TypeError,
        ValueError,
    ):
        return None


def station_by_abbr(stations: list, abbr: str):
    code = (abbr or "").upper()
    for row in stations:
        if row["abbr"] == code:
            return row
    return None


def pick_board(origin, dest, stations, location):
    if not location or not location.get("ok"):
        return origin, "outbound", location
    start = station_by_abbr(stations, origin)
    end = station_by_abbr(stations, dest)
    if not start or not end:
        return origin, "outbound", location
    lat, lon = location["lat"], location["lon"]
    d_start = haversine_m(lat, lon, start["lat"], start["lon"])
    d_end = haversine_m(lat, lon, end["lat"], end["lon"])
    location["startMeters"] = round(d_start)
    location["endMeters"] = round(d_end)
    if d_end + 80 < d_start and d_end <= NEAR_METERS:
        location.update(nearest=dest, meters=round(d_end), message=f"Near {end['name']} — inbound")
        return dest, "inbound", location
    if d_start <= NEAR_METERS:
        location.update(nearest=origin, meters=round(d_start), message=f"Near {start['name']} — outbound")
        return origin, "outbound", location
    closer, dist, name = (origin, d_start, start["name"]) if d_start <= d_end else (dest, d_end, end["name"])
    location.update(nearest=closer, meters=round(dist), message=f"Closest stop {name} ({round(dist / 1000, 1)} km)")
    if closer == dest:
        return dest, "inbound", location
    return origin, "outbound", location


def _service_day_offsets(service: dict, today: date) -> list[tuple[date, int]]:
    out = []
    for day_delta in (-1, 0, 1):
        service_day = today + timedelta(days=day_delta)
        if service_active(service, service_day):
            out.append((service_day, day_delta * 24 * 3600))
    return out


def matching_trips(
    gtfs,
    origin,
    dest,
    today: date,
    now: datetime | None = None,
):
    out = []
    now_sec = (
        now.hour * 3600 + now.minute * 60 + now.second
        if now is not None
        else None
    )
    for trip in gtfs["trips"]:
        service = gtfs["services"].get(trip["service"])
        if not service:
            continue
        abbrs = [row[0] for row in trip["stops"]]
        try:
            i = abbrs.index(origin)
            j = abbrs.index(dest)
        except ValueError:
            continue
        if i < j:
            origin_stop = trip["stops"][i]
            offsets = (
                [(today, 0)]
                if now is None and service_active(service, today)
                else _service_day_offsets(service, today)
                if now is not None
                else []
            )
            for _, day_offset in offsets:
                depart_sec = hms_seconds(origin_stop[2]) + day_offset
                if (
                    now_sec is not None
                    and not now_sec - 90 <= depart_sec <= now_sec + 24 * 3600
                ):
                    continue
                if len(out) >= MAX_MATCHES:
                    raise FeedLimitError(
                        f"more than {MAX_MATCHES} trips match this station pair"
                    )
                out.append({
                    "id": trip["id"],
                    "instance": f"{trip['id']}@{day_offset}",
                    "route": trip["route"],
                    "headsign": trip["headsign"],
                    "depart": origin_stop[2],
                    "departSec": depart_sec,
                    "stopId": origin_stop[1],
                    "destAbbr": dest,
                })
    out.sort(key=lambda t: t["departSec"])
    return out


def _leg_record(
    trip: dict,
    origin_stop,
    dest: str,
    *,
    day_offset: int,
    dest_stop,
) -> dict:
    return {
        "id": trip["id"],
        "instance": f"{trip['id']}@{day_offset}",
        "route": trip["route"],
        "headsign": trip["headsign"],
        "depart": origin_stop[2],
        "departSec": hms_seconds(origin_stop[2]) + day_offset,
        "stopId": origin_stop[1],
        "destStopId": dest_stop[1],
        "destAbbr": dest,
    }


def transfer_journeys(
    gtfs,
    origin: str,
    dest: str,
    today: date,
    now: datetime,
) -> list[dict]:
    """Find bounded, scheduled one-transfer journeys for a station pair."""
    now_sec = now.hour * 3600 + now.minute * 60 + now.second
    first_by_hub: dict[str, list] = {}
    second_by_hub: dict[str, list] = {}
    candidate_count = 0

    for trip in gtfs["trips"]:
        service = gtfs["services"].get(trip["service"])
        if not service:
            continue
        stops = trip["stops"]
        abbrs = [stop[0] for stop in stops]

        for _, day_offset in _service_day_offsets(service, today):
            try:
                origin_index = abbrs.index(origin)
            except ValueError:
                origin_index = -1
            if 0 <= origin_index < len(stops) - 1:
                origin_stop = stops[origin_index]
                depart_abs = hms_seconds(origin_stop[2]) + day_offset
                if now_sec - 90 <= depart_abs <= now_sec + TRANSFER_SEARCH_SECONDS:
                    for transfer_stop in stops[origin_index + 1 :]:
                        hub = transfer_stop[0]
                        if hub in (origin, dest):
                            continue
                        transfer_abs = hms_seconds(transfer_stop[2]) + day_offset
                        if transfer_abs < depart_abs:
                            continue
                        first_by_hub.setdefault(hub, []).append({
                            "trip": _leg_record(
                                trip,
                                origin_stop,
                                hub,
                                day_offset=day_offset,
                                dest_stop=transfer_stop,
                            ),
                            "transferAbs": transfer_abs,
                        })
                        candidate_count += 1
                        if candidate_count > MAX_TRANSFER_CANDIDATES:
                            raise FeedLimitError("too many transfer journey candidates")

            try:
                dest_index = len(abbrs) - 1 - abbrs[::-1].index(dest)
            except ValueError:
                dest_index = -1
            if dest_index > 0:
                destination_stop = stops[dest_index]
                for transfer_stop in stops[:dest_index]:
                    hub = transfer_stop[0]
                    if hub in (origin, dest):
                        continue
                    depart_abs = hms_seconds(transfer_stop[2]) + day_offset
                    if not (
                        now_sec - 90
                        <= depart_abs
                        <= now_sec + TRANSFER_SEARCH_SECONDS + MAX_TRANSFER_SECONDS
                    ):
                        continue
                    arrive_abs = hms_seconds(destination_stop[2]) + day_offset
                    if arrive_abs < depart_abs:
                        continue
                    second_by_hub.setdefault(hub, []).append({
                        "trip": _leg_record(
                            trip,
                            transfer_stop,
                            dest,
                            day_offset=day_offset,
                            dest_stop=destination_stop,
                        ),
                        "departAbs": depart_abs,
                        "arriveAbs": arrive_abs,
                        "arrive": destination_stop[2],
                    })
                    candidate_count += 1
                    if candidate_count > MAX_TRANSFER_CANDIDATES:
                        raise FeedLimitError("too many transfer journey candidates")

    for hub, candidates in first_by_hub.items():
        unique = {}
        for item in candidates:
            trip = item["trip"]
            key = (
                trip["instance"],
                trip["stopId"],
                trip["destStopId"],
                item["transferAbs"],
            )
            unique.setdefault(key, item)
        first_by_hub[hub] = list(unique.values())
    for hub, candidates in second_by_hub.items():
        unique = {}
        for item in candidates:
            trip = item["trip"]
            key = (
                trip["instance"],
                trip["stopId"],
                trip["destStopId"],
                item["departAbs"],
                item["arriveAbs"],
            )
            unique.setdefault(key, item)
        second_by_hub[hub] = sorted(
            unique.values(),
            key=lambda item: item["departAbs"],
        )

    pairings = {}
    pairing_attempts = 0
    for hub in sorted(first_by_hub.keys() & second_by_hub.keys()):
        second_legs = second_by_hub[hub]
        departure_times = [item["departAbs"] for item in second_legs]
        for first in first_by_hub[hub]:
            minimum = first["transferAbs"] + MIN_TRANSFER_SECONDS
            index = bisect_left(departure_times, minimum)
            end = bisect_right(
                departure_times,
                first["transferAbs"] + MAX_TRANSFER_SECONDS,
            )
            for second in second_legs[index:end]:
                pairing_attempts += 1
                if pairing_attempts > MAX_TRANSFER_PAIRING_ATTEMPTS:
                    raise FeedLimitError("too many transfer pairing attempts")
                if first["trip"]["instance"] == second["trip"]["instance"]:
                    continue
                journey = {
                    "hub": hub,
                    "departAbs": first["trip"]["departSec"],
                    "transferAbs": first["transferAbs"],
                    "secondDepartAbs": second["departAbs"],
                    "arriveAbs": second["arriveAbs"],
                    "arrive": second["arrive"],
                    "first": first["trip"],
                    "second": second["trip"],
                }
                pair_key = (
                    first["trip"]["instance"],
                    second["trip"]["instance"],
                )
                previous = pairings.get(pair_key)
                if (
                    previous is not None
                    and (
                        previous["arriveAbs"],
                        previous["transferAbs"],
                    )
                    <= (
                        journey["arriveAbs"],
                        journey["transferAbs"],
                    )
                ):
                    continue
                pairings[pair_key] = journey
                if len(pairings) > MAX_TRANSFER_PAIRINGS:
                    raise FeedLimitError("too many feasible transfer journey pairings")

    journeys = list(pairings.values())
    journeys.sort(key=lambda item: (item["departAbs"], item["arriveAbs"]))
    return journeys


def rank_transfer_journeys(journeys, rt, now: datetime):
    """Apply realtime timing, reject missed transfers, and pick one per first train."""
    now_sec = now.hour * 3600 + now.minute * 60 + now.second
    unix_now = int(now.timestamp())
    best_for_first_leg = {}
    for journey in journeys:
        first = journey["first"]
        second = journey["second"]
        first_rt = rt.get(first["id"]) or {}
        second_rt = rt.get(second["id"]) or {}
        first_stops = first_rt.get("stops") or {}
        second_stops = second_rt.get("stops") or {}

        first_depart = first_stops.get(first["stopId"])
        if not first_depart:
            first_depart = unix_now + journey["departAbs"] - now_sec
        departure_delta = first_depart - unix_now
        if departure_delta < -45 or departure_delta > 3 * 3600:
            continue
        first_arrive = first_stops.get(first["destStopId"])
        if not first_arrive:
            first_arrive = first_depart + journey["transferAbs"] - journey["departAbs"]
        second_depart = second_stops.get(second["stopId"])
        if not second_depart:
            second_depart = unix_now + journey["secondDepartAbs"] - now_sec
        wait_seconds = second_depart - first_arrive
        if not MIN_TRANSFER_SECONDS <= wait_seconds <= MAX_TRANSFER_SECONDS:
            continue
        second_arrive = second_stops.get(second["destStopId"])
        if not second_arrive:
            second_arrive = (
                second_depart + journey["arriveAbs"] - journey["secondDepartAbs"]
            )

        ranked = {
            **journey,
            "realtimeDepart": first_depart,
            "realtimeArrive": second_arrive,
            "realtimeWaitSeconds": wait_seconds,
        }
        first_key = first["instance"]
        previous = best_for_first_leg.get(first_key)
        if previous is None or (
            ranked["realtimeArrive"],
            ranked["secondDepartAbs"],
        ) < (
            previous["realtimeArrive"],
            previous["secondDepartAbs"],
        ):
            best_for_first_leg[first_key] = ranked

    out = list(best_for_first_leg.values())
    out.sort(
        key=lambda item: (
            item["realtimeDepart"],
            item["realtimeArrive"],
        )
    )
    return out[:MAX_TRANSFER_JOURNEYS]


def transfer_lines(journeys, routes, stations):
    seen = set()
    lines = []
    for journey in journeys[:1]:
        station = station_by_abbr(stations, journey["hub"])
        transfer_name = station["name"] if station else journey["hub"]
        for leg_name in ("first", "second"):
            trip = journey[leg_name]
            key = (trip["route"], trip["headsign"])
            if key in seen:
                continue
            if len(lines) >= MAX_LINES:
                return lines
            seen.add(key)
            info = routes.get(trip["route"], {})
            lines.append({
                "id": trip["route"],
                "name": (info.get("name") or trip["route"]).split("-")[0],
                "hexcolor": info.get("hexcolor") or "#888888",
                "head": trip["headsign"],
                "platform": "",
                "transfer": (
                    f"transfer at {transfer_name}" if leg_name == "first" else ""
                ),
            })
    return lines


def scheduled_journeys(journeys, routes, stations, limit: int = 3):
    out = []
    for journey in journeys[:limit]:
        first = journey["first"]
        second = journey["second"]
        hub = station_by_abbr(stations, journey["hub"])
        depart_hh, depart_mm = first["depart"].split(":")[:2]
        arrive_hh, arrive_mm = journey["arrive"].split(":")[:2]
        duration = max(0, (journey["arriveAbs"] - journey["departAbs"]) // 60)
        out.append({
            "depart": f"{int(depart_hh) % 24:02d}:{depart_mm}",
            "arrive": f"{int(arrive_hh) % 24:02d}:{arrive_mm}",
            "minutes": duration,
            "fare": "",
            "legs": [
                {
                    "route": routes.get(first["route"], {}).get("name")
                    or first["route"],
                    "head": first["headsign"],
                    "to": hub["name"] if hub else journey["hub"],
                },
                {
                    "route": routes.get(second["route"], {}).get("name")
                    or second["route"],
                    "head": second["headsign"],
                    "to": second["destAbbr"],
                },
            ],
        })
    return out


def unique_lines(matches, routes):
    seen = set()
    lines = []
    for trip in matches:
        key = (trip["route"], trip["headsign"])
        if key in seen:
            continue
        if len(lines) >= MAX_LINES:
            break
        seen.add(key)
        info = routes.get(trip["route"], {})
        short = (info.get("name") or trip["route"]).split("-")[0]
        lines.append({
            "id": trip["route"],
            "name": short,
            "hexcolor": info.get("hexcolor") or "#888888",
            "head": trip["headsign"],
            "platform": "",
            "transfer": "",
        })
    return lines


def minute_label(minutes: int, leaving: bool) -> str:
    if leaving:
        return "Leaving"
    return f"{minutes} min"


def upcoming(matches, routes, rt, now: datetime, limit: int = 8):
    now_sec = now.hour * 3600 + now.minute * 60 + now.second
    unix_now = int(now.timestamp())
    rows = {}
    used = []
    for trip in matches:
        rt_trip = rt.get(trip["id"]) or {}
        unix = (rt_trip.get("stops") or {}).get(trip["stopId"])
        if unix:
            delta = unix - unix_now
        else:
            delta = trip["departSec"] - now_sec
        if delta < -45 or delta > 3 * 3600:
            continue
        used.append(trip)
        minutes = max(0, int(round(delta / 60)))
        leaving = minutes <= 0
        info = routes.get(trip["route"], {})
        key = (trip["headsign"], trip["route"])
        if key not in rows and len(rows) >= MAX_DEPARTURE_GROUPS:
            continue
        rec = rows.setdefault(key, {
            "dest": trip["headsign"],
            "destAbbr": trip["destAbbr"],
            "hexcolor": info.get("hexcolor") or "#888888",
            "platform": "",
            "estimates": [],
        })
        rec["estimates"].append({
            "minutes": "Leaving" if leaving else str(minutes),
            "minuteLabel": minute_label(minutes, leaving),
            "cars": "",
            "platform": "",
            "depart": trip["depart"],
        })
    departures = []
    for rec in rows.values():
        rec["estimates"] = rec["estimates"][:3]
        if rec["estimates"]:
            departures.append(rec)
    departures.sort(key=lambda r: int(r["estimates"][0]["minutes"]) if r["estimates"][0]["minutes"].isdigit() else 0)
    return departures[:limit], used


def soonest_of(departures):
    soonest = None
    best = 10**9
    for row in departures:
        for est in row.get("estimates") or []:
            raw = str(est["minutes"])
            val = 0 if raw == "Leaving" else int(raw) if raw.isdigit() else 999
            if val < best:
                best = val
                soonest = {
                    "minutes": est["minutes"],
                    "dest": row["dest"],
                    "destAbbr": row["destAbbr"],
                    "hexcolor": row["hexcolor"],
                    "platform": est.get("platform") or "",
                }
    return soonest


def scheduled_trips(matches, limit: int = 3):
    now = datetime.now(TZ)
    now_sec = now.hour * 3600 + now.minute * 60 + now.second
    future = [t for t in matches if t["departSec"] >= now_sec - 60]
    if not future:
        future = matches
    out = []
    for trip in future[:limit]:
        hh, mm = (trip["depart"] or "0:00:00").split(":")[:2]
        out.append({
            "depart": f"{int(hh) % 24:02d}:{mm}",
            "arrive": "",
            "minutes": 0,
            "fare": "",
            "legs": [],
        })
    return out


def _error_text(exc) -> str:
    text = " ".join(str(exc).split())
    if len(text) > MAX_ERROR_CHARS:
        return text[: MAX_ERROR_CHARS - 1] + "…"
    return text


def emit_output(out: dict) -> bool:
    """Write exactly one bounded JSON document for the QML consumer."""
    try:
        payload = json.dumps(
            out,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        payload = b""
    within_limit = bool(payload) and len(payload) + 1 <= MAX_OUTPUT_BYTES
    if not within_limit:
        fallback = {
            "ok": False,
            "origin": str(out.get("origin") or "")[:MAX_GTFS_ID_CHARS],
            "dest": str(out.get("dest") or "")[:MAX_GTFS_ID_CHARS],
            "lines": [],
            "trips": [],
            "departures": [],
            "soonest": None,
            "advisories": [],
            "traincount": 0,
            "error": "Transit response exceeded the local output safety limit",
        }
        payload = json.dumps(fallback, separators=(",", ":")).encode("utf-8")
        if len(payload) + 1 > MAX_OUTPUT_BYTES:
            payload = b'{"ok":false}'
        if len(payload) + 1 > MAX_OUTPUT_BYTES:
            payload = b""
    sys.stdout.buffer.write(payload)
    if MAX_OUTPUT_BYTES >= 1:
        sys.stdout.buffer.write(b"\n")
    return within_limit


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("origin", nargs="?", default="12TH")
    parser.add_argument("dest", nargs="?", default="EMBR")
    parser.add_argument("--locate", action="store_true")
    args = parser.parse_args()
    origin = (args.origin or "").strip().upper()
    dest = (args.dest or "").strip().upper()
    invalid_identifiers = (
        len(origin) > MAX_GTFS_ID_CHARS or len(dest) > MAX_GTFS_ID_CHARS
    )
    origin = origin[:MAX_GTFS_ID_CHARS]
    dest = dest[:MAX_GTFS_ID_CHARS]
    if origin in ("", "-", "NONE"):
        origin = ""
    if dest in ("", "-", "NONE"):
        dest = ""
    now = datetime.now(TZ)
    today = now.date()

    out = {
        "ok": False,
        "origin": origin,
        "dest": dest,
        "originName": origin,
        "destName": dest,
        "direction": "outbound",
        "boardStation": origin,
        "boardName": origin,
        "headingName": dest,
        "locationEnabled": bool(args.locate),
        "location": {"ok": False, "source": "", "message": "Location off"},
        "lines": [],
        "trips": [],
        "departures": [],
        "soonest": None,
        "transfer": None,
        "advisories": [],
        "traincount": 0,
        "updated": now.strftime("%-I:%M %p"),
        "error": "",
    }

    if invalid_identifiers:
        out["error"] = (
            f"Station identifiers are limited to {MAX_GTFS_ID_CHARS} characters"
        )
        emit_output(out)
        return 1

    errors = []
    try:
        gtfs = load_gtfs()
    except Exception as exc:
        out["error"] = f"gtfs: {_error_text(exc)}"
        emit_output(out)
        return 1
    if gtfs.get("_usingStaleArchive"):
        errors.append("Using a cached schedule while the provider is unavailable")

    start = station_by_abbr(gtfs["stations"], origin) if origin else None
    end = station_by_abbr(gtfs["stations"], dest) if dest else None
    out["originName"] = start["name"] if start else origin
    out["destName"] = end["name"] if end else dest

    if origin and dest and origin == dest:
        out["error"] = "Pick two different stations"
        emit_output(out)
        return 1

    if not origin or not dest:
        advisories = []
        traincount = 0
        try:
            advisories = parse_alerts(get_bytes(
                RT_ALERTS_URL,
                max_bytes=MAX_ALERT_BYTES,
                timeout=8,
            ))
        except Exception as exc:
            errors.append(f"alerts: {_error_text(exc)}")
        if not advisories:
            try:
                advisories = parse_rss(get_text(
                    RSS_URL,
                    max_bytes=MAX_RSS_BYTES,
                    timeout=8,
                ))
            except Exception as exc:
                errors.append(f"rss: {_error_text(exc)}")
        try:
            traincount = len(parse_trip_updates(get_bytes(
                RT_TRIPS_URL,
                max_bytes=MAX_RT_BYTES,
                timeout=8,
            )))
        except Exception:
            pass
        out.update({
            "ok": True,
            "advisories": advisories[:MAX_ALERTS],
            "traincount": traincount,
            "error": _error_text("; ".join(errors)),
        })
        return 0 if emit_output(out) else 1

    location = locate_ip() if args.locate else None
    if args.locate and not location:
        out["location"] = {"ok": False, "source": "", "message": "Location unavailable"}
    if location:
        out["location"] = location

    board, direction, location = pick_board(origin, dest, gtfs["stations"], location)
    heading = dest if direction == "outbound" else origin
    board_st = station_by_abbr(gtfs["stations"], board)
    head_st = station_by_abbr(gtfs["stations"], heading)
    out.update({
        "direction": direction,
        "boardStation": board,
        "boardName": board_st["name"] if board_st else board,
        "headingName": head_st["name"] if head_st else heading,
    })
    if location:
        out["location"] = location

    rt = {}
    advisories = []
    try:
        rt = parse_trip_updates(get_bytes(
            RT_TRIPS_URL,
            max_bytes=MAX_RT_BYTES,
            timeout=8,
        ))
    except Exception as exc:
        errors.append(f"gtfsrt: {_error_text(exc)}")
    try:
        advisories = parse_alerts(get_bytes(
            RT_ALERTS_URL,
            max_bytes=MAX_ALERT_BYTES,
            timeout=8,
        ))
    except Exception as exc:
        errors.append(f"alerts: {_error_text(exc)}")
    if not advisories:
        try:
            advisories = parse_rss(get_text(
                RSS_URL,
                max_bytes=MAX_RSS_BYTES,
                timeout=8,
            ))
        except Exception as exc:
            errors.append(f"rss: {_error_text(exc)}")

    try:
        matches = matching_trips(gtfs, board, heading, today, now)
        journeys = []
        if not matches:
            journeys = rank_transfer_journeys(
                transfer_journeys(gtfs, board, heading, today, now),
                rt,
                now,
            )
    except FeedLimitError as exc:
        out["error"] = _error_text(exc)
        emit_output(out)
        return 1

    display_matches = matches
    if journeys:
        display_matches = []
        seen_first_legs = set()
        for journey in journeys:
            first = journey["first"]
            key = (first["id"], first["stopId"])
            if key not in seen_first_legs:
                seen_first_legs.add(key)
                display_matches.append(first)

    departures, soon = upcoming(display_matches, gtfs["routes"], rt, now)
    lines = (
        transfer_lines(journeys, gtfs["routes"], gtfs["stations"])
        if journeys
        else unique_lines(soon or matches, gtfs["routes"])
    )
    out.update({
        "ok": bool(matches or journeys),
        "lines": lines,
        "trips": (
            scheduled_journeys(
                journeys,
                gtfs["routes"],
                gtfs["stations"],
            )
            if journeys
            else scheduled_trips(soon or matches)
        ),
        "departures": departures,
        "soonest": soonest_of(departures),
        "transfer": (
            {
                "station": journeys[0]["hub"],
                "stationName": (
                    station_by_abbr(gtfs["stations"], journeys[0]["hub"]) or {}
                ).get("name", journeys[0]["hub"]),
                "waitMinutes": max(
                    0,
                    journeys[0].get(
                        "realtimeWaitSeconds",
                        (
                            journeys[0]["secondDepartAbs"]
                            - journeys[0]["transferAbs"]
                        ),
                    )
                    // 60,
                ),
            }
            if journeys
            else None
        ),
        "advisories": advisories[:MAX_ALERTS],
        "traincount": len(rt),
        "error": _error_text("; ".join(errors)),
    })
    emitted = emit_output(out)
    return 0 if out["ok"] and emitted else 1


if __name__ == "__main__":
    raise SystemExit(main())
