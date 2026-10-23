# Thumbnails v2: face + text (2026-10-23)

Melek validated this layout on 2026-10-23 from four prototypes (current, A screen + text,
B face + text, C split + band). He chose **B with reaction photos**. The prototype commands
are at the end of this file.

## Layout (1280x720 JPEG, under 2 MB)

1. **Background:** the video frame at `at_s`, scaled to 1280x720, `boxblur=6`, darkened
   (`eq=brightness=-0.25`).
2. **Face:** one reaction photo, scaled to 760 px high, on the right (`x=W-w+40`, `y=-20`).
   Its left 35% fades from transparent to opaque (alpha gradient), so a photo with a
   background blends in. No cut-out is needed.
3. **Text:** a hook of 2 to 4 words in capitals, on the left, starting at x=50.
   Line 1 is yellow `#FFD400`, size 160, black border 10. Line 2 is white, size 110, black
   border 10. Line 1 = first word, or the first two if they are short; line 2 = the rest.
   The text must fit in the left 60% (shrink the font size until `text_w` fits).
4. **Font:** Impact (`C:\Windows\Fonts\impact.ttf`) on Windows, else Anton or DejaVu Sans
   Bold through the existing `_find_font()` order with Impact added first.

## Reaction photos

- Folder: `CONFIG_DIR/faces/` (`%USERPROFILE%\.config\k2y\faces\` on the laptop). Any
  `.jpg`, `.jpeg`, `.png`, `.webp`.
- Choice: deterministic, `sorted(files)[(vod_id * 31 + idx) % len(files)]`, so a re-run gives
  the same thumbnail and parts of one VOD get different faces.
- **No photos:** same layout without the face. The text then gets the full width. Log one
  warning per run.
- User paths never go into the filter string (spec 6.7): pass the photo as a second `-i`
  input and the hook through a relative `textfile=` in the thumbnail folder, as today.

## Hook text (Gemini)

- The merge schema gets `thumb_text` per part (required), and a top-level `vod_thumb_text`
  for the full VOD. Prompt rule: "2 to 4 words, in the language of the stream title (French or
  Latin-script darija), punchy, no emoji, at most 22 characters".
- Code sanitises it: strip, upper-case, remove `<>` and newlines, cut at 22 characters on a
  word boundary.
- **Fallback** (plans made before this change, or empty value): the first 3 words of the
  title, upper-cased.
- The prompt also names the streamer **Melek** (Gemini called him "Malik") and asks for
  French `description` fields.

## Cut-outs (added 2026-10-23 at Melek's request)

- Melek's photos are landscape phone shots with the face centred, so the code crops them to
  a centred 4:5 portrait before scaling.
- Background removal happens once per photo, outside k2y, so k2y keeps no extra dependency:
  `rembg i -m birefnet-portrait in.jpg out.png` in a throwaway virtualenv. Put the PNGs in
  `faces/`.
- The code keeps the PNG's alpha and draws an 8 px white rim from the dilated alpha, so a
  dark t-shirt stays visible on the dark background. Under an opaque JPG the rim is hidden,
  so JPGs render as before.

## Out of scope

Picking webcam frames automatically, background removal inside k2y, A/B testing thumbnails.

## Prototype (reference)

```
ffmpeg -i frame.png -i face.png -filter_complex "
[0]crop=800:450:20:80,scale=1280:720,boxblur=6,eq=brightness=-0.25[bg];
[1]crop=640:720:180:0,scale=-1:760[face];
[bg][face]overlay=x=W-w+40:y=-20,
drawtext=fontfile=Anton.ttf:textfile=l1.txt:fontsize=160:fontcolor=#FFD400:borderw=10:bordercolor=black:x=50:y=210,
drawtext=fontfile=Anton.ttf:textfile=l2.txt:fontsize=110:fontcolor=white:borderw=10:bordercolor=black:x=50:y=400" v2.jpg
```
