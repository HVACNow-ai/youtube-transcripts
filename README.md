# YouTube Transcripts

Download the transcripts of a YouTube playlist (or single videos) to text files. Then give the files to Claude (or a different AI tool) for summaries.

- Free. No API keys. No paid services.
- Continues where it stopped. You can run it again at any time.
- Makes "batch" files with 5 videos each, ready to upload to an AI chat.

## Contents

- [Requirements](#requirements)
- [Install](#install)
- [Quick start](#quick-start)
- [Private playlists](#private-playlists)
- [Output](#output)
- [Use the transcripts with Claude](#use-the-transcripts-with-claude)
- [Options](#options)
- [Exit codes](#exit-codes)
- [Troubleshooting](#troubleshooting)
- [Responsible use](#responsible-use)
- [Development](#development)

## Requirements

- Python 3.10 or newer.
- Your own computer. YouTube often blocks requests that come from cloud servers (for example AWS, Railway, or GitHub Actions). A home or office internet connection usually works.

## Install

```bash
git clone https://github.com/HVACNow-AI/youtube-transcripts.git
cd youtube-transcripts
python -m venv .venv
```

Activate the virtual environment:

```bash
# macOS / Linux
source .venv/bin/activate

# Windows (PowerShell)
.venv\Scripts\Activate.ps1
```

Install the dependencies:

```bash
pip install -r requirements.txt
```

## Quick start

```bash
python get_youtube_transcript.py "https://www.youtube.com/playlist?list=YOUR_PLAYLIST_ID"
```

The script shows each video while it works:

```text
Videos found: 100

[1/100] How We Get Leads With Google Ads
[2/100] 5 Facebook Ad Mistakes
    No transcript (TranscriptsDisabled).
...
Saved: 97  Already saved: 0  No transcript: 3  Unavailable: 0  Failed: 0  Not processed: 0
Batch files: 20 in output/batches
Report: output/report.csv
```

100 videos take approximately 6 to 10 minutes. The script waits a few seconds between videos to prevent a YouTube block.

You can also give video URLs, video IDs, or more than one playlist:

```bash
python get_youtube_transcript.py "https://youtu.be/dQw4w9WgXcQ" dQw4w9WgXcQ "https://www.youtube.com/playlist?list=PL..."
```

## Private playlists

The script must log in to read a **private** playlist. Use one of these options.

**Option A: Use your browser login (recommended).** Log in to YouTube in your browser. Then run:

```bash
python get_youtube_transcript.py --cookies-from-browser firefox "PLAYLIST_URL"
```

Supported browsers include `firefox`, `chrome`, `edge`, `safari`, and `brave`. If the script cannot read the cookies, close the browser and try again, or use Firefox.

**Option B: Make the playlist "Unlisted".** On YouTube, change the playlist visibility to **Unlisted**. Only people with the link can see it. Change it back to **Private** when the script is done.

> **Warning:** Browser cookies give full access to your Google account. Never share a `cookies.txt` file, and never commit it to Git. This repository ignores `cookies.txt` for this reason.

## Output

```text
output/
├── transcripts/
│   ├── dQw4w9WgXcQ - How We Get Leads With Google Ads.txt
│   └── ...
├── batches/
│   ├── batch_001.txt      ← 5 videos in each file
│   └── ...
└── report.csv             ← the result for each video
```

Each transcript file starts with a short header:

```text
TITLE: How We Get Leads With Google Ads
CHANNEL: Example Channel
URL: https://www.youtube.com/watch?v=dQw4w9WgXcQ
DURATION: 18:42
LANGUAGE: en (auto-generated)

[0:00] Today I want to show you the three ad types that ...

[1:01] The first one is ...
```

The `[m:ss]` time at the start of each paragraph lets the AI tell you where an idea is in the video. Use `--no-timestamps` to remove the times.

`report.csv` has one row for each video. The `status` column can be:

| Status | Meaning |
|---|---|
| `saved` | The transcript was saved in this run. |
| `already_saved` | The transcript was saved in an earlier run. |
| `no_transcript` | The video has no transcript (for example, the creator turned off captions). |
| `unavailable` | The video is private or deleted. |
| `failed` | A temporary error. Run the script again. |
| `not_processed` | The run stopped before this video. Run the script again. |

## Use the transcripts with Claude

1. Open a new chat in Claude (a Claude Project is best, because it can hold your company information).
2. Upload one file from `output/batches/`.
3. Paste the prompt below. Change the text in `[brackets]`.
4. Do this again for each batch file. Start a new chat after 2 or 3 batches.

```text
Each video in this file starts with "TITLE:".
Our company: [one or two sentences about your company, customers, and offer].

For each video, write in simple English:
1. Score 1-5: how useful is this video for our company.
2. Summary (max 3 sentences).
3. Top 5 tactics. One line each, with the [m:ss] time.
4. How our company can use each tactic. Be specific.
5. What does not fit our company, and why.
6. One test we can do this week: steps, budget, success metric.

Rules: Do not suggest claims that we cannot prove.
Ignore sponsor segments and course offers.
```

## Options

| Option | Default | Description |
|---|---|---|
| `-o`, `--output-dir` | `output` | The folder for the results. |
| `-l`, `--languages` | `en` | Language codes in priority order, for example `en,es`. |
| `-b`, `--batch-size` | `5` | The number of videos in each batch file. |
| `--delay` | `3` | Seconds between videos. A low value can cause a YouTube block. |
| `--retries` | `3` | Retries for temporary errors. |
| `--timeout` | `30` | Network timeout in seconds. |
| `--no-timestamps` | off | Do not put `[m:ss]` times in the transcripts. |
| `--cookies-from-browser` | none | Read a private playlist with your browser login. |
| `--cookies` | none | Read a private playlist with a `cookies.txt` file (Netscape format). |
| `--proxy` | none | Proxy URL. Use it only if YouTube blocks your IP address. |
| `-v`, `--verbose` | off | Show debug messages. |
| `-q`, `--quiet` | off | Show only problems. |

Run `python get_youtube_transcript.py --help` to see all options.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Complete. Each video has a saved transcript, or has no transcript on YouTube. |
| `1` | Input error. For example, the playlist cannot be read. |
| `2` | Not complete (YouTube block, network failure, or Ctrl+C). Run the same command again. |

You can stop the script with Ctrl+C at any time. It keeps all finished work.

## Troubleshooting

| Problem | Solution |
|---|---|
| "YouTube blocked the requests from your IP address." | Wait at least 1 hour. Then run the same command again. Do not decrease `--delay`. |
| "Cannot read ... If the playlist is private ..." | See [Private playlists](#private-playlists). Make sure that the URL is correct. |
| Many videos show `failed` | YouTube can change its website. Update the dependencies: `pip install -U yt-dlp youtube-transcript-api`. |
| A video shows `no_transcript` | The creator turned off captions, or the video has no captions in the languages you asked for. Try `--languages en,es`. |
| It does not work on a server | YouTube often blocks cloud servers. Run the script on your own computer, or use `--proxy`. |

## Responsible use

This tool reads the caption data that YouTube shows to all viewers. Use it for personal research at low volume. Respect the [YouTube Terms of Service](https://www.youtube.com/t/terms) and the rights of the creators. Do not publish the transcripts.

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
ruff check .
ruff format --check .
pytest
```

The tests use fakes, so they do not need the network. GitHub Actions runs the same checks on each push and pull request.

## License

[MIT](LICENSE)
