# k2y v1: Kick VODs to YouTube, automatically

Status: approved by Melek via session goal, 2026-10-22. Spike results in section 9.

## 1. Intent

Melek streams on Kick (`kick.com/randomguy42`) in Tunisian darija. He wants every finished
stream to land on his YouTube channel without touching anything:

- the **full VOD**, with chapters in the description, and
- the same stream **split into thematic parts** (cut where the subject or activity changes),
  each its own YouTube video,
- every video with a **thumbnail**.

Ready-to-use short clips are a later goal (non-goal for v1, section 3).

## 2. Goals

1. A finished Kick VOD is detected within 10 minutes of the laptop being awake.
2. It is downloaded at the best rendition, split into thematic parts, given titles,
   descriptions and thumbnails, and uploaded **private** to YouTube.
3. Nothing is processed twice; a crash, sleep or reboot resumes where it stopped.
4. A `k2y` CLI shows status and lets an agent or Melek retry, re-plan or skip a VOD.
5. Runs unattended on Melek's **Windows** laptop through a Windows Task Scheduler task
   (every 10 minutes). The same code runs on Linux (used for the end-to-end test on the VM).

## 3. Non-goals and anti-goals

Non-goals (v1): short clips / vertical shorts, multiple channels or creators, a web UI,
publishing (making videos public), re-encoding for quality, Docker, a server deployment.

Anti-goals (a failure even if it "works"):
- A video goes **public** without Melek. Every upload is `privacyStatus: private`, hard-coded.
- A VOD is uploaded twice, or a part is silently missing (gap in the timeline).
- Local disk fills up: source files are deleted after a VOD is fully uploaded.
- Secrets in the repo, in logs, or in SQLite.

## 4. Constraints (facts, checked 2026-10-22)

- **Kick API.** `GET https://kick.com/api/v2/channels/randomguy42/videos` (any browser
  User-Agent, no auth) returns the VOD list. Per VOD: `id`, `session_title`, `start_time`,
  `duration` (ms), `is_live`, `source` (HLS `master.m3u8`), `categories[].name`,
  `language`. Renditions in the master: 720p30 (best), 480p30, 360p30, 160p30.
  Endpoint is unofficial: handle non-200 and schema drift with a clear error, never crash
  the timer loop.
- **Kick CDN resets connections** mid-download (3 resets in one 3h VOD during the spike).
  ffmpeg's HLS demuxer recovered from all of them (output 12,044.4 s vs Kick 12,045 s), so
  the downloader is one `ffmpeg -i <rendition m3u8> -c copy` run, **verified by duration**
  and retried as a whole when the check fails.
- **YouTube Data API v3 quota**: 10,000 units/day; `videos.insert` = 1,600, `thumbnails.set`
  = 50. That is **6 uploads/day**. A stream with 5 parts plus the full VOD is 6 uploads.
  On `quotaExceeded` the job stops cleanly and resumes the next day; it must not mark
  anything failed.
- **Unaudited API project**: uploads are locked private anyway until Google audits the
  project. Matches our private-only rule.
- **Videos over 15 minutes** and **custom thumbnails** need a phone-verified YouTube
  channel. Melek's to do once; the uploader reports a clear error if it is missing.
- **Machine**: Melek's **Windows** laptop (Python 3.12 + ffmpeg via
  `winget install Python.Python.3.12 Gyan.FFmpeg`). Development and the end-to-end test run on
  the Linux VM where T3 runs (Python 3.12, ffmpeg 6.1). All code must work on both:
  `pathlib` everywhere, OS-specific calls only behind an `os.name` check (run lock), ffmpeg found on PATH.
- **Stack (decided)**: Python **stdlib only** + `ffmpeg`/`ffprobe` binaries. No pip deps.
  OAuth, HTTP, multipart upload, SQLite all through stdlib.

## 5. Secrets and config

| What | Where | In Bitwarden as |
|---|---|---|
| Gemini API key | `~/.config/k2y/gemini.key` (0600) | `kick-to-youtube / Gemini API key` |
| Google OAuth client (Desktop) | `~/.config/k2y/client_secret.json` (0600) | `kick-to-youtube / Google OAuth client (YouTube upload)` |
| YouTube refresh token | `~/.config/k2y/youtube_token.json` (0600), written by `k2y auth` | saved after first auth |

Config: `~/.config/k2y/config.json`, all keys optional:
`{"channel": "randomguy42", "work_dir": "~/.local/share/k2y", "model": "gemini-3.8-flash",
"min_part_minutes": 15, "since": "<ISO date>"}`. `since` defaults to the date of the first
`k2y run`, so old VODs are only processed with `k2y backfill <vod_id>`.

## 6. Design

### 6.1 Layout

