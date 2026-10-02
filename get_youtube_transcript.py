#!/usr/bin/env python3
"""Download the transcripts of YouTube playlists and videos. Free. No API keys.

For each source (playlist URL, video URL, or video ID), the script finds the
videos, downloads the transcript of each video, and writes:

    <output>/transcripts/<video_id> - <title>.txt   one file for each video
    <output>/batches/batch_001.txt                   N videos in each file
    <output>/report.csv                              the result for each video

Run the same command again to continue an earlier run. The script skips videos
that already have a saved transcript.

Exit codes:
    0  Complete. Each video has a saved transcript, or has no transcript on YouTube.
    1  Input error. For example, the playlist cannot be read.
    2  Not complete (YouTube block, network failure, or Ctrl+C). Run it again.
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import random
import re
import sys
import tempfile
import time
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from youtube_transcript_api import (
    AgeRestricted,
    CouldNotRetrieveTranscript,
    FailedToCreateConsentCookie,
    FetchedTranscriptSnippet,
    InvalidVideoId,
    NoTranscriptFound,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
    VideoUnplayable,
    YouTubeDataUnparsable,
    YouTubeRequestFailed,
    YouTubeTranscriptApi,
)
from youtube_transcript_api.proxies import GenericProxyConfig
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

__version__ = "1.0.0"

log = logging.getLogger("get_youtube_transcript")

EXIT_OK = 0
EXIT_INPUT_ERROR = 1
EXIT_INCOMPLETE = 2

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
TRANSCRIPT_FILE_RE = re.compile(r"^([A-Za-z0-9_-]{11}) - .*\.txt$")
NON_SPEECH_RE = re.compile(
    r"\[(?:music|applause|laughter|cheering|silence|inaudible|__)\]", re.IGNORECASE
)
INVALID_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
UNAVAILABLE_TITLES = {"[Private video]", "[Deleted video]"}
PARAGRAPH_SECONDS = 60
MAX_TITLE_CHARS = 80
BATCH_SEPARATOR = "\n\n" + "=" * 20 + " NEXT VIDEO " + "=" * 20 + "\n\n"

# The result does not change if you try again.
PERMANENT_ERRORS: tuple[type[Exception], ...] = (
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
    VideoUnplayable,
    AgeRestricted,
    InvalidVideoId,
)
# The next attempt can succeed.
TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
    YouTubeRequestFailed,
    YouTubeDataUnparsable,
    FailedToCreateConsentCookie,
    requests.RequestException,
)

# Result status for each video.
SAVED = "saved"
ALREADY_SAVED = "already_saved"
NO_TRANSCRIPT = "no_transcript"
UNAVAILABLE = "unavailable"
FAILED = "failed"
NOT_PROCESSED = "not_processed"
INCOMPLETE_STATUSES = {FAILED, NOT_PROCESSED}


class SourceError(Exception):
    """A source (playlist or video) cannot be read."""


class Blocked(Exception):
    """YouTube blocks the requests from this IP address."""


@dataclass(frozen=True)
class Video:
    video_id: str
    title: str
    channel: str = ""
    duration: float | None = None

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


@dataclass
class Result:
    video: Video
    status: str
    detail: str = ""
    path: Path | None = None


class TimeoutSession(requests.Session):
    """A requests session with a default timeout, so that no request waits forever."""

    def __init__(self, timeout: float) -> None:
        super().__init__()
        self._timeout = timeout

    def request(self, *args, **kwargs):  # type: ignore[override]
        kwargs.setdefault("timeout", self._timeout)
        return super().request(*args, **kwargs)


class _YtDlpLogger:
    """Send yt-dlp messages to the debug log. The script logs its own errors."""

    def debug(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    info = debug
    warning = debug
    error = debug


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def format_time(seconds: float) -> str:
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def safe_filename(title: str) -> str:
    text = unicodedata.normalize("NFKC", title)
    text = INVALID_FILENAME_CHARS_RE.sub("", text)
    text = " ".join(text.split())[:MAX_TITLE_CHARS].rstrip(" .")
    return text or "untitled"


def atomic_write(path: Path, text: str) -> None:
    """Write a file in one step. A stopped run never leaves half a file."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.chmod(tmp_name, 0o644)  # mkstemp makes the file private (0600)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def sleep_with_jitter(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds + random.uniform(0, seconds / 2))


# ---------------------------------------------------------------------------
# Find the videos
# ---------------------------------------------------------------------------


