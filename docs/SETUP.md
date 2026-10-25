# k2y — full setup guide

This guide takes a new machine from zero to "my Kick streams appear on YouTube by themselves".
It takes about **30 minutes**, most of it in the Google Cloud console. No coding is required.

The examples use Windows PowerShell, because Windows is the primary target. Linux differences
are called out in each step. macOS is untested: the pipeline should run, but thumbnails may fall
back to ffmpeg's default font.

---

## What you are setting up

k2y runs every 10 minutes on your computer. When it sees a finished Kick stream it:

1. downloads the best available quality,
2. asks Gemini where the subject changes and builds thematic parts,
3. cuts the parts and draws a thumbnail for each,
4. uploads the full stream and every part to YouTube as **private**.

You publish manually in YouTube Studio when you are happy with them. k2y never publishes.

**Before you start, make sure you have:**

- [ ] A Kick channel you stream on.
- [ ] A YouTube channel you own.
- [ ] A Google account (the one that owns the YouTube channel).
- [ ] A Gemini API key with billing enabled.
- [ ] About 20 GB of free disk space.
- [ ] A computer that is on and awake for a few hours after each stream.

---

## Step 1 — Verify your YouTube channel first

Two things k2y needs are locked behind YouTube verification:

- uploading videos **longer than 15 minutes**;
- setting a **custom thumbnail**.

A livestream VOD is almost always longer than 15 minutes, so without this step the first run
will fail.

1. Open <https://www.youtube.com/verify>.
2. Verify with your phone number.
3. Wait up to 24 hours for the feature to activate on a new channel.

You can confirm later: YouTube Studio → **Settings → Channel → Feature eligibility** should show
"Intermediate features" enabled.

---

## Step 2 — Install the tools

### Windows

Open **PowerShell** (not the old Command Prompt) and run:

```powershell
winget install Git.Git Python.Python.3.12 Gyan.FFmpeg
```

Close and reopen PowerShell afterwards so the new `PATH` is loaded. Then check:

```powershell
py --version
ffmpeg -version
ffprobe -version
git --version
```

`py --version` must print **3.12** or newer. If `ffmpeg` or `ffprobe` is "not recognized",
reopen the terminal; if it still fails, add `%LOCALAPPDATA%\Microsoft\WinGet\Links` to your
`PATH` (Windows Settings → System → About → Advanced system settings → Environment Variables).

### Linux (Ubuntu / Debian)

```bash
sudo apt update
sudo apt install -y git python3 python3-venv ffmpeg
python3 --version
ffmpeg -version
```

k2y needs Python 3.12 or newer. Ubuntu 24.04 ships 3.12. On an older release, install a newer
Python before continuing.

### macOS (untested)

```bash
brew install git python@3.12 ffmpeg
```

---

## Step 3 — Get the code

```powershell
cd $env:USERPROFILE
git clone https://github.com/manfromtunis/kick-to-youtube-lives.git
cd kick-to-youtube-lives
```

On Linux:

```bash
cd ~
git clone https://github.com/manfromtunis/kick-to-youtube-lives.git
cd kick-to-youtube-lives
```

Every k2y command must be run from this folder. If you see
`No module named k2y`, you are in the wrong directory.

> The repository is private. Accept the GitHub invitation first, or ask the owner to make it
> public.

---

## Fast path — let the wizard do Steps 6 to 9

Once Step 1 is done and you have the two credentials from Steps 4 and 5, the wizard can write
`config.json`, authorize YouTube and install the timer for you:

- **Windows:** double-click **`setup.cmd`** in the repository folder.
- **Linux / macOS:** run `./setup.sh` from the repository folder.
- **Any terminal:** `py -m k2y setup` (or `python3 -m k2y setup`).

It asks for:

1. the path to the `client_secret.json` you downloaded in Step 4;
2. your Gemini API key (input hidden; you can also paste a path to a file containing it) — see
   [Step 5](#step-5--create-a-gemini-api-key) for billing and how to create one;
3. your Kick channel slug, the start date, the work folder, the minimum part length and
   optional description links;
4. whether to authorize YouTube now and whether to install the 10-minute timer.

It checks Python and FFmpeg, keeps unknown keys in an existing `config.json`, uploads nothing,
and can be re-run safely. Use `--config-dir PATH` for another config folder, `--no-auth` or
`--no-timer` to skip those steps. Then continue with **Step 8 — Test on one VOD**.

If you prefer to see every file it writes, follow Steps 6 to 9 manually below. The wizard only
automates the same steps.

---

## Step 4 — Create the Google Cloud credentials (the long part)

k2y uploads with the YouTube Data API. You need your own OAuth client. This is free.

### 4.1 Create a project

1. Open <https://console.cloud.google.com/>.
2. At the top, click the project selector → **New project**.
3. Name it `k2y` (or anything), then **Create**.
4. Make sure the new project is selected in the top bar.

### 4.2 Enable the YouTube Data API v3

1. Go to **APIs & Services → Library**.
2. Search for **YouTube Data API v3**.
3. Open it and click **Enable**.

### 4.3 Configure the OAuth consent screen

Google renamed this page to **Google Auth Platform** in newer consoles. The fields are the same.

1. Go to **APIs & Services → OAuth consent screen**
   (or search "Google Auth Platform" in the top bar).
2. If you see a **Get started** button, click it and fill in:
   - **App name:** `k2y`
   - **User support email:** your email
   - **Audience:** **External**
   - **Contact email:** your email
   - Accept the policy and click **Create**.
3. Open **Audience** (left menu).
   - Keep the publishing status on **Testing** for now. Step 7 explains the 7-day consequence.
   - Under **Test users**, click **Add users** and add the Google account that owns the YouTube
     channel.
4. Open **Data Access** (left menu) → **Add or remove scopes**. Add these two if the page asks
   you to choose scopes:
   - `https://www.googleapis.com/auth/youtube.upload`
   - `https://www.googleapis.com/auth/youtube`

   If the page does not offer scope selection, skip this; k2y requests the scopes during
   authorization anyway.

### 4.4 Create the OAuth client

1. Go to **APIs & Services → Credentials**
   (or **Google Auth Platform → Clients**).
2. Click **Create credentials → OAuth client ID**.
3. Application type: **Desktop app**.
4. Name: `k2y desktop`, then **Create**.
5. Click **Download JSON** in the dialog (or the download icon in the client list).

The file is named something like `client_secret_1234567890-abc.apps.googleusercontent.com.json`.

### 4.5 Put the client secret where k2y expects it

k2y reads exactly this path:

- Windows: `%USERPROFILE%\.config\k2y\client_secret.json`
- Linux/macOS: `~/.config/k2y/client_secret.json`

Create the folder:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.config\k2y" | Out-Null
```

Then move and rename the downloaded file. In File Explorer, rename it to `client_secret.json`
and move it into that folder. With PowerShell:

```powershell
Move-Item "$env:USERPROFILE\Downloads\client_secret_*.json" `
  "$env:USERPROFILE\.config\k2y\client_secret.json"
```

Linux:

```bash
mkdir -p ~/.config/k2y
mv ~/Downloads/client_secret_*.json ~/.config/k2y/client_secret.json
```

The file is a credential: do not commit it, paste it in a chat, or put it inside the repository.

---

## Step 5 — Create a Gemini API key

k2y uses Gemini to understand the stream and find the thematic cuts. Long streams are sent in
10-minute audio chunks, so the key must have **billing enabled**; the free tier is too small.

### 5.1 Get the key

1. Open <https://aistudio.google.com/apikey> and sign in with a Google account.
2. Click **Create API key** (top right).
3. In the dialog, pick a Google Cloud project. You can use the same project as Step 4, or
   let Google AI Studio create a new one.
4. Copy the key that appears. Treat it like a password.

### 5.2 Enable billing (required)

If billing is not set up for that project, Google will block requests for long audio or show a
quota error.

1. Open <https://console.cloud.google.com/billing>.
2. Click **Link a billing account** and attach a card.
3. Go to <https://console.cloud.google.com/apis/api/generativelanguage.googleapis.com>
   and make sure the **Generative Language API** is enabled for that project.
4. If AI Studio said "Set up billing", complete that flow before running k2y.

### 5.3 Save it as `gemini.key`

The fastest way: run the wizard (`setup.cmd` / `setup.sh` / `py -m k2y setup`) and paste the
key when asked. To save by hand:

**Windows (PowerShell):**

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.config\k2y" | Out-Null
notepad "$env:USERPROFILE\.config\k2y\gemini.key"
```

Paste only the key, save and close Notepad. Remove spaces and line breaks.

**Linux / macOS:**

```bash
mkdir -p ~/.config/k2y
nano ~/.config/k2y/gemini.key
```

Paste only the key and save (`Ctrl+X` in nano).

**Tips:**
- The file must be named exactly `gemini.key` with no extension like `.txt`.
- If you create it from a text editor that adds a BOM or extra quotes, the wizard/manual run
  will fail — paste the raw key only.
- If you get `API_KEY_INVALID`, regenerate the key in AI Studio and replace the file.
- If you get quota/billing errors, confirm billing is linked to the correct project.

**Cost:** roughly **$0.30 per 3-hour stream** at the time of writing. Check current Gemini
pricing; a 6-hour stream costs more because there are more 10-minute chunks.

---

## Step 6 — Write the configuration file

The fastest way is the wizard (see "Fast path"). To do it by hand: k2y needs to know which
Kick channel to watch and which links to put in descriptions.

Create `%USERPROFILE%\.config\k2y\config.json` (Linux: `~/.config/k2y/config.json`):

```powershell
notepad "$env:USERPROFILE\.config\k2y\config.json"
```

Paste this and adapt every value:

```json
{
  "channel": "your_kick_slug",
  "work_dir": "C:/Users/YOU/.local/share/k2y",
  "model": "gemini-3.8-flash",
  "min_part_minutes": 15,
  "since": "2026-10-24",
  "links": [
    ["Instagram", "https://instagram.com/yourhandle"],
    ["Discord", "https://discord.gg/yourserver"]
  ]
}
```

| Key | What it means |
|---|---|
| `channel` | Your Kick **slug**: the last part of `kick.com/<slug>`. This is required. |
| `work_dir` | Where downloads, the database and logs live. Default: `~/.local/share/k2y`. On Windows use forward slashes or escaped backslashes. |
| `model` | Gemini model. Leave `gemini-3.8-flash` unless you know you need another. |
| `min_part_minutes` | Parts shorter than this are merged into a neighbour. |
| `since` | Only VODs started on/after this date are picked up automatically. Use `YYYY-MM-DD`. If you omit it, k2y writes today's date on the first run. Older VODs need `backfill`. |
| `links` | Lines added at the bottom of every description. Set your own, or `[]` for none. |

> **Do not skip this step.** The built-in defaults point at the original author's channel and
> social links. If you run without `config.json`, k2y will watch the wrong channel and put
> someone else's links in your descriptions.

**Windows path tip:** `"C:/Users/YOU/.local/share/k2y"` works fine. `YOU` is your Windows
username. You can also omit `work_dir` to use the default under your home folder.

---

## Step 7 — Authorize YouTube

From the repository folder:

```powershell
py -m k2y auth
```

1. A browser opens on Google's consent screen.
2. Choose the Google account that owns the YouTube channel.
   **If your channel is a Brand Account**, pick that channel/account in the chooser. Choosing
   the personal account uploads to the personal channel instead.
3. Google shows "Google hasn't verified this app" because the app is yours and unpublished.
   Click **Advanced → Go to k2y (unsafe)**. This is expected; you are the developer.
4. Approve the two YouTube permissions.
5. The browser shows **"k2y: you can close this tab"**. The token is saved to
   `~/.config/k2y/youtube_token.json`.

If the browser does not open, copy the printed URL into any browser. On a headless machine:

```powershell
py -m k2y auth --paste
```

Approve in a browser on another machine, then paste the full `http://127.0.0.1:8765/?...`
address back into the terminal.

### The 7-day catch

While the Google Cloud app stays in **Testing** mode, Google expires the refresh token after
**7 days**. When that happens, runs stop with:

```
YouTube authorization expired or missing: run `py -m k2y auth`
```

Just run `py -m k2y auth` again. If you do not want to re-authorize weekly, open
**Google Auth Platform → Audience** and click **Publish app**, then complete Google's
verification for the YouTube scopes. For a personal tool, re-authorizing weekly is usually
simpler.

---

## Step 8 — Test on one VOD before scheduling

### 8.1 Find a VOD ID

Kick's site does not always show the numeric VOD ID in the page URL. The reliable way:

1. Open `https://kick.com/api/v2/channels/<your_slug>/videos` in your browser.
2. Find the stream you want (search for its title).
3. Copy its `"id"` value (a number such as `133658767`).

### 8.2 Add it and plan it (no upload)

```powershell
py -m k2y backfill 133658767
py -m k2y plan 133658767
```

`plan` downloads the VOD, sends the audio to Gemini, and prints the proposed parts. Nothing is
uploaded. This is the step that can take a while: a 3-hour stream means a multi-GB download plus
several Gemini calls. Expect **20 to 60 minutes** the first time.

Check that the parts and titles make sense. If they do not, you can run `plan` again; it
re-plans.

### 8.3 Run the real pipeline

```powershell
py -m k2y run
py -m k2y status
```

`run` advances every unfinished VOD as far as it can. Watch `status` until the VOD shows
`state = done` and `uploads = N/N`.

Then open <https://studio.youtube.com> → **Content**. Every video must show **Private**. Check:

- the full stream and each part exist;
- each has a thumbnail;
- the description has chapters, links and the Kick link;
- the titles are in French.

Publish them manually when you are ready.

### 8.4 If something fails

```powershell
py -m k2y status
```

The `Error` column gives the reason. Logs are in the work directory:

- Windows: `%USERPROFILE%\.local\share\k2y\k2y.log`
- Linux: `~/.local/share/k2y/k2y.log`

See [Troubleshooting](#troubleshooting) below. To retry after fixing the cause:

```powershell
py -m k2y retry 133658767
```

---

## Step 9 — Schedule it

### Windows

```powershell
py -m k2y install-timer
```

This creates a Task Scheduler task named `k2y` that runs `k2y-task.pyw` every 10 minutes with
`pythonw.exe`, so no console window appears.

Check it:

```powershell
schtasks /Query /TN k2y
```

**Battery:** by default Windows only runs the task on AC power. To allow runs on battery:

1. Open **Task Scheduler** (`taskschd.msc`).
2. **Task Scheduler Library → k2y → Properties → Conditions**.
3. Untick **"Start the task only if the computer is on AC power"**.
4. Untick **"Stop if the computer switches to battery power"**.

The task runs only while you are logged in. If the laptop sleeps, the next wake triggers the
next scheduled run; k2y resumes where it stopped.

### Linux

`install-timer` prints the crontab line instead of installing it:

```bash
python3 -m k2y install-timer
```

Copy the printed line into `crontab -e`. It looks like:

```
*/10 * * * * cd '/home/you/kick-to-youtube-lives' && '/usr/bin/python3' -m k2y run >> '/home/you/.local/share/k2y/k2y-task.log' 2>&1
```

If `ffmpeg` is not in cron's minimal `PATH`, add `PATH=/usr/local/bin:/usr/bin:/bin` at the top
of the crontab.

---

## Step 10 — Daily use

| Situation | Command |
|---|---|
| See what happened | `py -m k2y status` |
| A run failed | `py -m k2y retry <vod_id>` |
| An upload session is stuck | `py -m k2y retry <vod_id> --restart-upload` |
| Do not process a VOD | `py -m k2y skip <vod_id>` |
| Process an old stream | `py -m k2y backfill <vod_id>` then wait, or `py -m k2y run` |
| Fix descriptions/tags on published videos | `py -m k2y refresh-metadata <vod_id>` or `--all` |
| Re-authorize YouTube | `py -m k2y auth` |
| Preview the cuts without uploading | `py -m k2y plan <vod_id>` |

`status` states:

| State | Meaning |
|---|---|
| `new` | Discovered, not downloaded yet. |
| `downloaded` | File on disk and duration checked. |
| `planned` | Gemini produced the thematic parts. |
| `cut` | Parts and thumbnails are ready. |
| `uploading` | Some videos are on YouTube, the rest are in progress. |
| `done` | Everything is uploaded and the local files were deleted. |
| `failed` | Three attempts failed. Fix the cause, then `retry`. |
| `skipped` | Ignored on purpose. |

---

## Troubleshooting

| Message / symptom | Cause and fix |
|---|---|
| `missing secret ...gemini.key` | Create `~/.config/k2y/gemini.key` with the key (Step 5). |
| `missing secret ...client_secret.json` | Download the **Desktop app** OAuth client and rename it (Step 4.5). |
| `YouTube authorization expired or missing` | Run `py -m k2y auth`. In Testing mode this happens every 7 days. |
| `custom thumbnails need a phone-verified channel` | Verify the channel at <https://www.youtube.com/verify> (Step 1). Runs stop until fixed. |
| Upload rejected because the video is too long | Same verification. Videos over 15 minutes need it. |
| `quotaExceeded` / `uploadLimitExceeded` / `dailyLimitExceeded` | YouTube's 10,000-unit daily quota is used up. Wait until midnight **Pacific Time**; the next run resumes automatically. Nothing is lost. |
| `a run is in progress` | Another `k2y run` (often the timer) holds the lock. Wait for it to finish. |
| Kick discovery error / HTTP 403 | Wrong `channel` slug, the channel has no public VODs, or Kick changed its API. Check the slug in a browser first. |
| `ffmpeg` / `ffprobe` not found | Not on `PATH`. Reopen the terminal, or fix `PATH` (Step 2). On Linux with cron, set `PATH` in the crontab. |
| Gemini 429 / daily quota | Billing not enabled on the key's project, or the model quota is exhausted. Fix billing; the run retries later. A per-day quota stops the run cleanly. |
| New streams are ignored | They started before `since`. Use `backfill` or lower `since`. |
| `VOD ... not found in channel` | The ID does not belong to your channel, or Kick's list changed. Recheck the ID. |
| Timer never runs | `schtasks /Query /TN k2y`, check the battery condition, and make sure you are logged in. |
| Videos are on the wrong YouTube channel | The token belongs to another account or to the personal channel instead of the Brand Account. Run `py -m k2y auth` again and pick the right one. |
| Thumbnail has no text on macOS | macOS fonts are untested. Add a DejaVu/Arial font path, or run on Windows/Linux. |
| Disk fills up | A long stream needs ~20 GB while processing; files are deleted after `done`. Free space or lower `since`. |

If a message is not here, open `k2y.log`; every pipeline error and traceback is written there.

---

## Security

- Secrets live in `~/.config/k2y/`, never in the repository. Keep that folder out of backups you
  share.
- `youtube_token.json` is written with owner-only permissions (`0600`). Do not copy it to another
  machine; run `auth` there instead.
- If the Gemini key leaks, delete it in AI Studio and create a new one. If the OAuth client
  leaks, delete it in Google Cloud → Credentials.
- To revoke k2y's access to your YouTube account:
  <https://myaccount.google.com/permissions>.
- Uploads are always private. There is no setting to change that.

---

## Updating k2y

```powershell
cd $env:USERPROFILE\kick-to-youtube-lives
git pull
```

The timer uses the repository code directly, so the next run picks up the update. Your config,
token and database are outside the repo and are not touched.

Check for breaking changes in the repository's releases/commits before pulling on a busy day.

---

## Uninstalling

1. Remove the schedule:
   - Windows: `schtasks /Delete /TN k2y /F`
   - Linux: delete the k2y line from `crontab -e`.
2. Delete the work directory (`~/.local/share/k2y` or your `work_dir`): database, logs,
   downloads.
3. Delete `~/.config/k2y`: credentials and token.
4. Delete the repository folder.
5. Optional: revoke access at <https://myaccount.google.com/permissions> and delete the Google
   Cloud project.

---

## FAQ

**Does k2y publish my videos?**
No. Every upload is `private`. You publish in YouTube Studio.

**Can I use it for someone else's Kick channel?**
Technically yes if the VODs are public, but only upload content you own. The YouTube account is
the one you authorize.

**Can I run two channels on one machine?**
One configuration is one Kick channel plus one YouTube channel. For a second pair, use a
separate checkout with `K2Y_CONFIG_DIR` pointing at a different config folder and a different
`work_dir`. This is advanced and not covered by the timer installer; use two scheduled tasks.

**Does my PC need to stay on?**
Yes. k2y runs locally. The timer only fires while you are logged in and the machine is awake.
A sleeping laptop simply runs later.

**What does a 3-hour stream cost?**
About $0.30 in Gemini usage, plus your normal electricity/bandwidth. YouTube uploads are free
within quota.

**Why are videos private even though my API project is verified?**
By design. k2y has no publish path. This is a safety guarantee, not a limitation.

**Can I edit a description after k2y uploads it?**
Yes, in YouTube Studio. `refresh-metadata` will overwrite it with the generated version if you
run it again for that VOD.

**Does it upload Shorts or vertical clips?**
No. That is not implemented.

**Where are my files while it works?**
`work_dir/<vod_id>/` (`source.mp4`, parts, thumbnails). They are deleted once every upload is
`done`.

**Can I run it on a server?**
The code works on Linux, but the design assumes a desktop that is on after streams. On a
headless server, use `py -m k2y auth --paste` and cron. Make sure the server has disk space and
outbound HTTPS.
