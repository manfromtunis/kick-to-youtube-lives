<img src="assets/icon.svg" width="64" height="64" alt="">

# k2y

Kick livestream VODs to YouTube, automatically.

## Usage

All commands (`py -m k2y ...`) must be run from the repository root folder (`kick-to-youtube-lives`).

### Windows Setup
1. Install Python 3.12 and FFmpeg:
   ```cmd
   winget install Python.Python.3.12 Gyan.FFmpeg
   ```
2. Clone the repository and enter the clone directory:
   ```cmd
   git clone <repo-url>
   cd kick-to-youtube-lives
   ```
3. Put credentials in `%USERPROFILE%\.config\k2y\` (see `docs/superpowers/specs/2026-10-22-vod-pipeline-design.md` section 5):
   - `gemini.key`: Gemini API key with billing enabled.
   - `client_secret.json`: Google OAuth Desktop client credentials.
   - Optional `config.json`:
     ```json
     {
       "channel": "randomguy42",
       "work_dir": "~/.local/share/k2y",
       "model": "gemini-3.8-flash",
       "min_part_minutes": 15,
       "since": "YYYY-MM-DD",
       "links": [
         ["📸 Instagram", "https://instagram.com/ghmelek"],
         ["⚡ AuraStack", "https://aurastack.ai"],
         ["🎬 Clip2Earn", "https://clip2earn.ai"],
         ["☕ Bakchich Instagram", "https://instagram.com/bakchich.app"]
       ]
     }
     ```
4. Authorize YouTube account:
   ```cmd
   py -m k2y auth
   ```
   For headless machines or environments without a web browser, run:
   ```cmd
   py -m k2y auth --paste
   ```
5. Install automated 10-minute task in Windows Task Scheduler:
   ```cmd
   py -m k2y install-timer
   ```
   This schedules `schtasks` to run `k2y-task.pyw` with `pythonw.exe` directly (no console popups or `.cmd` scripts).

   **Battery condition**: By default, Windows Task Scheduler only runs tasks when connected to AC power. To allow runs while on battery, open **Task Scheduler** (`taskschd.msc`), select **Task Scheduler Library > k2y**, open **Properties > Conditions**, and untick **"Start the task only if the computer is on AC power"** (and untick "Stop if the computer switches to battery power").
6. Check pipeline status:
   ```cmd
   py -m k2y status
   ```

### Reaction Photos
Place reaction photos in `%USERPROFILE%\.config\k2y\faces\` (or `$HOME/.config/k2y/faces/`). Any `.jpg`, `.jpeg`, `.png`, or `.webp` file is supported. The pipeline deterministically selects a photo for each thumbnail and blends it on the right edge with an alpha gradient. If the folder is missing or empty, thumbnails are generated without reaction photos and a warning is logged once per run.

### Daily Use
Run all commands from the `kick-to-youtube-lives` root directory:
- `py -m k2y status` (or `py -m k2y status --json`): view pipeline status, state, upload progress, attempts, and errors.
- `py -m k2y plan <vod_id>`: run download (if needed) and generate thematic segmentation parts preview without uploading.
- `py -m k2y retry <vod_id> [--restart-upload]`: clear errors and reset failed or errored VODs to re-process. Passing `--restart-upload` also clears unfinished upload sessions on rows without a YouTube ID so they can start fresh.
- `py -m k2y set-upload <vod_id> <idx> <youtube_id>`: record that an upload part (0 for full VOD, 1+ for thematic parts) is already on YouTube, clearing any upload session for that part. Refuses if the row does not exist.
- `py -m k2y refresh-metadata <vod_id> | --all`: rebuild video descriptions, tags, and hashtags using the v2 layout and update snippets on YouTube for published VODs in `done` state. For older plans missing hooks/tags, calls Gemini once to backfill metadata into `plan_json`.
- `py -m k2y skip <vod_id>`: mark a VOD as skipped to ignore it (cleans up any existing work directory for that VOD).
- `py -m k2y backfill <vod_id>`: fetch and process an older Kick VOD from before the `since` cutoff.

### Logs
Logs are kept in `%USERPROFILE%\.local\share\k2y\` (or `$HOME/.local/share/k2y` / `work_dir`):
- `k2y.log`: pipeline state machine and execution logs, including any uncaught exceptions from `cli.main` or `k2y-task.pyw` (5 MB max, 3 backups, UTF-8).
- `k2y-task.log`: crontab redirect log on Linux.

### Privacy & Quotas
- **Private-only**: All videos are uploaded strictly with `privacyStatus: private`. There is no option to publish publicly.
- **YouTube Quota**: Limited to ~6 uploads/day (10,000 units/day). If `quotaExceeded` occurs, the run stops cleanly without marking attempts and resumes the next day.
- **Channel Verification (Thumbnails)**: A `ThumbnailsForbidden` stop blocks every run until the channel is phone-verified at https://www.youtube.com/verify. To check if a run is blocked by this, run `py -m k2y status` to view the recorded error.
- **Gemini Billing**: Gemini API requires billing enabled to support inline multi-chunk audio processing for long streams without hitting free tier daily limits.
