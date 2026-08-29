import io
import tempfile
import unittest
import zipfile
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
        with mock.patch.object(fetch.urllib.request, "urlopen", return_value=response):
            with self.assertRaises(fetch.FeedLimitError):
                fetch.get_bytes("https://example.test", max_bytes=4)

    def test_get_bytes_rejects_streamed_oversize_response(self):
        response = FakeResponse(b"abcde")
        with mock.patch.object(fetch.urllib.request, "urlopen", return_value=response):
            with self.assertRaises(fetch.FeedLimitError):
                fetch.get_bytes("https://example.test", max_bytes=4)

    def test_download_removes_partial_oversize_file(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "feed.tmp"
            response = FakeResponse(b"abcde")
            with mock.patch.object(fetch.urllib.request, "urlopen", return_value=response):
                with self.assertRaises(fetch.FeedLimitError):
                    fetch.download_file("https://example.test", target, max_bytes=4)
            self.assertFalse(target.exists())


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


if __name__ == "__main__":
    unittest.main()
