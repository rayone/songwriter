"""Licensed downloads must not redirect the server to arbitrary hosts."""
import io
import json
from urllib.parse import urlparse
from urllib.request import Request

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from songwriter.corpus import licensed
from songwriter.corpus.licensed import _LicensedRedirects


@pytest.mark.parametrize("target", [
    "https://127.0.0.1/private",
    "https://huggingface.co.attacker.example/songs.parquet",
    "http://us.aws.cdn.hf.co/songs.parquet",
    "https://us.aws.cdn.hf.co:8443/songs.parquet",
    "https://user:password@us.aws.cdn.hf.co/songs.parquet",
])
def test_licensed_source_rejects_redirects_outside_approved_https_hosts(target):
    with pytest.raises(RuntimeError, match="approved HTTPS hosts"):
        _LicensedRedirects().redirect_request(
            Request("https://huggingface.co/source", method="HEAD"),
            None, 302, "Found", {}, target,
        )


@pytest.fixture
def licensed_source(monkeypatch):
    rows = [
        [("Northbound", "Mr.Kitty"), ("Other Song", "Another Artist"), ("Daybreak", "MR KITTY")],
        [("Homeward", "Mr-Kitty"), ("Noise", "!!!"), ("Collaboration", "Mr.Kitty feat. Another")],
    ]
    files = []
    for records in rows:
        output = io.BytesIO()
        pq.write_table(pa.Table.from_pylist([
            {"title": title, "artist": artist, "tag": "electronic", "year": 2020,
             "lyrics": "[Verse]\nWe leave a light above the door\nThe morning finds us on the shore"}
            for title, artist in records
        ]), output, row_group_size=1)
        files.append(output.getvalue())
    exports = [licensed._EXPORT_ROOT + f"/{index}.parquet" for index in range(len(files))]
    faults = {}

    class Response(io.BytesIO):
        def __init__(self, body=b"", *, status=200, headers=None, url=""):
            super().__init__(body)
            self.status = status
            self.headers = headers or {}
            self.url = url

        def geturl(self):
            return self.url

        def getheader(self, name):
            return self.headers.get(name)

    class Opener:
        def open(self, request, timeout):
            if request.full_url == licensed._EXPORT_ROOT:
                return Response(json.dumps(exports).encode())
            index = exports.index(request.full_url)
            return Response(headers={"Content-Length": str(len(files[index]))},
                            url=f"https://us.aws.cdn.hf.co/{index}.parquet")

    class Connection:
        def __init__(self, host, timeout):
            pass

        def request(self, method, path, headers):
            self.index = int(urlparse(path).path.strip("/").split(".")[0])
            self.start, self.end = map(int, headers["Range"].removeprefix("bytes=").split("-"))

        def getresponse(self):
            if faults.get(self.index) == "disconnect":
                raise OSError("The source disconnected")
            data = files[self.index]
            if faults.get(self.index) == "ignore_range":
                return Response(data)
            return Response(data[self.start:self.end + 1], status=206, headers={
                "Content-Range": f"bytes {self.start}-{self.end}/{len(data)}",
            })

        def close(self):
            pass

    monkeypatch.setattr(licensed, "build_opener", lambda *args: Opener())
    monkeypatch.setattr(licensed.http.client, "HTTPSConnection", Connection)
    return faults


@pytest.mark.parametrize(("artist", "titles"), [
    (" mR. kItTy ", {"Northbound", "Daybreak", "Homeward"}),
    ("!!!", {"Noise"}),
])
def test_artist_reader_matches_complete_names_across_files_and_row_groups(licensed_source, artist, titles):
    songs = licensed.load_artist(artist)
    assert {song.title for song in songs} == titles


def test_artist_reader_does_not_return_partial_results_after_source_failure(licensed_source):
    licensed_source[1] = "disconnect"
    with pytest.raises(RuntimeError) as error:
        licensed.load_artist("Mr.Kitty")
    assert isinstance(error.value.__cause__, OSError)


def test_artist_reader_rejects_a_source_that_ignores_partial_downloads(licensed_source):
    licensed_source[0] = "ignore_range"
    with pytest.raises(RuntimeError):
        licensed.load_artist("Mr.Kitty")