```
k2y/
  __main__.py   # CLI entry: python3 -m k2y <command>
  cli.py        # argparse commands, calls pipeline + db
  db.py         # SQLite state (schema below)
  kick.py       # list VODs, resolve best rendition, one ffmpeg download + duration check
  segment.py    # audio extract, Gemini Files upload, thematic parts, validation
  media.py      # cut parts, thumbnails, chapters text
  youtube.py    # OAuth (loopback + PKCE), resumable upload, thumbnails.set
  pipeline.py   # one step at a time per VOD, state machine
  config.py     # paths, config.json, secrets loading
tests/          # unittest, stdlib only, no network
```

### 6.2 State (SQLite, `work_dir/k2y.db`)

```sql
CREATE TABLE vods (
  id INTEGER PRIMARY KEY,          -- Kick video id
  title TEXT NOT NULL, started_at TEXT NOT NULL, duration_s INTEGER NOT NULL,
  source_url TEXT NOT NULL, category TEXT,
  state TEXT NOT NULL,             -- see 6.3
  error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
  plan_json TEXT,                  -- validated parts from segment.py
  updated_at TEXT NOT NULL
);
CREATE TABLE uploads (
  vod_id INTEGER NOT NULL REFERENCES vods(id),
  idx INTEGER NOT NULL,            -- 0 = full VOD, 1..n = parts
  title TEXT NOT NULL, start_s INTEGER NOT NULL, end_s INTEGER NOT NULL,
  file TEXT, thumb TEXT,
  youtube_id TEXT,                 -- set once uploaded; never re-upload when set
  upload_session TEXT,             -- resumable upload URL, to resume after a crash
  thumb_done INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (vod_id, idx)
);
```

### 6.3 Pipeline

States: `new → downloaded → planned → cut → uploading → done`, plus `failed` and `skipped`.
`k2y run` (what the timer calls) does, under a lock file (`work_dir/k2y.lock`, skip the run
if held):

1. **Discover.** List Kick VODs; insert unseen ones with `started_at >= since` and
   `is_live == false` as `new`.
2. For each VOD not `done/failed/skipped`, oldest first, advance one state at a time until
   it finishes or an error occurs. Errors: store `error`, `attempts += 1`, stay in the same
   state; after 3 attempts move to `failed`. `quotaExceeded` stops the whole run without
   counting an attempt.
3. Steps:
   - **download** (`kick.py`): best rendition (highest `BANDWIDTH`), one
     `ffmpeg -i <rendition m3u8> -c copy` run to `work_dir/<id>/source.mp4` (write to
     `.part`, rename on success). Check the result: `ffprobe` duration within 1% of
     Kick's `duration`, else error (the step is retried by the pipeline).
   - **plan** (`segment.py`), the design proven by the spike (section 9):
     1. Extract audio (mono, 16 kHz, Opus 24 kbps) and split it into 10-minute chunks
        (`ffmpeg -f segment -segment_time 600 -c copy`).
     2. For each chunk, one Gemini call with the audio **inline** (base64, ~1 MB) returning
        `{summary_en, activity, segments:[{at:"MM:SS", topic}]}` with times **relative to
        the chunk**. Code converts to absolute seconds (`chunk_index*600 + at`, clamped
        inside the chunk). Never send the whole file: whole-file timestamps drift ~3x.
        Run at most 2 chunk calls concurrently.
     3. One text-only merge call over the absolute timeline returns parts
        `{start_s, title, description, summary_en, key_moments:[{at_s, what}]}`; every
        `start_s` and `at_s` must be a timeline `at_s` value (code snaps any other value to
        the nearest one). Prompt rules: cut where the subject changes, not at chunk edges;
        15 to 60 min parts; titles short French or Latin-script darija; descriptions French.
     4. **Validate and repair** in code: sort, first part at 0, clamp to [0, duration],
        `end_s` = next `start_s` (last = duration), merge parts shorter than
        `min_part_minutes` into the shorter neighbour, VODs under 20 min get one part.
        If the merge fails twice, fall back to a single part.
     Models: `config.model` first, then the fallback list `["gemini-3.8-flash",
     "gemini-3.7-flash", "gemini-3.5-flash", "gemini-3-flash-preview"]`, backoff on
     429/500/503, rotating model per retry. A daily-quota 429 (`PerDay` in the quotaId)
     stops the run like YouTube's quotaExceeded (retry next run, no attempt counted).
     Prompts and schemas: start from `docs/spike/segment2.py`.
   - **cut** (`media.py`): `ffmpeg -ss <start> -to <end> -c copy` per part (keyframe
     snapping is acceptable). Thumbnail per upload: frame at the part's first key moment,
     1280x720 JPEG, title text overlaid with `drawtext` (bold, white, black outline,
     bottom third), under 2 MB. If there is only one part, upload only the full VOD.
   - **upload** (`youtube.py`): full VOD first (idx 0), then parts in order. Resumable
     upload; `upload_session` is persisted so a crash resumes the same upload. Then
     `thumbnails.set`. `privacyStatus` is always `private`. Title max 100 chars, description
     max 5000.
   - **done**: delete `work_dir/<id>/` once every upload row has `youtube_id` and
     `thumb_done`.

### 6.4 Video metadata

