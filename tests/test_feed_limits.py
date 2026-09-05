import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from email.message import Message
from pathlib import Path
from unittest import mock

import fetch


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, content_length=None):
        super().__init__(body)
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class SlowResponse(FakeResponse):
    def read(self, size=-1):
        time.sleep(0.05)
        return super().read(size)


def write_minimal_gtfs(path: Path) -> None:
    files = {
        "routes.txt": (
            "route_id,route_short_name,route_long_name,route_color\n"
            "R1,Red,Red line,FF0000\n"
        ),
        "stops.txt": (
            "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\n"
            "A,Alpha,37.0,-122.0,1,\n"
            "B,Beta,37.1,-122.1,1,\n"
        ),
        "trips.txt": (
            "route_id,service_id,trip_id,trip_headsign\n"
            "R1,S1,T1,Beta\n"
        ),
        "stop_times.txt": (
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "T1,08:00:00,08:00:00,A,1\n"
            "T1,08:10:00,08:10:00,B,2\n"
        ),
        "calendar.txt": (
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
            "S1,1,1,1,1,1,1,1,20260101,20261231\n"
        ),
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, body in files.items():
            zf.writestr(name, body)


class NetworkLimitTests(unittest.TestCase):
    def test_get_bytes_rejects_declared_oversize_response(self):
        response = FakeResponse(b"", content_length=5)
        with mock.patch.object(fetch.URL_OPENER, "open", return_value=response):
            with self.assertRaises(fetch.FeedLimitError):
                fetch.get_bytes("https://example.test", max_bytes=4)

    def test_get_bytes_rejects_streamed_oversize_response(self):
        response = FakeResponse(b"abcde")
        with mock.patch.object(fetch.URL_OPENER, "open", return_value=response):
            with self.assertRaises(fetch.FeedLimitError):
                fetch.get_bytes("https://example.test", max_bytes=4)

    def test_download_fd_rejects_streamed_oversize_response(self):
        with tempfile.TemporaryFile() as target:
            response = FakeResponse(b"abcde")
            with mock.patch.object(fetch.URL_OPENER, "open", return_value=response):
                with self.assertRaises(fetch.FeedLimitError):
                    fetch.download_to_fd(
                        "https://example.test",
                        target.fileno(),
                        max_bytes=4,
                    )

    def test_cross_origin_redirect_is_refused(self):
        handler = fetch.SameOriginRedirectHandler()
        request = urllib.request.Request("https://example.test/feed")
        with self.assertRaises(urllib.error.HTTPError) as raised:
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                Message(),
                "https://attacker.test/feed",
            )
        raised.exception.close()

    def test_same_origin_redirect_is_allowed(self):
        handler = fetch.SameOriginRedirectHandler()
        request = urllib.request.Request("https://example.test/feed")
        redirected = handler.redirect_request(
            request,
            None,
            302,
            "Found",
            Message(),
            "https://example.test/new-feed",
        )
        self.assertEqual(redirected.full_url, "https://example.test/new-feed")

    def test_relative_same_origin_redirect_is_allowed(self):
        handler = fetch.SameOriginRedirectHandler()
        request = urllib.request.Request("https://example.test/path/feed")
        redirected = handler.redirect_request(
            request,
            None,
            302,
            "Found",
            Message(),
            "../new-feed",
        )
        self.assertEqual(redirected.full_url, "https://example.test/new-feed")

    def test_https_downgrade_redirect_is_refused(self):
        handler = fetch.SameOriginRedirectHandler()
        request = urllib.request.Request("https://example.test/feed")
        with self.assertRaises(ValueError):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                Message(),
                "http://example.test/feed",
            )

    def test_redirect_to_different_port_is_refused(self):
        handler = fetch.SameOriginRedirectHandler()
        request = urllib.request.Request("https://example.test/feed")
        with self.assertRaises(urllib.error.HTTPError) as raised:
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                Message(),
                "https://example.test:444/feed",
            )
        raised.exception.close()

    def test_redirect_count_is_limited(self):
        handler = fetch.SameOriginRedirectHandler()
        request = urllib.request.Request("https://example.test/feed")
        request.redirect_dict = {
            "https://example.test/one": 1,
            "https://example.test/two": 1,
            "https://example.test/three": 1,
        }
        headers = Message()
        headers["Location"] = "/four"
        response = FakeResponse(b"")
        with self.assertRaises(urllib.error.HTTPError) as raised:
            handler.http_error_302(
                request,
                response,
                302,
                "Found",
                headers,
            )
        raised.exception.close()

    def test_whole_request_deadline_interrupts_work(self):
        with self.assertRaises(fetch.RequestDeadlineError):
            with fetch._request_deadline(0.01):
                time.sleep(0.05)

    def test_whole_request_deadline_interrupts_response_read(self):
        response = SlowResponse(b"payload")
        with mock.patch.object(fetch.URL_OPENER, "open", return_value=response):
            with self.assertRaises(fetch.RequestDeadlineError):
                fetch.get_bytes(
                    "https://example.test/feed",
                    max_bytes=100,
                    timeout=0.01,
                )

    def test_whole_request_deadline_interrupts_opener(self):
        def slow_open(*_args, **_kwargs):
            time.sleep(0.05)
            return FakeResponse(b"payload")

        with mock.patch.object(fetch.URL_OPENER, "open", side_effect=slow_open):
            with self.assertRaises(fetch.RequestDeadlineError):
                fetch.get_bytes(
                    "https://example.test/feed",
                    max_bytes=100,
                    timeout=0.01,
                )


class GtfsLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "gtfs.zip"
        write_minimal_gtfs(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_valid_bounded_archive_parses(self):
        data = fetch.parse_gtfs(self.path)
        self.assertEqual(len(data["stations"]), 2)
        self.assertEqual(len(data["trips"]), 1)

    def test_archive_entry_limit_is_enforced(self):
        with mock.patch.object(fetch, "MAX_GTFS_ARCHIVE_ENTRIES", 1):
            with self.assertRaises(fetch.FeedLimitError):
                fetch.parse_gtfs(self.path)

    def test_archive_expanded_size_limit_is_enforced(self):
        with mock.patch.object(fetch, "MAX_GTFS_EXPANDED_BYTES", 1):
            with self.assertRaises(fetch.FeedLimitError):
                fetch.parse_gtfs(self.path)

    def test_csv_row_limit_is_enforced(self):
        with mock.patch.dict(fetch.CSV_LIMITS, {"routes.txt": (fetch.MIB, 0)}):
            with self.assertRaises(fetch.FeedLimitError):
                fetch.parse_gtfs(self.path)

    def test_cache_is_bounded_non_executable_json_tied_to_archive(self):
        data = fetch.parse_gtfs(self.path)
        archive_hash = "a" * 64
        payload = fetch._cache_payload(data, archive_hash)
        envelope = json.loads(payload)
        self.assertEqual(envelope["schemaVersion"], fetch.CACHE_SCHEMA_VERSION)
        loaded = fetch._load_cache(payload, archive_hash)
        self.assertEqual(loaded["trips"][0]["id"], "T1")
        self.assertIsInstance(loaded["services"]["S1"]["added"], set)
        self.assertIsInstance(loaded["services"]["S1"]["removed"], set)
        with self.assertRaises(fetch.FeedLimitError):
            fetch._load_cache(payload, "b" * 64)

    def test_recent_stale_archive_is_used_when_refresh_fails(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as temp:
            state = Path(temp) / ".local/state/omarchy/bay-transit-board"
            state.mkdir(parents=True, mode=0o700)
            archive = state / fetch.ARCHIVE_NAME
            write_minimal_gtfs(archive)
            archive.chmod(0o600)
            stale_time = time.time() - fetch.GTFS_MAX_AGE_SECONDS - 60
            os.utime(archive, (stale_time, stale_time))
            with mock.patch.object(fetch, "STATE", state):
                with mock.patch.object(
                    fetch,
                    "download_to_fd",
                    side_effect=urllib.error.URLError("offline"),
                ) as download:
                    data = fetch.load_gtfs()
                    cached_data = fetch.load_gtfs()
            self.assertTrue(data["_usingStaleArchive"])
            self.assertTrue(cached_data["_usingStaleArchive"])
            self.assertEqual(download.call_count, 1)
            self.assertTrue((state / fetch.CACHE_NAME).is_file())

    def test_far_future_archive_is_not_considered_fresh_or_stale(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as temp:
            state = Path(temp) / ".local/state/omarchy/bay-transit-board"
            state.mkdir(parents=True, mode=0o700)
            archive = state / fetch.ARCHIVE_NAME
            write_minimal_gtfs(archive)
            archive.chmod(0o600)
            future_time = time.time() + fetch.MAX_CLOCK_SKEW_SECONDS + 60
            os.utime(archive, (future_time, future_time))
            with mock.patch.object(fetch, "STATE", state):
                with mock.patch.object(
                    fetch,
                    "download_to_fd",
                    side_effect=urllib.error.URLError("offline"),
                ):
                    with self.assertRaises(urllib.error.URLError):
                        fetch.load_gtfs()


def protobuf_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def protobuf_bytes(field: int, payload: bytes) -> bytes:
    return protobuf_varint((field << 3) | 2) + protobuf_varint(len(payload)) + payload


class DerivedLimitTests(unittest.TestCase):
    def test_realtime_entity_count_is_bounded(self):
        payload = protobuf_bytes(2, b"") + protobuf_bytes(2, b"")
        with mock.patch.object(fetch, "MAX_RT_ENTITIES", 1):
            with self.assertRaises(fetch.FeedLimitError):
                fetch.parse_trip_updates(payload)

    def test_alert_string_length_is_bounded(self):
        translation = protobuf_bytes(1, b"x" * 5)
        translated_string = protobuf_bytes(1, translation)
        alert = protobuf_bytes(11, translated_string)
        entity = protobuf_bytes(5, alert)
        payload = protobuf_bytes(2, entity)
        with mock.patch.object(fetch, "MAX_ALERT_CHARS", 4):
            with self.assertRaises(fetch.FeedLimitError):
                fetch.parse_alerts(payload)

    def test_unique_lines_are_capped(self):
        matches = [
            {"route": str(index), "headsign": f"head-{index}"}
            for index in range(4)
        ]
        with mock.patch.object(fetch, "MAX_LINES", 2):
            self.assertEqual(len(fetch.unique_lines(matches, {})), 2)

    def test_matching_trip_count_is_bounded(self):
        service = {
            "days": {day: True for day in fetch.WEEKDAYS},
            "start": "20200101",
            "end": "20991231",
            "added": set(),
            "removed": set(),
        }
        trip = {
            "id": "T",
            "route": "R",
            "service": "S",
            "headsign": "Beta",
            "stops": [("A", "A", "08:00:00"), ("B", "B", "08:10:00")],
        }
        gtfs = {"services": {"S": service}, "trips": [trip, {**trip, "id": "T2"}]}
        with mock.patch.object(fetch, "MAX_MATCHES", 1):
            with self.assertRaises(fetch.FeedLimitError):
                fetch.matching_trips(gtfs, "A", "B", fetch.date(2026, 9, 5))

    def test_serialized_stdout_is_capped(self):
        captured = type("Captured", (), {"buffer": io.BytesIO()})()
        with mock.patch.object(fetch.sys, "stdout", captured):
            self.assertFalse(fetch.emit_output({"advisories": ["x" * fetch.MAX_OUTPUT_BYTES]}))
        payload = captured.buffer.getvalue()
        self.assertLessEqual(len(payload), fetch.MAX_OUTPUT_BYTES)
        self.assertFalse(json.loads(payload)["ok"])

    def test_serialized_stdout_includes_newline_in_exact_boundary(self):
        limit = 32
        overhead = len(json.dumps({"x": ""}, separators=(",", ":")).encode())
        out = {"x": "y" * (limit - 1 - overhead)}
        captured = type("Captured", (), {"buffer": io.BytesIO()})()
        with mock.patch.object(fetch, "MAX_OUTPUT_BYTES", limit):
            with mock.patch.object(fetch.sys, "stdout", captured):
                self.assertTrue(fetch.emit_output(out))
        self.assertEqual(len(captured.buffer.getvalue()), limit)

    def test_one_transfer_journeys_are_computed_and_ranked(self):
        service = {
            "days": {day: True for day in fetch.WEEKDAYS},
            "start": "20200101",
            "end": "20991231",
            "added": set(),
            "removed": set(),
        }
        first = {
            "id": "FIRST",
            "route": "R1",
            "service": "S",
            "headsign": "Hub",
            "stops": [
                ("A", "A", "10:00:00"),
                ("H", "H", "10:05:00"),
            ],
        }
        second = {
            "id": "SECOND",
            "route": "R2",
            "service": "S",
            "headsign": "Beta",
            "stops": [
                ("H", "H", "10:10:00"),
                ("B", "B", "10:20:00"),
            ],
        }
        gtfs = {
            "services": {"S": service},
            "trips": [first, second],
        }
        journeys = fetch.transfer_journeys(
            gtfs,
            "A",
            "B",
            fetch.date(2026, 9, 5),
            datetime(2026, 9, 5, 9, 50, tzinfo=fetch.TZ),
        )
        self.assertEqual(len(journeys), 1)
        self.assertEqual(journeys[0]["hub"], "H")
        self.assertEqual(journeys[0]["first"]["id"], "FIRST")
        self.assertEqual(journeys[0]["second"]["id"], "SECOND")

    def test_later_express_connection_is_preferred(self):
        service = {
            "days": {day: True for day in fetch.WEEKDAYS},
            "start": "20200101",
            "end": "20991231",
            "added": set(),
            "removed": set(),
        }
        first = {
            "id": "FIRST",
            "route": "R1",
            "service": "S",
            "headsign": "Hub",
            "stops": [("A", "A", "10:00:00"), ("H", "H", "10:05:00")],
        }
        local = {
            "id": "LOCAL",
            "route": "R2",
            "service": "S",
            "headsign": "Beta",
            "stops": [("H", "H", "10:10:00"), ("B", "B", "11:00:00")],
        }
        express = {
            "id": "EXPRESS",
            "route": "R3",
            "service": "S",
            "headsign": "Beta",
            "stops": [("H", "H", "10:15:00"), ("B", "B", "10:30:00")],
        }
        now = datetime(2026, 9, 5, 9, 50, tzinfo=fetch.TZ)
        options = fetch.transfer_journeys(
            {"services": {"S": service}, "trips": [first, local, express]},
            "A",
            "B",
            now.date(),
            now,
        )
        ranked = fetch.rank_transfer_journeys(options, {}, now)
        self.assertEqual(ranked[0]["second"]["id"], "EXPRESS")

    def test_realtime_missed_connection_selects_consistent_journey(self):
        service = {
            "days": {day: True for day in fetch.WEEKDAYS},
            "start": "20200101",
            "end": "20991231",
            "added": set(),
            "removed": set(),
        }
        first_delayed = {
            "id": "DELAYED",
            "route": "R1",
            "service": "S",
            "headsign": "Hub",
            "stops": [("A", "A1", "10:00:00"), ("H", "H1", "10:05:00")],
        }
        first_ontime = {
            "id": "ONTIME",
            "route": "R1",
            "service": "S",
            "headsign": "Hub",
            "stops": [("A", "A2", "10:05:00"), ("H", "H2", "10:10:00")],
        }
        second = {
            "id": "SECOND",
            "route": "R2",
            "service": "S",
            "headsign": "Beta",
            "stops": [("H", "H3", "10:15:00"), ("B", "B1", "10:30:00")],
        }
        now = datetime(2026, 9, 5, 9, 50, tzinfo=fetch.TZ)
        options = fetch.transfer_journeys(
            {
                "services": {"S": service},
                "trips": [first_delayed, first_ontime, second],
            },
            "A",
            "B",
            now.date(),
            now,
        )
        unix_now = int(now.timestamp())
        rt = {
            "DELAYED": {
                "stops": {
                    "A1": unix_now + 20 * 60,
                    "H1": unix_now + 30 * 60,
                }
            }
        }
        ranked = fetch.rank_transfer_journeys(options, rt, now)
        self.assertEqual(ranked[0]["first"]["id"], "ONTIME")
        self.assertEqual(ranked[0]["hub"], "H")

    def test_tomorrow_service_is_evaluated_without_rolling_today(self):
        days = {day: False for day in fetch.WEEKDAYS}
        days["sunday"] = True
        service = {
            "days": days,
            "start": "20200101",
            "end": "20991231",
            "added": set(),
            "removed": set(),
        }
        first = {
            "id": "FIRST",
            "route": "R1",
            "service": "S",
            "headsign": "Hub",
            "stops": [("A", "A", "00:10:00"), ("H", "H", "00:15:00")],
        }
        second = {
            "id": "SECOND",
            "route": "R2",
            "service": "S",
            "headsign": "Beta",
            "stops": [("H", "H", "00:20:00"), ("B", "B", "00:30:00")],
        }
        now = datetime(2026, 9, 5, 23, 50, tzinfo=fetch.TZ)
        options = fetch.transfer_journeys(
            {"services": {"S": service}, "trips": [first, second]},
            "A",
            "B",
            now.date(),
            now,
        )
        ranked = fetch.rank_transfer_journeys(options, {}, now)
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0]["departAbs"], 24 * 3600 + 10 * 60)

    def test_duplicate_transfer_candidates_are_deduplicated_before_pairing(self):
        service = {
            "days": {day: True for day in fetch.WEEKDAYS},
            "start": "20200101",
            "end": "20991231",
            "added": set(),
            "removed": set(),
        }
        first = {
            "id": "FIRST",
            "route": "R1",
            "service": "S",
            "headsign": "Hub",
            "stops": [("A", "A", "10:00:00"), ("H", "H1", "10:05:00")],
        }
        second = {
            "id": "SECOND",
            "route": "R2",
            "service": "S",
            "headsign": "Beta",
            "stops": [("H", "H2", "10:10:00"), ("B", "B", "10:20:00")],
        }
        now = datetime(2026, 9, 5, 9, 50, tzinfo=fetch.TZ)
        gtfs = {
            "services": {"S": service},
            "trips": [first] * 100 + [second] * 100,
        }
        with mock.patch.object(fetch, "MAX_TRANSFER_PAIRING_ATTEMPTS", 1):
            options = fetch.transfer_journeys(
                gtfs,
                "A",
                "B",
                now.date(),
                now,
            )
        self.assertEqual(len(options), 1)

    def test_transfer_pairing_attempts_are_bounded(self):
        service = {
            "days": {day: True for day in fetch.WEEKDAYS},
            "start": "20200101",
            "end": "20991231",
            "added": set(),
            "removed": set(),
        }
        first = {
            "id": "FIRST",
            "route": "R1",
            "service": "S",
            "headsign": "Hub",
            "stops": [("A", "A", "10:00:00"), ("H", "H1", "10:05:00")],
        }
        seconds = [
            {
                "id": f"SECOND-{minute}",
                "route": "R2",
                "service": "S",
                "headsign": "Beta",
                "stops": [
                    ("H", f"H{minute}", f"10:{minute:02d}:00"),
                    ("B", f"B{minute}", f"10:{minute + 10:02d}:00"),
                ],
            }
            for minute in (10, 15)
        ]
        now = datetime(2026, 9, 5, 9, 50, tzinfo=fetch.TZ)
        with mock.patch.object(fetch, "MAX_TRANSFER_PAIRING_ATTEMPTS", 1):
            with self.assertRaises(fetch.FeedLimitError):
                fetch.transfer_journeys(
                    {"services": {"S": service}, "trips": [first, *seconds]},
                    "A",
                    "B",
                    now.date(),
                    now,
                )


class SecureStateTests(unittest.TestCase):
    def test_state_directory_rejects_symlinked_component(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as temp:
            base = Path(temp)
            outside = base / "outside"
            outside.mkdir()
            (base / "state-link").symlink_to(outside, target_is_directory=True)
            state = base / "state-link" / "bay-transit-board"
            with mock.patch.object(fetch, "STATE", state):
                with self.assertRaises(fetch.FeedLimitError):
                    with fetch._secure_state_directory():
                        self.fail("symlinked state component was accepted")

    def test_state_directory_rejects_writable_ancestor(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as temp:
            base = Path(temp)
            base.chmod(0o777)
            with mock.patch.object(fetch, "STATE", base / "state"):
                with self.assertRaises(fetch.FeedLimitError):
                    with fetch._secure_state_directory():
                        self.fail("writable state ancestor was accepted")

    def test_open_state_descriptor_survives_directory_rename(self):
        with tempfile.TemporaryDirectory(dir=Path.home()) as temp:
            base = Path(temp)
            state = base / "state"
            moved = base / "moved"
            with mock.patch.object(fetch, "STATE", state):
                with fetch._secure_state_directory() as directory_fd:
                    state.rename(moved)
                    fetch._atomic_write(
                        directory_fd,
                        "cache.json",
                        b"safe",
                        max_bytes=10,
                    )
            self.assertEqual((moved / "cache.json").read_bytes(), b"safe")

    def test_state_read_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            directory_fd = os.open(temp, os.O_RDONLY | os.O_DIRECTORY)
            try:
                target = Path(temp) / "target"
                target.write_bytes(b"payload")
                (Path(temp) / "link").symlink_to(target)
                with self.assertRaises(fetch.FeedLimitError):
                    fetch._read_state_file(directory_fd, "link", max_bytes=100)
            finally:
                os.close(directory_fd)

    def test_state_read_rejects_non_regular_file(self):
        with tempfile.TemporaryDirectory() as temp:
            directory_fd = os.open(temp, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.mkfifo(Path(temp) / "pipe")
                with self.assertRaises(fetch.FeedLimitError):
                    fetch._read_state_file(directory_fd, "pipe", max_bytes=100)
            finally:
                os.close(directory_fd)

    def test_exclusive_temp_refuses_existing_name(self):
        with tempfile.TemporaryDirectory() as temp:
            directory_fd = os.open(temp, os.O_RDONLY | os.O_DIRECTORY)
            token = "a" * 32
            name = f".cache.json.{token}.tmp"
            (Path(temp) / name).write_bytes(b"occupied")
            try:
                with mock.patch.object(fetch.secrets, "token_hex", return_value=token):
                    with self.assertRaises(fetch.FeedLimitError):
                        fetch._exclusive_temp(directory_fd, "cache.json")
            finally:
                os.close(directory_fd)

    def test_atomic_write_replaces_symlink_with_private_regular_file(self):
        with tempfile.TemporaryDirectory() as temp:
            directory_fd = os.open(temp, os.O_RDONLY | os.O_DIRECTORY)
            try:
                outside = Path(temp) / "outside"
                outside.write_bytes(b"outside")
                target = Path(temp) / "cache.json"
                target.symlink_to(outside)
                fetch._atomic_write(directory_fd, "cache.json", b"safe", max_bytes=10)
                self.assertFalse(target.is_symlink())
                self.assertEqual(target.read_bytes(), b"safe")
                self.assertEqual(outside.read_bytes(), b"outside")
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            finally:
                os.close(directory_fd)

    def test_replacement_rejects_swapped_temporary_name(self):
        with tempfile.TemporaryDirectory() as temp:
            directory_fd = os.open(temp, os.O_RDONLY | os.O_DIRECTORY)
            name, original_fd = fetch._exclusive_temp(directory_fd, "cache.json")
            try:
                os.unlink(name, dir_fd=directory_fd)
                replacement_fd = os.open(
                    name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=directory_fd,
                )
                os.close(replacement_fd)
                with self.assertRaises(fetch.FeedLimitError):
                    fetch._replace_temp(
                        directory_fd,
                        name,
                        "cache.json",
                        original_fd,
                    )
            finally:
                os.close(original_fd)
                try:
                    os.unlink(name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
                os.close(directory_fd)


if __name__ == "__main__":
    unittest.main()
