# Spike: thematic cuts on a Tunisian darija stream (2026-10-22)

Question: can Gemini cut a Kick VOD where Melek speaks Tunisian darija into thematic parts
with usable titles and accurate timestamps?

Answer: **yes, if the audio is sent in chunks.** Script: `docs/spike/segment2.py`.
Result: `docs/spike/2026-10-22-parts-133658767.json`.

## Input

VOD 133658767, "sick stream, malla jaw, tinkering with AI", 3:20:44. Downloaded at 360p
(950 MB) for the spike, audio extracted as mono 16 kHz Opus 24 kbps (34 MB).
Screen frames every 2 minutes were used as ground truth.

## Findings

1. **Darija is understood.** Self-reported confidence 0.92 to 0.95, and the topics match the
   frames (Bakchich clipping campaign, BotFather, multi-agent setup, DeepSeek reaction,
   Primeagen video, browser fingerprints article, Skate clone demo, giveaway).
2. **Whole-file timestamps are wrong.** Sending the full 3h20 audio in one request
   (300,900 audio tokens, 47 s) returned parts ending at 1:01:28: time compressed ~3.3x.
   Early times were right, later ones drifted. Not usable.
3. **Chunked timestamps are right.** 20-minute chunks, each described with times relative
   to the chunk, made absolute by our code: every checked event matched the frames within
   about 2 minutes (Botrix 1:42, Primeagen 1:48, fingerprints article 1:58, Skate 3:00,
   giveaway 3:13).
4. **The merge step needs a good model and strict rules.** `flash-lite` cut exactly on
   chunk edges and produced a 44 s outro part. `gemini-3-flash-preview` with the rules
   "cut where the subject changes, not at chunk boundaries; never under 15 min" produced
   5 coherent parts of ~40 min. It still prefers chunk edges; code-side validation and
   smaller chunks are the mitigation.
5. **Screen frames alone are weak** for this kind of stream (mostly a browser), so audio
   leads. Frames stay useful for thumbnails.
6. **Gemini free tier is 20 requests/day/model** and the 3.x Flash models return 503 under
   load. A stream needs ~20 requests with 10-minute chunks. **Billing must be enabled** on
   the key (cost estimate: ~300k audio tokens per 3h stream, well under $1). The code needs
   a model fallback list and backoff on 429/500/503.
7. **Kick download is simple.** The unofficial API returns the HLS master URL; ffmpeg
   recovered from 3 connection resets and the output duration matched (12,044.4 s vs
   12,045 s).

## Spike output (merge by gemini-3-flash-preview)

| Part | Title |
|---|---|
| 0:00:00 to 0:40:00 | Les contrats CDI & Plan de clipping |
| 0:40:00 to 1:20:00 | Automating Dev avec les agents AI |
| 1:20:00 to 2:00:33 | DeepSeek local & Marché noir AI |
| 2:00:33 to 2:40:00 | Fraude KYC & Baqshish Design System |
| 2:40:00 to 3:20:44 | Claude Projects & Giveaway Final |
