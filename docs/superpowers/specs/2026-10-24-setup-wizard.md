# Setup Wizard Spec

## Goal
Provide a friendly first-time setup for a friend: run `py -m k2y setup` or double-click `setup.cmd` (Windows) / `./setup.sh` (Linux/macOS), and the wizard collects credentials, writes configuration, authorizes YouTube and installs the 10-minute timer. It must be re-runnable, preserve unknown keys in `config.json`, and upload nothing.

## Requirements
- CLI subcommand `setup` added to `k2y.cli`; dispatched before config loading. Flags: `--config-dir`, `--no-auth`, `--no-timer`.
- `k2y/wizard.py`: injectable dependencies for tests (`ask`, `secret_ask`, `out`, `auth_fn`, `install_timer_fn`, `check_channel_fn`, `run`/command runner, `which`).
- Prerequisites: Python >= 3.12; `ffmpeg`/`ffprobe`. On Windows with `winget`, offer to install FFmpeg (Gyan.FFmpeg); if installed mid-run PATH may need new terminal.
- Config dir defaults to `~/.config/k2y` (`%USERPROFILE%\.config\k2y\` on Windows); `config.CONFIG_DIR` patched during wizard and restored.
- Credentials:
  - `client_secret.json` (OAuth Desktop app). Must contain `installed`. Validate JSON and shape; accept path input.
  - `gemini.key` (hidden input or path to file). Basic validation (length/whitespace). Permissions `0o600` on POSIX.
- Configuration:
  - Kick channel: normalize `https://kick.com/slug`, `@slug`, paths, case; check reachability via `kick.list_vods(slug)` with option to override if network fails.
  - `since` (YYYY-MM-DD, default today), `work_dir` (must be writable), `min_part_minutes` (>=1), `links` `[[label, url], ...]` with `http(s)` validation; skip empty list.
  - Merge into existing `config.json` preserving unknown keys.
- YouTube: if token exists with `refresh_token`, offer to re-authorize. Else run auth; support headless `--paste` via question. Return actionable error message on failure.
- Timer: delegate to `k2y.timer.install_timer(repo_dir, work_dir)`. Windows creates Task Scheduler task `k2y` every 10 min; non-Windows prints crontab line and notes manual install.
- Exit codes: 0 success, 1 failure, 130 cancelled (Ctrl+C/EOF).
- Friendly output, no secrets printed.

## Files
- New: `k2y/wizard.py`, `setup.cmd`, `setup.sh`, `tests/test_wizard.py`, `docs/superpowers/specs/2026-10-24-setup-wizard.md`, `LICENSE`
- Modified: `k2y/cli.py`, `k2y/config.py` (secret_path/read/write accept config_dir; error message updated), `k2y/timer.py` (injectable run, callable default), `tests/test_cli.py`, `README.md`, `docs/SETUP.md`

## Testing
- Unit tests cover normalization, validation, preservation of keys, full happy paths, flags, token reuse, errors, cancellation, custom config dir, permissions, CLI dispatch before config load.
- All existing tests updated to use `k2y.timer.*` patches.