def parse_video_id(source: str) -> str | None:
    """Return the video ID if the source is one video. Return None for a playlist."""
    source = source.strip()
    if VIDEO_ID_RE.match(source):
        return source
    url = urlparse(source)
    query = parse_qs(url.query)
    if "list" in query:
        return None
    host = (url.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    candidate = ""
    if host == "youtu.be":
        candidate = url.path.strip("/").split("/")[0]
    elif host == "youtube.com" or host.endswith(".youtube.com"):
        parts = url.path.strip("/").split("/")
        if url.path == "/watch":
            candidate = query.get("v", [""])[0]
        elif len(parts) >= 2 and parts[0] in {"shorts", "embed", "live", "v"}:
            candidate = parts[1]
    return candidate if VIDEO_ID_RE.match(candidate) else None


def playlist_url(source: str) -> str:
    """Change 'watch?v=...&list=ID' to a clean playlist URL."""
    query = parse_qs(urlparse(source.strip()).query)
    if "list" in query:
        return f"https://www.youtube.com/playlist?list={query['list'][0]}"
    return source.strip()


def list_playlist(source: str, ydl_options: dict) -> list[Video]:
    try:
        with YoutubeDL(ydl_options) as ydl:
            info = ydl.extract_info(playlist_url(source), download=False)
            entries = list((info or {}).get("entries") or [])
    except DownloadError as error:
        raise SourceError(
            f"Cannot read {source}. If the playlist is private, use --cookies-from-browser "
            f"or set the playlist to 'Unlisted'. Details: {error}"
        ) from error
    if not entries:
        raise SourceError(f"No videos found in {source}.")

    videos = []
    for entry in entries:
        if not entry:
            continue
        video_id = entry.get("id") or ""
        if not VIDEO_ID_RE.match(video_id) or entry.get("ie_key") not in (None, "Youtube"):
            log.warning("Skipped an item that is not a video: %s", entry.get("url") or video_id)
            continue
        videos.append(
            Video(
                video_id=video_id,
                title=entry.get("title") or video_id,
                channel=entry.get("channel") or entry.get("uploader") or "",
                duration=entry.get("duration"),
            )
        )
    return videos


def lookup_video(video_id: str, session: requests.Session) -> Video:
    """Find the title and channel of one video. If this fails, use the video ID."""
    try:
        response = session.get(
            "https://www.youtube.com/oembed",
            params={"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"},
        )
        response.raise_for_status()
        data = response.json()
        return Video(video_id, data.get("title") or video_id, data.get("author_name") or "")
    except (requests.RequestException, ValueError) as error:
        log.debug("No title for %s: %s", video_id, error)
        return Video(video_id, video_id)


def collect_videos(
    sources: Iterable[str], ydl_options: dict, session: requests.Session
) -> list[Video]:
    videos: list[Video] = []
    seen: set[str] = set()
    for source in sources:
        video_id = parse_video_id(source)
        found = (
            [lookup_video(video_id, session)] if video_id else list_playlist(source, ydl_options)
        )
        for video in found:
            if video.video_id not in seen:
                seen.add(video.video_id)
                videos.append(video)
    return videos


# ---------------------------------------------------------------------------
# Download and save the transcripts
# ---------------------------------------------------------------------------


def format_transcript(snippets: Iterable[FetchedTranscriptSnippet], timestamps: bool) -> str:
    """Join the caption lines into paragraphs of approximately 60 seconds."""
    paragraphs: list[tuple[float, str]] = []
    words: list[str] = []
    start: float | None = None
    for snippet in snippets:
        text = " ".join(NON_SPEECH_RE.sub(" ", snippet.text).split())
        if not text:
            continue
        if start is None:
            start = snippet.start
        words.append(text)
        if snippet.start + snippet.duration - start >= PARAGRAPH_SECONDS:
            paragraphs.append((start, " ".join(words)))
            words, start = [], None
    if words and start is not None:
        paragraphs.append((start, " ".join(words)))
    if timestamps:
        return "\n\n".join(f"[{format_time(s)}] {text}" for s, text in paragraphs)
    return "\n\n".join(text for _, text in paragraphs)


def render_file(video: Video, body: str, language_code: str, generated: bool) -> str:
    header = [
        f"TITLE: {video.title}",
        f"CHANNEL: {video.channel or 'unknown'}",
        f"URL: {video.url}",
    ]
    if video.duration:
        header.append(f"DURATION: {format_time(video.duration)}")
    header.append(f"LANGUAGE: {language_code}{' (auto-generated)' if generated else ''}")
    return "\n".join(header) + "\n\n" + body + "\n"


def existing_transcripts(folder: Path) -> dict[str, Path]:
    found = {}
    for path in folder.glob("*.txt"):
        match = TRANSCRIPT_FILE_RE.match(path.name)
        if match:
            found[match.group(1)] = path
    return found


def fetch_with_retry(
    api: YouTubeTranscriptApi, video_id: str, languages: Sequence[str], retries: int
):
    for attempt in range(retries + 1):
        try:
            return api.fetch(video_id, languages=languages)
        except TRANSIENT_ERRORS as error:
            if attempt == retries:
                raise
            wait = 5 * 2**attempt
            log.warning(
                "    Temporary error (%s). Retry %d of %d in %ds.",
                type(error).__name__,
                attempt + 1,
                retries,
                wait,
            )
            sleep_with_jitter(wait)
    raise AssertionError("unreachable")


def download_one(
    api: YouTubeTranscriptApi, video: Video, folder: Path, options: argparse.Namespace
) -> Result:
    try:
        transcript = fetch_with_retry(api, video.video_id, options.languages, options.retries)
    except RequestBlocked as error:
        raise Blocked(str(error)) from error
    except PERMANENT_ERRORS as error:
        log.warning("    No transcript (%s).", type(error).__name__)
        return Result(video, NO_TRANSCRIPT, type(error).__name__)
    except (CouldNotRetrieveTranscript, requests.RequestException) as error:
        log.warning("    Failed (%s). Run the script again later.", type(error).__name__)
        return Result(video, FAILED, type(error).__name__)

    body = format_transcript(transcript, timestamps=options.timestamps)
    if not body:
        log.warning("    The transcript is empty.")
        return Result(video, NO_TRANSCRIPT, "empty transcript")
    path = folder / f"{video.video_id} - {safe_filename(video.title)}.txt"
    atomic_write(path, render_file(video, body, transcript.language_code, transcript.is_generated))
    return Result(video, SAVED, path=path)


def download_all(
    videos: Sequence[Video], api: YouTubeTranscriptApi, folder: Path, options: argparse.Namespace
) -> list[Result]:
    existing = existing_transcripts(folder)
    results: list[Result] = []
    try:
        for number, video in enumerate(videos, start=1):
            if video.title in UNAVAILABLE_TITLES:
                log.info("[%d/%d] Skipped: %s", number, len(videos), video.title)
                results.append(Result(video, UNAVAILABLE, "private or deleted"))
                continue
            if video.video_id in existing:
                results.append(Result(video, ALREADY_SAVED, path=existing[video.video_id]))
                continue
            log.info("[%d/%d] %s", number, len(videos), video.title)
            try:
                results.append(download_one(api, video, folder, options))
            except Blocked:
                log.error(
                    "YouTube blocked the requests from your IP address. Wait at least "
                    "1 hour (or use --proxy), then run the same command again."
                )
                break
            if number < len(videos):
                sleep_with_jitter(options.delay)
    except KeyboardInterrupt:
        log.warning("Stopped by the user.")

    done = {result.video.video_id for result in results}
    for video in videos:
        if video.video_id not in done:
            if video.video_id in existing:
                results.append(Result(video, ALREADY_SAVED, path=existing[video.video_id]))
            else:
                results.append(Result(video, NOT_PROCESSED))
    order = {video.video_id: index for index, video in enumerate(videos)}
    results.sort(key=lambda result: order[result.video.video_id])
    return results


# ---------------------------------------------------------------------------
# Batches and report
# ---------------------------------------------------------------------------


def write_batches(results: Sequence[Result], folder: Path, size: int) -> int:
    for old in folder.glob("batch_*.txt"):
        old.unlink()
    paths = [r.path for r in results if r.status in (SAVED, ALREADY_SAVED) and r.path]
    count = 0
    for start in range(0, len(paths), size):
        count += 1
        parts = [path.read_text(encoding="utf-8").strip() for path in paths[start : start + size]]
        atomic_write(folder / f"batch_{count:03d}.txt", BATCH_SEPARATOR.join(parts) + "\n")
    return count


def write_report(results: Sequence[Result], path: Path) -> None:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["video_id", "title", "channel", "url", "status", "detail", "file"])
    for r in results:
        writer.writerow(
            [
                r.video.video_id,
                r.video.title,
                r.video.channel,
                r.video.url,
                r.status,
                r.detail,
                r.path.name if r.path else "",
            ]
        )
    atomic_write(path, buffer.getvalue())


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def _non_negative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return number


