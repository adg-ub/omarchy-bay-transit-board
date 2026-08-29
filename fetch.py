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
import io
import json
import math
import os
import pickle
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

UA = "bay-transit-board/1.2 (+https://github.com/adg-ub/omarchy-bay-transit-board)"
GTFS_URL = "https://www.bart.gov/dev/schedules/google_transit.zip"
RT_TRIPS_URL = "https://api.bart.gov/gtfsrt/tripupdate.aspx"
RT_ALERTS_URL = "https://api.bart.gov/gtfsrt/alerts.aspx"
RSS_URL = "https://www.bart.gov/schedules/advisories/advisories.xml"
TZ = ZoneInfo("America/Los_Angeles")
NEAR_METERS = 1500
STATE = Path.home() / ".local/state/omarchy/bay-transit-board"
HERE = Path(__file__).resolve().parent
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def get_bytes(url: str, timeout: int = 12) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def get_text(url: str, timeout: int = 12) -> str:
    return get_bytes(url, timeout).decode("utf-8", "replace")


# ---- protobuf (stdlib) -------------------------------------------------------

def _varint(buf: bytes, i: int):
    result = 0
    shift = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            return result, i
        shift += 7
    return result, i


def _decode(buf: bytes) -> dict[int, list]:
    i = 0
    fields: dict[int, list] = {}
    n = len(buf)
    while i < n:
        key, i = _varint(buf, i)
        field, wtype = key >> 3, key & 7
        if wtype == 0:
            val, i = _varint(buf, i)
        elif wtype == 1:
            val = int.from_bytes(buf[i : i + 8], "little")
            i += 8
        elif wtype == 2:
            ln, i = _varint(buf, i)
            val = buf[i : i + ln]
            i += ln
        elif wtype == 5:
            val = int.from_bytes(buf[i : i + 4], "little")
            i += 4
        else:
            break
        fields.setdefault(field, []).append(val)
    return fields


def _str(fields: dict, n: int) -> str:
    vals = fields.get(n) or []
    if not vals:
        return ""
    v = vals[0]
    return v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else str(v)


def _int(fields: dict, n: int, default: int = 0) -> int:
    vals = fields.get(n) or []
    return int(vals[0]) if vals else default


def _msg(fields: dict, n: int) -> dict:
    vals = fields.get(n) or []
    if not vals:
        return {}
    v = vals[0]
    return _decode(v) if isinstance(v, (bytes, bytearray)) else {}


def _msgs(fields: dict, n: int) -> list[dict]:
    out = []
    for v in fields.get(n) or []:
        if isinstance(v, (bytes, bytearray)):
            out.append(_decode(v))
    return out


def parse_trip_updates(buf: bytes) -> dict[str, dict]:
    """trip_id -> {route_id, stop_times: {stop_id: unix_departure}}"""
    root = _decode(buf)
    out: dict[str, dict] = {}
    for entity in _msgs(root, 2):
        tu = _msg(entity, 3)
        if not tu:
            continue
        trip = _msg(tu, 1)
        trip_id = _str(trip, 1)
        if not trip_id:
            continue
        stops = {}
        # Spec puts StopTimeUpdate at field 3; BART's feed emits them at field 2.
        for stu in _msgs(tu, 2) or _msgs(tu, 3):
            stop_id = _str(stu, 4)
            dep = _msg(stu, 3) or _msg(stu, 2)
            unix = _int(dep, 2)
            if stop_id and unix:
                stops[stop_id] = unix
        if trip_id:
            out[trip_id] = {"route_id": _str(trip, 5), "stops": stops}
    return out


def _translated(fields: dict, n: int) -> str:
    ts = _msg(fields, n)
    for tr in _msgs(ts, 1):
        text = _str(tr, 1).strip()
        if text:
            return text
    return ""


def parse_alerts(buf: bytes) -> list[str]:
    root = _decode(buf)
    out = []
    for entity in _msgs(root, 2):
        alert = _msg(entity, 5)
        if not alert:
            header = _str(entity, 1)
            # some feeds put BSA id on entity; still try alert field
            if not alert:
                continue
        text = _translated(alert, 11) or _translated(alert, 10)
        if text and "no delays" not in text.lower():
            out.append(text)
    return out


