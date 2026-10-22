# Spike v2: chunked. Gemini describes each 10-min chunk (relative times, accurate),
# our code makes times absolute, then one text-only call merges chunks into parts.
# Usage: python3 -I segment2.py <audio.ogg> <out.json>
import base64, concurrent.futures as cf, json, pathlib, subprocess, sys, tempfile, time, urllib.error, urllib.request

import os
MODELS = os.environ.get("K2Y_MODELS", "gemini-3.8-flash,gemini-3.7-flash,gemini-3.5-flash").split(",")
KEY = (pathlib.Path.home() / ".config/k2y/gemini.key").read_text().strip()
BASE = "https://generativelanguage.googleapis.com/v1beta"
CHUNK = int(os.environ.get("K2Y_CHUNK", "600"))
TS = {"type": "STRING"}


def generate(parts, schema):
    body = json.dumps({"contents": [{"parts": parts}], "generationConfig": {
        "responseMimeType": "application/json", "responseSchema": schema}}).encode()
    for attempt in range(8):
        model = MODELS[attempt % len(MODELS)]
        try:
            r = urllib.request.Request(f"{BASE}/models/{model}:generateContent", body,
                                       {"x-goog-api-key": KEY, "Content-Type": "application/json"})
            with urllib.request.urlopen(r, timeout=600) as resp:
                d = json.loads(resp.read())
            return json.loads(d["candidates"][0]["content"]["parts"][0]["text"]), model, d.get("usageMetadata", {})
        except urllib.error.HTTPError as e:
            print(model, e.code, e.read()[:160], file=sys.stderr)
            if e.code not in (429, 500, 503):
                raise RuntimeError(f"{e.code}")
            time.sleep(min(5 * 2 ** attempt, 60))
    raise RuntimeError("gemini unavailable")


def mmss(s):
    m, s = divmod(int(s), 60)
    return f"{m // 60:02d}:{m % 60:02d}:{s:02d}"


def to_s(t):
    n = [int(x) for x in t.split(":")]
    return sum(v * 60 ** i for i, v in enumerate(reversed(n)))


CHUNK_SCHEMA = {"type": "OBJECT", "properties": {
    "summary_en": TS, "activity": TS,
    "segments": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "at": TS, "topic": TS}, "required": ["at", "topic"]}}},
    "required": ["summary_en", "activity", "segments"]}

CHUNK_PROMPT = """Audio: a {mins}-minute piece of a Kick livestream by a Tunisian streamer speaking Tunisian darija
(mixed with French and English). Describe it:
- summary_en: 2-3 sentences, what is said and done.
- activity: one of intro, just chatting, coding/building, reacting to a video, gaming, giveaway, break, outro.
- segments: the topics in order. "at" = MM:SS from the start of THIS audio piece (first one at 00:00);
  add a new segment only when the subject really changes. "topic" = 3-8 words in English."""


def describe(i, path):
    # 10-min chunk is ~1.8 MB, small enough to send inline (limit 20 MB per request).
    d, model, usage = generate([{"inline_data": {"mime_type": "audio/ogg",
                                                 "data": base64.b64encode(path.read_bytes()).decode()}},
                                {"text": CHUNK_PROMPT.format(mins=CHUNK // 60)}], CHUNK_SCHEMA)
    off = i * CHUNK
    segs = [{"at_s": off + min(to_s(s["at"]), CHUNK - 1), "topic": s["topic"]} for s in d["segments"]]
    return {"start_s": off, "summary_en": d["summary_en"], "activity": d["activity"], "segments": segs,
            "model": model, "tokens": usage.get("totalTokenCount")}


MERGE_SCHEMA = {"type": "OBJECT", "properties": {"parts": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "start_s": {"type": "INTEGER"}, "title": TS, "description": TS, "summary_en": TS,
    "key_moments": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
        "at_s": {"type": "INTEGER"}, "what": TS}, "required": ["at_s", "what"]}}},
    "required": ["start_s", "title", "description", "summary_en", "key_moments"]}}}, "required": ["parts"]}

MERGE_PROMPT = """Below is a timeline of a {dur} livestream by a Tunisian streamer (Tunisian darija),
described in 10-minute chunks with topic changes at absolute times (seconds).
Group it into thematic parts, the way an editor would cut it into separate YouTube videos.
Rules:
- start_s of each part MUST be one of the "at_s" values in the timeline. First part starts at 0.
- Cut where the SUBJECT changes, not at chunk boundaries; the 20-minute chunks are arbitrary.
- Never create a part shorter than 15 minutes; fold intro and outro into the neighbouring part.
- Parts are 15 to 60 minutes; merge short tangents into the neighbouring part.
- title: short, punchy, French or Latin-script darija, like his Kick titles ("Stream khedma fel gueyla").
- description: 1-2 sentences in French.
- key_moments: 2-3, at_s taken from the timeline, "what" in French.
Timeline:
{timeline}"""

if __name__ == "__main__":
    audio, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    dur = int(float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                              "-of", "csv=p=0", str(audio)])))
    tmp = pathlib.Path(tempfile.mkdtemp())
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(audio), "-f", "segment",
                    "-segment_time", str(CHUNK), "-c", "copy", str(tmp / "c%03d.ogg")], check=True)
    chunks = sorted(tmp.glob("c*.ogg"))
    t0 = time.time()
    with cf.ThreadPoolExecutor(2) as ex:
        described = list(ex.map(describe, range(len(chunks)), chunks))
    print("chunks", len(described), round(time.time() - t0), "s", file=sys.stderr)
    timeline = "\n".join(f"[{mmss(c['start_s'])}] ({c['activity']}) {c['summary_en']}\n" +
                         "\n".join(f"   at_s={s['at_s']} ({mmss(s['at_s'])}): {s['topic']}" for s in c["segments"])
                         for c in described)
    merged, model, usage = generate([{"text": MERGE_PROMPT.format(dur=mmss(dur), timeline=timeline)}], MERGE_SCHEMA)
    parts = sorted(merged["parts"], key=lambda p: p["start_s"])
    for a, b in zip(parts, parts[1:] + [None]):
        a["end_s"] = b["start_s"] if b else dur
    out.write_text(json.dumps({"duration_s": dur, "merge_model": model, "chunks": described, "parts": parts},
                              ensure_ascii=False, indent=2))
    print("wrote", out, round(time.time() - t0), "s", file=sys.stderr)
