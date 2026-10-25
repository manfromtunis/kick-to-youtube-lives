<img src="assets/icon.svg" width="64" height="64" alt="">

# k2y

Kick livestream VODs to YouTube, automatically.

k2y watches a Kick channel and, every time a stream ends, downloads it, splits it into
thematic parts, writes French titles, descriptions and tags, draws a thumbnail, and uploads
everything to YouTube **private**. It runs on a schedule, survives crashes and reboots, and
never publishes anything on its own.

## What you get

- The **full stream**, with chapters in the description.
- The same stream **cut into thematic parts** (15 to 60 minutes each), one YouTube video per part.
- A **thumbnail** with a reaction photo and the part title for every upload.
- **Resumable state**: a crash, sleep or reboot continues where it stopped.
- **No duplicate uploads**: a part is recorded before the next one starts.
- **Private by default and by design**: `privacyStatus` is hard-coded to `private`.

## Requirements

| | |
|---|---|
| OS | Windows 10/11 (primary), Linux (tested). macOS is untested. |
| Python | 3.12 or newer |
| Binaries | `ffmpeg` and `ffprobe` on `PATH` |
| Accounts | a Kick channel, a YouTube channel, a Google Cloud project, a Gemini API key |
| YouTube | a **phone-verified** channel (required for uploads over 15 min and custom thumbnails) |
| Disk | about 20 GB free while a long stream is processed |
| Network | outbound HTTPS to Kick, Google and YouTube |

k2y uses the Python standard library only. There is no `pip install`, no virtualenv and no
database server.

## Quick start

1. Install Git, Python 3.12+ and FFmpeg. On Windows:

   ```cmd
   winget install Git.Git Python.Python.3.12 Gyan.FFmpeg
   ```

2. Clone the repository:

   ```cmd
   git clone https://github.com/manfromtunis/kick-to-youtube-lives.git
   cd kick-to-youtube-lives
   ```

3. Start the setup wizard. On Windows, double-click **`setup.cmd`**. On Linux or macOS:

   ```bash
   ./setup.sh
   ```

   The wizard checks Python and FFmpeg, saves your Google and Gemini credentials, asks for
   your Kick channel, authorizes YouTube, writes the configuration and offers to install the
   timer. It uploads nothing and can be re-run at any time.

4. Before the wizard, you still need a Google Cloud OAuth "Desktop app" client JSON and a
   Gemini API key. The **[full setup guide](docs/SETUP.md)** walks through creating both,
   with screenshots of every console page and a troubleshooting section. Budget about
   30 minutes, mostly clicking through Google Cloud.

Prefer doing it by hand? The guide documents the manual path as well.

## Commands

Run every command from the repository root (`kick-to-youtube-lives`).

| Command | What it does |
|---|---|
| `py -m k2y run` | Discover new VODs and advance every unfinished one. This is what the timer runs. |
| `py -m k2y status` | Show VODs, state, uploads done, attempts and last error. Add `--json` for machines. |
| `py -m k2y plan <vod_id>` | Download and plan the thematic cuts, print them, upload nothing. |
| `py -m k2y retry <vod_id>` | Clear the error and retry. `--restart-upload` also drops an unfinished upload session. |
| `py -m k2y skip <vod_id>` | Never process this VOD, and delete its work folder. |
| `py -m k2y backfill <vod_id>` | Add an older VOD from before your `since` date. |
| `py -m k2y refresh-metadata <vod_id>` | Rebuild and push descriptions, tags and hashtags for a published VOD. `--all` for all. |
| `py -m k2y setup` | Guided first-time setup: credentials, config, YouTube, timer. `--no-auth`, `--no-timer`, `--config-dir`. |
| `py -m k2y auth` | Authorize the YouTube account. Add `--paste` on a headless machine. |
| `py -m k2y install-timer` | Windows: create the 10-minute Task Scheduler task. Linux: print the crontab line. |

## How it works

```
Kick API -> download (ffmpeg) -> Gemini plan -> cut parts + thumbnails -> YouTube (private)
```

The pipeline stores its state in SQLite (`work_dir/k2y.db`) and advances one step at a time:

`new -> downloaded -> planned -> cut -> uploading -> done`, plus `failed` and `skipped`.

- The full VOD is uploaded first (part `0`), then each thematic part in order.
- YouTube quota is checked implicitly: `quotaExceeded` stops the run cleanly and it resumes the
  next day, without counting an attempt.
- Once every upload has a YouTube ID and a thumbnail, the local files for that VOD are deleted.
- A run lock prevents two runs from working at the same time.

## Costs and limits

- **YouTube quota:** 10,000 units per day; one upload costs 1,600. That is **6 uploads per day**.
  A stream with the full VOD plus 5 parts uses the whole day. Extra uploads wait for the next day.
- **Gemini:** long streams are sent in 10-minute audio chunks, so the API key needs billing.
  Budget roughly **$0.30 per 3-hour stream** at the time of writing; check current Gemini pricing.
- **Channel verification:** uploads longer than 15 minutes and custom thumbnails require a
  phone-verified YouTube channel. Do this before the first run.
- **OAuth testing mode:** while the Google Cloud app stays in "Testing", Google expires the
  refresh token after **7 days**. Re-run `py -m k2y auth` weekly, or publish the app. The setup
  guide explains both options.
- **Your PC must be on:** the timer only runs while you are logged in and the machine is awake.
  Nothing runs in the cloud.

## Privacy and secrets

- Every upload is `private`. k2y has no publish command.
- Credentials live **outside the repository**, in `~/.config/k2y/`
  (`%USERPROFILE%\.config\k2y\` on Windows), with `0600` permissions.
- The repository contains no keys, tokens or cookies.
- Each person who runs k2y uses their own Google Cloud project, Gemini key and YouTube account.

## Project layout

```
k2y/            Python package (CLI, pipeline, Kick, Gemini, media, YouTube)
tests/          stdlib unittest suite, no network
assets/faces/   reaction photos used by the thumbnail generator
docs/SETUP.md   full setup tutorial for a new machine
docs/superpowers/specs/   design documents
setup.cmd       double-click setup wizard for Windows
setup.sh        setup wizard launcher for Linux and macOS
k2y-task.pyw    Windows Task Scheduler launcher (no console window)
```

## Development

```bash
python3 -m unittest discover -s tests
```

The suite is offline and uses no real credentials.

## License

MIT. See [LICENSE](LICENSE).