def parse_rss(xml_text: str) -> list[str]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        desc = (item.findtext("description") or "").strip()
        text = desc or title
        if text and "no delays" not in text.lower() and "has been issued" not in text.lower():
            out.append(text)
        elif desc and "no delays" not in desc.lower():
            out.append(desc)
    return out[:4]


# ---- GTFS cache ---------------------------------------------------------------

def _csv_rows(zf: zipfile.ZipFile, name: str) -> list[dict]:
    with zf.open(name) as fh:
        text = io.TextIOWrapper(fh, encoding="utf-8-sig", newline="")
        return list(csv.DictReader(text))


def parse_gtfs(zip_path: Path) -> dict:
    with zipfile.ZipFile(zip_path) as zf:
        routes_raw = _csv_rows(zf, "routes.txt")
        stops_raw = _csv_rows(zf, "stops.txt")
        trips_raw = _csv_rows(zf, "trips.txt")
        times_raw = _csv_rows(zf, "stop_times.txt")
        cal_raw = _csv_rows(zf, "calendar.txt")
        dates_raw = _csv_rows(zf, "calendar_dates.txt") if "calendar_dates.txt" in zf.namelist() else []

    parent_of = {}
    stations = []
    for stop in stops_raw:
        sid = stop["stop_id"]
        parent = stop.get("parent_station") or ""
        if stop.get("location_type") == "1":
            stations.append({
                "abbr": sid,
                "name": stop["stop_name"],
                "lat": float(stop["stop_lat"]),
                "lon": float(stop["stop_lon"]),
            })
            parent_of[sid] = sid
        elif parent:
            parent_of[sid] = parent
        else:
            parent_of[sid] = sid

    routes = {}
    for route in routes_raw:
        rid = route["route_id"]
        color = (route.get("route_color") or "888888").upper()
        routes[rid] = {
            "id": rid,
            "name": route.get("route_short_name") or route.get("route_long_name") or rid,
            "fullName": route.get("route_long_name") or "",
            "hexcolor": "#" + color if not color.startswith("#") else color,
        }

    services = {}
    for row in cal_raw:
        services[row["service_id"]] = {
            "days": {day: row.get(day) == "1" for day in WEEKDAYS},
            "start": row["start_date"],
            "end": row["end_date"],
            "added": set(),
            "removed": set(),
        }
    for row in dates_raw:
        sid = row["service_id"]
        rec = services.setdefault(sid, {"days": {d: False for d in WEEKDAYS}, "start": "00000000", "end": "99999999", "added": set(), "removed": set()})
        if row.get("exception_type") == "1":
            rec["added"].add(row["date"])
        else:
            rec["removed"].add(row["date"])

    times_by_trip: dict[str, list] = {}
    for row in times_raw:
        times_by_trip.setdefault(row["trip_id"], []).append((
            int(row["stop_sequence"] or 0),
            parent_of.get(row["stop_id"], row["stop_id"]),
            row["stop_id"],
            row.get("departure_time") or row.get("arrival_time") or "",
        ))
    for trip_id, rows in times_by_trip.items():
        rows.sort()
        times_by_trip[trip_id] = [(abbr, stop_id, dep) for _, abbr, stop_id, dep in rows]

    trips = []
    for trip in trips_raw:
        stops = times_by_trip.get(trip["trip_id"]) or []
        if not stops:
            continue
        trips.append({
            "id": trip["trip_id"],
            "route": trip["route_id"],
            "service": trip["service_id"],
            "headsign": trip.get("trip_headsign") or "",
            "stops": stops,
        })

    return {"stations": stations, "routes": routes, "services": services, "trips": trips}


def load_gtfs() -> dict:
    STATE.mkdir(parents=True, exist_ok=True)
    zip_path = STATE / "google_transit.zip"
    cache_path = STATE / "gtfs.pkl"
    need = True
    if zip_path.exists() and cache_path.exists():
        age = time.time() - zip_path.stat().st_mtime
        need = age > 12 * 3600
    if need:
        zip_path.write_bytes(get_bytes(GTFS_URL, timeout=20))
        data = parse_gtfs(zip_path)
        cache_path.write_bytes(pickle.dumps(data, protocol=4))
        return data
    return pickle.loads(cache_path.read_bytes())


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
        data = json.loads(get_text("https://ipinfo.io/json", timeout=4))
        parts = str(data.get("loc") or "").split(",")
        if len(parts) != 2:
            return None
        return {
            "ok": True,
            "source": "ip",
            "lat": float(parts[0]),
            "lon": float(parts[1]),
            "label": data.get("city") or "Approximate location",
        }
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
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