def _non_negative_float(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return number


def _positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be more than 0")
    return number


def _languages(value: str) -> list[str]:
    codes = [code.strip() for code in value.split(",") if code.strip()]
    if not codes:
        raise argparse.ArgumentTypeError("give at least one language code")
    return codes


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="get_youtube_transcript.py",
        description="Download the transcripts of YouTube playlists and videos.",
        epilog="Exit codes: 0 = complete, 1 = input error, 2 = not complete (run again).",
    )
    parser.add_argument(
        "sources",
        nargs="+",
        metavar="SOURCE",
        help="Playlist URL, video URL, or video ID. You can give more than one.",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path("output"),
        help="Folder for the results (default: output).",
    )
    parser.add_argument(
        "-l",
        "--languages",
        type=_languages,
        default=["en"],
        help="Language codes in priority order, comma-separated (default: en).",
    )
    parser.add_argument(
        "-b",
        "--batch-size",
        type=_positive_int,
        default=5,
        help="Videos in each batch file (default: 5).",
    )
    parser.add_argument(
        "--delay",
        type=_non_negative_float,
        default=3.0,
        help="Seconds between videos (default: 3). A low value can cause a block.",
    )
    parser.add_argument(
        "--retries",
        type=_non_negative_int,
        default=3,
        help="Retries for temporary errors (default: 3).",
    )
    parser.add_argument(
        "--timeout",
        type=_positive_float,
        default=30.0,
        help="Network timeout in seconds (default: 30).",
    )
    parser.add_argument(
        "--no-timestamps",
        dest="timestamps",
        action="store_false",
        help="Do not put [m:ss] times at the start of each paragraph.",
    )
    cookies = parser.add_mutually_exclusive_group()
    cookies.add_argument(
        "--cookies-from-browser",
        metavar="BROWSER",
        help="Read a private playlist with your browser login "
        "(firefox, chrome, edge, safari, brave).",
    )
    cookies.add_argument(
        "--cookies",
        type=Path,
        metavar="FILE",
        help="Read a private playlist with a cookies.txt file (Netscape format).",
    )
    parser.add_argument(
        "--proxy",
        metavar="URL",
        help="Proxy URL, for example http://user:pass@host:port. "
        "Use only if YouTube blocks your IP address.",
    )
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument("-v", "--verbose", action="store_true", help="Show debug messages.")
    verbosity.add_argument("-q", "--quiet", action="store_true", help="Show only problems.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)
    if args.cookies and not args.cookies.is_file():
        parser.error(f"cookies file not found: {args.cookies}")
    return args


def setup_logging(verbose: bool, quiet: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING if quiet else logging.INFO
    fmt = "%(asctime)s %(levelname)s %(message)s" if verbose else "%(message)s"
    logging.basicConfig(level=level, format=fmt, stream=sys.stderr)


def build_ydl_options(args: argparse.Namespace) -> dict:
    options: dict = {
        "extract_flat": "in_playlist",
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": args.timeout,
        "logger": _YtDlpLogger(),
    }
    if args.cookies_from_browser:
        options["cookiesfrombrowser"] = (args.cookies_from_browser,)
    if args.cookies:
        options["cookiefile"] = str(args.cookies)
    if args.proxy:
        options["proxy"] = args.proxy
    return options


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose, args.quiet)

    transcript_dir = args.output_dir / "transcripts"
    batch_dir = args.output_dir / "batches"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    batch_dir.mkdir(parents=True, exist_ok=True)

    session = TimeoutSession(args.timeout)
    proxy = GenericProxyConfig(http_url=args.proxy, https_url=args.proxy) if args.proxy else None
    api = YouTubeTranscriptApi(proxy_config=proxy, http_client=session)

    try:
        videos = collect_videos(args.sources, build_ydl_options(args), session)
    except SourceError as error:
        log.error("%s", error)
        return EXIT_INPUT_ERROR
    except KeyboardInterrupt:
        log.warning("Stopped by the user.")
        return EXIT_INCOMPLETE
    log.info("Videos found: %d\n", len(videos))

    results = download_all(videos, api, transcript_dir, args)
    batches = write_batches(results, batch_dir, args.batch_size)
    write_report(results, args.output_dir / "report.csv")

    counts = {
        status: 0
        for status in (SAVED, ALREADY_SAVED, NO_TRANSCRIPT, UNAVAILABLE, FAILED, NOT_PROCESSED)
    }
    for result in results:
        counts[result.status] += 1
    log.info(
        "\nSaved: %d  Already saved: %d  No transcript: %d  Unavailable: %d  "
        "Failed: %d  Not processed: %d",
        *counts.values(),
    )
    log.info("Batch files: %d in %s", batches, batch_dir)
    log.info("Report: %s", args.output_dir / "report.csv")

    if any(result.status in INCOMPLETE_STATUSES for result in results):
        log.warning("Not complete. Run the same command again to continue.")
        return EXIT_INCOMPLETE
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
