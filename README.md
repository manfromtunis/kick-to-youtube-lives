<img src="assets/icon.svg" width="64" height="64" alt="">

# k2y

Kick livestream VODs to YouTube, automatically.

## Usage

### Windows Setup
1. Install Python 3.12 and FFmpeg:
   ```cmd
   winget install Python.Python.3.12 Gyan.FFmpeg
   ```
2. Clone the repository:
   ```cmd
   git clone <repo-url>
   cd k2y
   ```
3. Put credentials in `%USERPROFILE%\.config\k2y\` (see `docs/superpowers/specs/2026-10-22-vod-pipeline-design.md` section 5):
   - `gemini.key`: Gemini API key with billing enabled.
   - `client_secret.json`: Google OAuth Desktop client credentials.
   - Optional `config.json`: `{"channel": "randomguy42", "work_dir": "~/.local/share/k2y", "model": "gemini-3.8-flash", "min_part_minutes": 15, "since": "YYYY-MM-DD"}`.
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
6. Check pipeline status:
   ```cmd
   py -m k2y status
   ```

### Daily Use
- `py -m k2y status` (or `py -m k2y status --json`): view pipeline status, state, upload progress, attempts, and errors.
- `py -m k2y plan <vod_id>`: run download (if needed) and generate thematic segmentation parts preview without uploading.
- `py -m k2y retry <vod_id>`: clear errors and reset failed or errored VODs to re-process.
- `py -m k2y skip <vod_id>`: mark a VOD as skipped to ignore it.
- `py -m k2y backfill <vod_id>`: fetch and process an older Kick VOD from before the `since` cutoff.

### Logs
Logs are written to `%USERPROFILE%\.local\share\k2y\k2y.log` (RotatingFileHandler, 5 MB, 3 backups).

### Privacy & Quotas
- **Private-only**: All videos are uploaded strictly with `privacyStatus: private`. There is no option to publish publicly.
- **YouTube Quota**: Limited to ~6 uploads/day (10,000 units/day). If `quotaExceeded` occurs, the run stops cleanly without marking attempts and resumes the next day.
- **Gemini Billing**: Gemini API requires billing enabled to support inline multi-chunk audio processing for long streams without hitting free tier daily limits.