def matching_trips(gtfs, origin, dest, today: date):
    out = []
    for trip in gtfs["trips"]:
        service = gtfs["services"].get(trip["service"])
        if not service or not service_active(service, today):
            continue
        abbrs = [row[0] for row in trip["stops"]]
        try:
            i = abbrs.index(origin)
            j = abbrs.index(dest)
        except ValueError:
            continue
        if i < j:
            origin_stop = trip["stops"][i]
            out.append({
                "id": trip["id"],
                "route": trip["route"],
                "headsign": trip["headsign"],
                "depart": origin_stop[2],
                "departSec": hms_seconds(origin_stop[2]),
                "stopId": origin_stop[1],
                "destAbbr": dest,
            })
    out.sort(key=lambda t: t["departSec"])
    return out


def unique_lines(matches, routes):
    seen = []
    lines = []
    for trip in matches:
        key = (trip["route"], trip["headsign"])
        if key in seen:
            continue
        seen.append(key)
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
            if delta < -90:
                delta += 24 * 3600
        if delta < -45 or delta > 3 * 3600:
            continue
        used.append(trip)
        minutes = max(0, int(round(delta / 60)))
        leaving = minutes <= 0
        info = routes.get(trip["route"], {})
        key = (trip["headsign"], trip["route"])
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("origin", nargs="?", default="12TH")
    parser.add_argument("dest", nargs="?", default="EMBR")
    parser.add_argument("--locate", action="store_true")
    args = parser.parse_args()
    origin = (args.origin or "").strip().upper()
    dest = (args.dest or "").strip().upper()
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
        "advisories": [],
        "traincount": 0,
        "updated": now.strftime("%-I:%M %p"),
        "error": "",
    }

    errors = []
    try:
        gtfs = load_gtfs()
    except Exception as exc:
        out["error"] = f"gtfs: {exc}"
        print(json.dumps(out, separators=(",", ":")))
        return 1

    start = station_by_abbr(gtfs["stations"], origin) if origin else None
    end = station_by_abbr(gtfs["stations"], dest) if dest else None
    out["originName"] = start["name"] if start else origin
    out["destName"] = end["name"] if end else dest

    if origin and dest and origin == dest:
        out["error"] = "Pick two different stations"
        print(json.dumps(out, separators=(",", ":")))
        return 1

    if not origin or not dest:
        advisories = []
        traincount = 0
        try:
            advisories = parse_alerts(get_bytes(RT_ALERTS_URL, timeout=8))
        except Exception as exc:
            errors.append(f"alerts: {exc}")
        if not advisories:
            try:
                advisories = parse_rss(get_text(RSS_URL, timeout=8))
            except Exception as exc:
                errors.append(f"rss: {exc}")
        try:
            traincount = len(parse_trip_updates(get_bytes(RT_TRIPS_URL, timeout=8)))
        except Exception:
            pass
        out.update({
            "ok": True,
            "advisories": advisories,
            "traincount": traincount,
            "error": "; ".join(errors),
        })
        print(json.dumps(out, separators=(",", ":")))
        return 0

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
        rt = parse_trip_updates(get_bytes(RT_TRIPS_URL, timeout=8))
    except Exception as exc:
        errors.append(f"gtfsrt: {exc}")
    try:
        advisories = parse_alerts(get_bytes(RT_ALERTS_URL, timeout=8))
    except Exception as exc:
        errors.append(f"alerts: {exc}")
    if not advisories:
        try:
            advisories = parse_rss(get_text(RSS_URL, timeout=8))
        except Exception as exc:
            errors.append(f"rss: {exc}")

    matches = matching_trips(gtfs, board, heading, today)
    departures, soon = upcoming(matches, gtfs["routes"], rt, now)
    lines = unique_lines(soon or matches, gtfs["routes"])
    out.update({
        "ok": bool(matches),
        "lines": lines,
        "trips": scheduled_trips(soon or matches),
        "departures": departures,
        "soonest": soonest_of(departures),
        "advisories": advisories,
        "traincount": len(rt),
        "error": "; ".join(errors),
    })
    print(json.dumps(out, separators=(",", ":")))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
