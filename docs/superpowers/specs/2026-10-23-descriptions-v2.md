# Descriptions v2 (2026-10-23)

Melek reviewed a mock-up comparing the current and proposed descriptions on 2026-10-23 and
approved it: first person ("je"), hashtags yes, plus the links below.

## Problems with v1

The description talks about Melek in the 3rd person ("le streamer"), and its first 2 lines
(the only ones shown in search) don't hook. The full VOD has no summary at all. Nothing links
the videos of one live together. There are no tags and no hashtags.

## Layout

Full VOD and parts use the same blocks, in this order, separated by one blank line.

1. **Hook:** 1 or 2 sentences in French, first person, at most 200 characters, saying what
   the viewer gets. Example: "Combien de RAM pour faire tourner DeepSeek chez soi ? Je fais
   le calcul en live, puis je réagis à la vidéo de Primeagen sur le marché noir des tokens IA
   et les faux KYC."
2. **Chapters:** `⏱️ Chapitres`, then the existing chapter lines (unchanged logic and rules).
   Leave the block out when there are no valid chapters.
3. **Navigation** (only when the live has more than one part):
   - In a part: `📺 Les autres parties de ce live`, one line per *other* part
     (`2/5 <part title> https://youtu.be/<id>`), then
     `▶️ Le stream complet (3h20) : https://youtu.be/<full id>`.
   - In the full VOD: `📺 Ce live découpé en N vidéos`, one line per part.
4. **Links**, from config (defaults below), one per line:
   ```
   🔴 En live sur Kick : https://kick.com/randomguy42
   📸 Instagram : https://www.instagram.com/ghmelek/
   🛠️ AuraStack : https://aurastack.link/
   💸 Clip2Earn : https://www.clip2earn.money/
   📱 Bakchich : https://www.instagram.com/bakchich.app/
   ```
   The Kick line uses `cfg.channel`. The others come from a new `Config.links` field, a list
   of `[label, url]` pairs, so Melek can change them in `config.json`.
5. **Hashtags:** 3 or 4 on one line, for example `#IA #DeepSeek #LLM #Tunisie`.

Also set `snippet.tags`: 5 to 10 lowercase tags, at most 500 characters in total (YouTube
limit).

Keep the existing limits: no `<` or `>`, at most 5,000 UTF-8 bytes. When the text is too
long, drop navigation lines from the end first, then shorten the hook. Never drop the
chapters or the links.

## Gemini

The merge schema gets, per part: `hook` (string), `tags` (array of strings) and `hashtags`
(array of strings). At the top level: `vod_hook`, `vod_tags` and `vod_hashtags`. These live
next to `thumb_text`/`vod_thumb_text` from the thumbnails v2 change. Prompt rules: French,
first person as Melek, no "le streamer"; hashtags start with `#` and have no spaces.
Code sanitises everything (strip, remove `<>`, deduplicate tags, prefix a missing `#`).

**Fallback when a field is missing** (old plans): the hook is the part's existing
`description` field. Tags and hashtags are left out.

## Final description pass

Each part's navigation needs the YouTube ids of all parts, but parts are uploaded one by
one. So after the last upload of a VOD succeeds and before the VOD moves to `done`, k2y
rebuilds every description of that VOD and sends `videos.update` (part=snippet: title,
description, tags, categoryId, defaultLanguage) for each upload. That is 50 quota units per
video, about 300 per live. Record each success (for example a `desc_done` column on
`uploads`) so a crash or quota stop resumes without redoing finished ones. A quota error
stops the run like other YouTube quota errors (StopRun, no attempt counted).

## Updating published videos

New CLI command `k2y refresh-metadata <vod_id> | --all`: for VODs in `done`, rebuild the
descriptions from the stored plan and run the same final pass. When the plan lacks the new
fields, first make one text-only Gemini call per VOD that fills `hook`/`tags`/`hashtags` from
the stored part titles, descriptions and key moments. Save the result into `plan_json` so
the call isn't repeated. It takes the run lock like the other commands that write state.
Thumbnails are not refreshed here, because the source video is already deleted.

## Out of scope

Playlists, pinned comments, end screens, translated descriptions.
