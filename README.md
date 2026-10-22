<img src="assets/icon.svg" width="64" height="64" alt="">

# k2y

Kick livestream VODs to YouTube, automatically.

## Usage

### Setup
1. Place credentials in `~/.config/k2y/` (see `docs/superpowers/specs/2026-10-22-vod-pipeline-design.md` section 5):
   - `gemini.key` (`chmod 600`): Gemini API key with billing enabled.
   - `client_secret.json` (`chmod 600`): Google OAuth Desktop client credentials.
   - Optional `config.json`: `{"channel": "randomguy42", "work_dir": "~/.local/share/k2y", "model": "gemini-3.8-flash", "min_part_minutes": 15, "since": "YYYY-MM-DD"}`.
2. Authorize YouTube account:
   ```bash
   python3 -m k2y auth
   ```
3. Install and enable the systemd user timer:
   ```bash
   python3 -m k2y install-timer
   ```

### Daily Use
- `python3 -m k2y status` (or `python3 -m k2y status --json`): view pipeline status, state, upload progress, attempts, and errors.
- `python3 -m k2y plan <vod_id>`: run download (if needed) and generate thematic segmentation parts preview without uploading.
- `python3 -m k2y retry <vod_id>`: clear errors and reset failed or errored VODs to re-process.
- `python3 -m k2y skip <vod_id>`: mark a VOD as skipped to ignore it.
- `python3 -m k2y backfill <vod_id>`: fetch and process an older Kick VOD from before the `since` cutoff.

### Logs
Monitor automated background runs via journald:
```bash
journalctl --user -u k2y -f
```

### Privacy & Quotas
- **Private-only**: All videos are uploaded strictly with `privacyStatus: private`. There is no option to publish publicly.
- **YouTube Quota**: Limited to ~6 uploads/day (10,000 units/day). If `quotaExceeded` occurs, the run stops cleanly without marking attempts and resumes the next day.
- **Gemini Billing**: Gemini API requires billing enabled to support inline multi-chunk audio processing for long streams without hitting free tier daily limits.