- Full VOD title: Kick `session_title` + ` | Stream du <DD/MM/YYYY>`.
  Description: part list as YouTube chapters (`00:00 <title>` lines, first line must be
  `00:00`, at least 3 chapters each at least 10 s, otherwise no chapters), then
  `Live sur https://kick.com/randomguy42`.
- Part title: Gemini title + ` | Partie <n>/<total>`. Description: Gemini description,
  the part's key moments as chapters if they qualify, link to the full VOD
  (`https://youtu.be/<id>`) and to Kick.
- Category: Gaming (20) if the Kick category is a game, else People & Blogs (22).
  `defaultLanguage`/`defaultAudioLanguage`: `ar`.

### 6.5 CLI

```
k2y run                    # timer entry: discover + advance everything
k2y status                 # table: vod id, date, title, state, uploads x/y, error
k2y plan <vod_id>          # (re)run the plan step and print the parts, no upload
k2y retry <vod_id>         # clear error/attempts, failed -> previous state
k2y skip <vod_id>          # never process this VOD
k2y backfill <vod_id>      # add an old VOD (before `since`)
k2y auth                   # OAuth loopback flow in the browser, writes the token
k2y install-timer          # Windows: create the 'k2y' Task Scheduler task (schtasks)
k2y auth --paste           # headless auth: paste the redirected URL instead of a local server
```

Exit codes: 0 ok, 1 error, 2 bad usage. `--json` on `status` for agents.

### 6.6 OAuth (stdlib)

Installed-app flow: start `http.server` on `127.0.0.1:<free port>`, open the consent URL
(scope `https://www.googleapis.com/auth/youtube.upload` plus
`https://www.googleapis.com/auth/youtube` for thumbnails) with PKCE (S256) and `state`,
receive the code, exchange it at `https://oauth2.googleapis.com/token`, store the refresh
token. Refresh the access token before each run. Print the URL too, for headless use.

### 6.7 Scheduling and portability

- **Windows (target).** `k2y install-timer` runs
  `schtasks /Create /F /SC MINUTE /MO 10 /TN k2y /TR "\"<pythonw.exe>\" \"<repo>\\k2y-task.pyw\""`.
  `k2y-task.pyw` (repo root, committed) puts its own folder first on `sys.path`, `chdir`s to
  it and calls `k2y.cli.main(["run"])` inside a try/except that logs any crash to `k2y.log`.
  `pythonw.exe` (next to `sys.executable`) has no console, so no window appears and there is
  no batch file or code-page issue. Prints how to check (`schtasks /Query /TN k2y`), remove
  (`schtasks /Delete /TN k2y /F`) and allow running on battery (README). The task runs only
  while Melek is logged in; that is acceptable.
- **Linux (VM, tests).** `install-timer` on Linux prints the equivalent crontab line
  (paths quoted with `shlex.quote`) instead of installing anything.
- **Run lock:** an OS lock held for the life of the process on `work_dir/k2y.lock`:
  `msvcrt.locking(fd, LK_NBLCK, 1)` on Windows, `fcntl.flock(fd, LOCK_EX | LOCK_NB)` elsewhere
  (only the branch for the current OS is imported). The OS releases it when the process dies
  (crash, kill, power loss), so there is no staleness logic, no PID check and no steal.
  Every command that writes state (`run`, `plan`, `retry`, `skip`, `backfill`) takes it.
- **ffmpeg filter paths:** on Windows, drawtext `fontfile`/`textfile` paths must use forward
  slashes and escape the drive colon (`C\:/Windows/Fonts/arialbd.ttf`). Font: DejaVu Sans
  Bold on Linux, Arial Bold (`arialbd.ttf`) on Windows, first one that exists.
- **Logging:** stdlib `logging` to stderr and to `work_dir/k2y.log` (rotating, 5 MB x 3).
- **Headless OAuth:** `k2y auth --paste` prints the consent URL (redirect
  `http://127.0.0.1:8765`), the user approves in any browser, the browser fails to load the
  redirect, and the user pastes that full URL back; the CLI checks `state` and exchanges the
  code with the PKCE verifier.

## 7. Testing

Stdlib `unittest`, no network: plan validation and repair (gaps, overlaps, short parts,
out of range, single part), chapters formatting rules, m3u8 parsing and best-rendition
choice, state machine transitions with fake step functions, quota-exceeded handling.
The end-to-end check is a real run on one VOD (section 8).

## 8. Done

One real VOD from `kick.com/randomguy42` uploaded end to end (full VOD + parts +
thumbnails, all private) by `k2y run`, the review loop clean, PR open.

## 9. Spike results (2026-10-22)

Thematic cuts work. Darija is understood; timestamps are accurate only when audio is sent in
chunks; Gemini free tier (20 requests/day/model) is too small, so **billing must be enabled
on the Gemini key** (about $0.30 per 3h stream). Details: `docs/spike/2026-10-22-thematic-cuts.md`.

## 10. Left to the builder

Internal function names, log format, backoff timings, the drawtext font (any bold font
present on Ubuntu, e.g. DejaVu Sans Bold).
