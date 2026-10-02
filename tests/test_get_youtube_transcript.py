"""Tests for get_youtube_transcript.py. They use fakes, so they need no network."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest
import requests
from youtube_transcript_api import (
    FetchedTranscript,
    FetchedTranscriptSnippet,
    IpBlocked,
    TranscriptsDisabled,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import get_youtube_transcript as gyt  # noqa: E402

# --- Helpers -----------------------------------------------------------------


def snippet(text: str, start: float, duration: float = 2.0) -> FetchedTranscriptSnippet:
    return FetchedTranscriptSnippet(text=text, start=start, duration=duration)


def transcript(video_id: str, snippets=None) -> FetchedTranscript:
    return FetchedTranscript(
        snippets=snippets or [snippet("hello", 0), snippet("world", 2)],
        video_id=video_id,
        language="English (auto-generated)",
        language_code="en",
        is_generated=True,
    )


class FakeApi:
    """Return a scripted result (or raise a scripted error) for each video ID."""

    def __init__(self, script: dict[str, list]):
        self.script = {key: list(value) for key, value in script.items()}
        self.calls: list[str] = []

    def fetch(self, video_id, languages=("en",)):
        self.calls.append(video_id)
        outcome = self.script[video_id].pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def make_options(**overrides):
    args = gyt.parse_args(["dummy"])
    args.delay = 0
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def vid(n: int) -> str:
    return f"video{n:06d}"  # 11 characters, a valid video ID


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(gyt.time, "sleep", lambda seconds: None)


# --- Parsing -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://m.youtube.com/watch?v=dQw4w9WgXcQ&t=30s", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?si=abc", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123", None),
        ("https://www.youtube.com/playlist?list=PL123", None),
        ("https://example.com/watch?v=dQw4w9WgXcQ", None),
        ("not a url", None),
    ],
)
def test_parse_video_id(source, expected):
    assert gyt.parse_video_id(source) == expected


def test_playlist_url_cleans_watch_urls():
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLabc&index=3"
    assert gyt.playlist_url(url) == "https://www.youtube.com/playlist?list=PLabc"


def test_safe_filename():
    assert gyt.safe_filename('Ads: "Why" <this> / works?') == "Ads Why this works"
    assert gyt.safe_filename("...") == "untitled"
    assert len(gyt.safe_filename("x" * 300)) == gyt.MAX_TITLE_CHARS


def test_format_time():
    assert gyt.format_time(5) == "0:05"
    assert gyt.format_time(754) == "12:34"
    assert gyt.format_time(3725) == "1:02:05"


def test_format_transcript_paragraphs_and_noise():
    lines = [snippet("[Music]", 0), snippet("first\nline", 1)]
    lines += [snippet("more", 30), snippet("end of minute", 59)]
    lines += [snippet("second  paragraph", 65), snippet("[Applause]", 70)]
    text = gyt.format_transcript(lines, timestamps=True)
    assert text == "[0:01] first line more end of minute\n\n[1:05] second paragraph"
    plain = gyt.format_transcript(lines, timestamps=False)
    assert plain.startswith("first line")


def test_parse_args_rejects_bad_values():
    with pytest.raises(SystemExit):
        gyt.parse_args(["x", "--batch-size", "0"])
    with pytest.raises(SystemExit):
        gyt.parse_args(["x", "--cookies", "missing-cookies.txt"])
    assert gyt.parse_args(["x", "-l", "en, es"]).languages == ["en", "es"]


# --- Download flow -----------------------------------------------------------


def test_download_all_statuses(tmp_path):
    videos = [
        gyt.Video(vid(1), "One"),
        gyt.Video(vid(2), "Two"),
        gyt.Video(vid(3), "[Private video]"),
        gyt.Video(vid(4), "Four"),
    ]
    api = FakeApi(
        {
            vid(1): [transcript(vid(1))],
            vid(2): [TranscriptsDisabled(vid(2))],
            vid(4): [requests.ConnectionError("down"), transcript(vid(4))],
        }
    )
    results = gyt.download_all(videos, api, tmp_path, make_options())

    assert [r.status for r in results] == [
        gyt.SAVED,
        gyt.NO_TRANSCRIPT,
        gyt.UNAVAILABLE,
        gyt.SAVED,
    ]
    assert api.calls == [vid(1), vid(2), vid(4), vid(4)]  # one retry for video 4
    saved = (tmp_path / f"{vid(1)} - One.txt").read_text(encoding="utf-8")
    assert saved.startswith(
        f"TITLE: One\nCHANNEL: unknown\nURL: https://www.youtube.com/watch?v={vid(1)}"
    )
    assert "LANGUAGE: en (auto-generated)" in saved
    assert not list(tmp_path.glob(".tmp-*"))


def test_transient_error_fails_after_retries(tmp_path):
    videos = [gyt.Video(vid(1), "One")]
    api = FakeApi({vid(1): [requests.Timeout("slow")] * 3})
    results = gyt.download_all(videos, api, tmp_path, make_options(retries=2))
    assert results[0].status == gyt.FAILED
    assert len(api.calls) == 3


def test_block_stops_the_run_and_resume_continues(tmp_path):
    videos = [gyt.Video(vid(n), f"Video {n}") for n in (1, 2, 3)]
    first = FakeApi({vid(1): [transcript(vid(1))], vid(2): [IpBlocked(vid(2))]})
    results = gyt.download_all(videos, first, tmp_path, make_options())
    assert [r.status for r in results] == [gyt.SAVED, gyt.NOT_PROCESSED, gyt.NOT_PROCESSED]

    second = FakeApi({vid(2): [transcript(vid(2))], vid(3): [transcript(vid(3))]})
    results = gyt.download_all(videos, second, tmp_path, make_options())
    assert [r.status for r in results] == [gyt.ALREADY_SAVED, gyt.SAVED, gyt.SAVED]
    assert second.calls == [vid(2), vid(3)]


def test_ctrl_c_keeps_finished_work(tmp_path):
    videos = [gyt.Video(vid(n), f"Video {n}") for n in (1, 2)]
    api = FakeApi({vid(1): [transcript(vid(1))], vid(2): [KeyboardInterrupt()]})
    results = gyt.download_all(videos, api, tmp_path, make_options())
    assert [r.status for r in results] == [gyt.SAVED, gyt.NOT_PROCESSED]


def test_write_batches_keeps_playlist_order(tmp_path):
    transcripts_dir = tmp_path / "t"
    batch_dir = tmp_path / "b"
    transcripts_dir.mkdir()
    batch_dir.mkdir()
    (batch_dir / "batch_009.txt").write_text("old", encoding="utf-8")
    results = []
    for n in range(1, 8):
        path = transcripts_dir / f"{vid(n)} - V{n}.txt"
        path.write_text(f"TITLE: V{n}\n", encoding="utf-8")
        results.append(gyt.Result(gyt.Video(vid(n), f"V{n}"), gyt.SAVED, path=path))
    results.append(gyt.Result(gyt.Video(vid(8), "V8"), gyt.NO_TRANSCRIPT))

    assert gyt.write_batches(results, batch_dir, size=3) == 3
    assert sorted(p.name for p in batch_dir.iterdir()) == [
        "batch_001.txt",
        "batch_002.txt",
        "batch_003.txt",
    ]
    first = (batch_dir / "batch_001.txt").read_text(encoding="utf-8")
    assert first.count("TITLE:") == 3
    assert first.index("V1") < first.index("V2") < first.index("V3")


# --- End to end (main) -------------------------------------------------------


def run_main(tmp_path, monkeypatch, videos, script):
    monkeypatch.setattr(gyt, "collect_videos", lambda sources, options, session: videos)
    monkeypatch.setattr(gyt, "YouTubeTranscriptApi", lambda **kwargs: FakeApi(script))
    return gyt.main(["PLAYLIST", "-o", str(tmp_path), "--delay", "0", "-q"])


def test_main_complete(tmp_path, monkeypatch):
    videos = [gyt.Video(vid(1), "One"), gyt.Video(vid(2), "Two")]
    script = {vid(1): [transcript(vid(1))], vid(2): [TranscriptsDisabled(vid(2))]}
    assert run_main(tmp_path, monkeypatch, videos, script) == gyt.EXIT_OK

    with open(tmp_path / "report.csv", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["status"] for row in rows] == [gyt.SAVED, gyt.NO_TRANSCRIPT]
    assert (tmp_path / "batches" / "batch_001.txt").exists()


def test_main_incomplete_when_blocked(tmp_path, monkeypatch):
    videos = [gyt.Video(vid(1), "One")]
    assert run_main(tmp_path, monkeypatch, videos, {vid(1): [IpBlocked(vid(1))]}) == 2


def test_main_input_error(tmp_path, monkeypatch):
    def fail(sources, options, session):
        raise gyt.SourceError("Cannot read the playlist.")

    monkeypatch.setattr(gyt, "collect_videos", fail)
    assert gyt.main(["PLAYLIST", "-o", str(tmp_path), "-q"]) == gyt.EXIT_INPUT_ERROR


# --- Finding videos ----------------------------------------------------------


class FakeYoutubeDL:
    info: dict | Exception = {}

    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        if isinstance(self.info, Exception):
            raise self.info
        return self.info


def test_list_playlist_reads_entries(monkeypatch):
    FakeYoutubeDL.info = {
        "entries": [
            {"id": vid(1), "title": "One", "channel": "Ch", "duration": 61, "ie_key": "Youtube"},
            None,
            {"id": "PLnested", "ie_key": "YoutubeTab", "url": "https://youtube.com/playlist"},
            {"id": vid(2), "title": "[Private video]", "ie_key": "Youtube"},
        ]
    }
    monkeypatch.setattr(gyt, "YoutubeDL", FakeYoutubeDL)
    videos = gyt.list_playlist("https://www.youtube.com/playlist?list=PL1", {})
    assert videos == [
        gyt.Video(vid(1), "One", "Ch", 61),
        gyt.Video(vid(2), "[Private video]"),
    ]


def test_list_playlist_errors(monkeypatch):
    monkeypatch.setattr(gyt, "YoutubeDL", FakeYoutubeDL)
    FakeYoutubeDL.info = gyt.DownloadError("This playlist does not exist")
    with pytest.raises(gyt.SourceError, match="private"):
        gyt.list_playlist("https://www.youtube.com/playlist?list=PL1", {})
    FakeYoutubeDL.info = {"entries": []}
    with pytest.raises(gyt.SourceError, match="No videos"):
        gyt.list_playlist("https://www.youtube.com/playlist?list=PL1", {})


def test_collect_videos_removes_duplicates(monkeypatch):
    monkeypatch.setattr(
        gyt,
        "list_playlist",
        lambda source, options: [gyt.Video(vid(1), "One"), gyt.Video(vid(2), "Two")],
    )
    monkeypatch.setattr(gyt, "lookup_video", lambda video_id, session: gyt.Video(video_id, "X"))
    videos = gyt.collect_videos(["PLAYLIST_URL", vid(1), vid(3)], {}, None)
    assert [v.video_id for v in videos] == [vid(1), vid(2), vid(3)]
