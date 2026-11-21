---
name: video-use-setup
description: First-run setup for video-use — checks the install, creates a client workspace, connects creative apps over MCP (Canva, Google Drive, Dropbox, Adobe, Figma, a website), pulls the brand kit from them, interviews the user for whatever is still missing, and writes brand.md + style.json + brand/ assets that every later edit and the Studio use. Use when someone says "set up video-use", "onboard a new client/brand", "brand setup", "connect Canva to video-use", or when video-use runs and no brand.md exists for the client. Also use to update an existing brand ("update the altr brand", "change our caption style", "new logo", "add Dropbox to the studio").
---

# video-use setup

Goal: in one sitting, go from a fresh install to a client workspace where every future video comes out on-brand without re-explaining anything. Pull from the user's apps first, ask second, verify last.

Paths below: `<video-use>` is this repo (the directory two levels above this file). `<root>` is where the user keeps video projects. `<client>` is `<root>/<client-slug>/`.

## Fresh setup or update?

Brand files live in the client folder (`<root>/<client>/`), never in this repo — it's code, and a public fork. If the user wants brand history, suggest `git init` inside the client folder (private remote).

Ask for the client folder first. If `<client>/brand.md` already exists, this is an **update**, not a fresh setup:

1. Read `brand.md`, `style.json`, list `brand/`, and read `<video-use>/.studio.json`. Summarize the current brand in ≤6 lines.
2. Ask what's changing (multi-select): look (colors/fonts/grade) · captions · logo/recurring elements · voice/pacing · connected apps · re-pull everything from the apps.
3. Run only the matching phases below. Re-pull from apps only when asked or when a source changed (new Canva kit, new logo file).
4. Edit files in place. Preserve everything not being changed. Overwritten assets: keep the old one as `<name>.prev.<ext>` once.
5. Re-run Phase 6 only for what changed (captions → caption test, logo → logo test).
6. Append to `brand.md` a `## Changelog` line: `- <YYYY-MM-DD>: <what changed> (<source>)`. Update its `## Sources`.
7. Tell the user existing edits keep their old renders; new renders (and the Studio) pick up the change automatically, because render.py reads `style.json` at render time.

Small one-off tweaks don't need this skill at all: editing `brand.md` / `style.json` directly, or asking Claude in the Studio panel ("make the caption highlight our accent colour for the whole brand"), works too. The Studio's Claude can read and edit the client folder.

## Ground rules

- Use `AskUserQuestion` for choices (2–4 options, recommended first). Batch related questions; never ask more than ~4 at once. Free-text only for names, taglines, hex codes.
- Pull before you ask. If Canva/Drive/the website already answers a question, show what you found and ask for a yes, not a blank.
- Never ask the user to paste an API key into chat. Tell them the exact file and line to add, then check it exists without printing it.
- Everything you write for a client goes in `<client>/`. Never write client files inside `<video-use>`.
- Finish every phase before starting the next; at the end give a short table of what's set up and what's still missing.

## Phase 1 — Install check (fix what you can, report the rest)

Run these and fix in place:

| Check | Command | If missing |
|---|---|---|
| ffmpeg + ffprobe | `ffmpeg -version`, `ffprobe -version` | macOS: `brew install ffmpeg` |
| Python deps | `cd <video-use> && uv sync` | install uv (`curl -LsSf https://astral.sh/uv/install.sh \| sh`) |
| Transcription key | `grep -q '^ELEVENLABS_API_KEY=.' <video-use>/.env` | tell the user: add `ELEVENLABS_API_KEY=<key>` to `<video-use>/.env` (key from elevenlabs.io → API keys). Don't take it in chat. |
| Node 22+ (only for HyperFrames/Remotion animations) | `node -v` | optional; note it |
| Skill registration | `ls -la ~/.claude/skills/video-use ~/.claude/skills/video-use-setup` | `ln -sfn <video-use> ~/.claude/skills/video-use` and `ln -sfn <video-use>/skills/setup ~/.claude/skills/video-use-setup` |

## Phase 2 — Workspace

Ask: where projects live (default `~/Videos/video-use-projects`), the client/brand name, and whether this is the user's own brand or a client's. Create:

```
<root>/<client-slug>/
├── brand.md          ← written in Phase 5
├── style.json        ← written in Phase 5 (render.py reads it for every shoot under this folder)
├── brand/
│   ├── logos/        ← transparent PNG/SVG, light + dark variants
│   ├── fonts/        ← TTF/OTF files you downloaded
│   ├── images/       ← end cards, textures, product shots, headshots
│   └── music/        ← approved beds / stingers
└── <shoot-date>/     ← one folder per shoot, footage goes here; edit/ is created by video-use
```

render.py looks for `style.json` in `edit/`, then the shoot folder, then `<client>/`. The Studio reads `<client>/brand.md` and lists `<client>/brand/` in its Assets bin. So the shoot folder must sit directly inside the client folder.

## Phase 3 — Connect apps (MCP)

The Studio runs headless `claude -p`, which sees the same MCP servers as the CLI. List them (it takes ~30s; run it in the background):

```bash
claude mcp list
```

Pick out creative sources: **Canva, Google Drive, Dropbox, Adobe, Figma, Notion, Box, OneDrive/Microsoft 365**, plus any web-fetch/scrape tool for the brand's website. Ask the user which ones hold brand material (multi-select). For each picked one:

- `✔ Connected` → good.
- `! Needs authentication` → tell them how: claude.ai connectors are authorized at claude.ai → Settings → Connectors (then they show up in the CLI automatically); CLI servers via `/mcp` in a `claude` terminal session. Continue with the others meanwhile.
- not listed → it can be added from claude.ai → Settings → Connectors, or `claude mcp add`.

Then write `<video-use>/.studio.json` (gitignored). The Studio grants nothing by default; only what's listed here runs without prompts. Prefer listing just the read/search/export tools (get them from the server's tool list; e.g. Canva `search-designs`, `list-brand-kits`, `get-assets`, `export-design`, `get-export-formats`). A whole-server prefix is allowed when the user explicitly wants it. Tool-name rule, from the `claude mcp list` name:

| Listed as | allow prefix |
|---|---|
| `claude.ai Google Drive` | `mcp__claude_ai_Google_Drive` (spaces → `_`) |
| `plugin:marketing:canva` | `mcp__plugin_marketing_canva` |
| `context7` | `mcp__context7` |

```json
{
  "apps": [
    {"name": "Canva", "use_for": "brand kit, logos, designs exported as PNG/MP4",
     "allow": ["mcp__claude_ai_Canva__search-designs", "mcp__claude_ai_Canva__list-brand-kits",
               "mcp__claude_ai_Canva__get-assets", "mcp__claude_ai_Canva__get-export-formats",
               "mcp__claude_ai_Canva__export-design", "mcp__claude_ai_Canva__read-design"]},
    {"name": "Google Drive", "use_for": "brand guidelines, raw logos, b-roll",
     "allow": ["mcp__claude_ai_Google_Drive__search_files", "mcp__claude_ai_Google_Drive__read_file_content",
               "mcp__claude_ai_Google_Drive__download_file_content", "mcp__claude_ai_Google_Drive__get_file_metadata"]}
  ]
}
```

Merge into an existing `.studio.json` rather than overwriting it.

## Phase 4 — Pull the brand kit

Do this with the connected tools, then summarize what you found in one table (source → what you got).

- **Canva:** list brand kits; take colors (with roles if named), fonts, logos. Export logos as transparent PNG at ≥1000px wide into `brand/logos/`. Look for existing designs named like "end card", "lower third", "thumbnail", "intro" and export the useful ones as PNG (stills) or MP4 (animated) into `brand/images/`.
- **Drive / Dropbox / Box / OneDrive:** search `logo`, `brand guidelines`, `style guide`, `brand book`, `fonts`. Read guideline PDFs/docs for colors, type, tone, do's and don'ts. Download logos and fonts into `brand/`.
- **Adobe / Figma:** same idea: brand libraries, logo files, color styles.
- **Website** (ask for the URL if not known): extract the dominant colors, fonts (from CSS), the tagline, how they talk, and the logo (favicon/apple-touch-icon/og:image as a fallback only).

Canva's free plan can't export transparent PNGs (it bakes the background in). If a logo comes back with a solid background, try Canva's or Adobe's background-removal tool if one is connected, otherwise key it out with ffmpeg (`colorkey=0xF5F0E6:0.08:0.05` for a flat background) and check the edges, or ask the user for the original file.

Name files plainly: `logo-primary.png`, `logo-white.png`, `logo-mark.png`, `endcard.png`. Check images with `ffprobe` (size, alpha: `pix_fmt` should contain `a` for transparent logos). If a "transparent" logo has no alpha, say so.

## Phase 5 — Interview for the gaps, then write the files

Ask only what Phases 3–4 didn't answer. Typical set, in this order:

1. **What it is:** one sentence on the brand/product, the audience, the 1–3 platforms it posts to → default aspect (`9:16` Reels/TikTok/Shorts, `16:9` YouTube/LinkedIn, `1:1`).
2. **Voice and pacing:** three adjectives; pacing (tight/punchy · conversational · cinematic); how hard to cut filler and retakes.
3. **Look:** palette roles (primary, accent, text, background — hex); grade (none · neutral punch · warm cinematic · custom); heading and body fonts.
4. **Captions:** style (bold 2-word UPPERCASE · natural sentence · boxed/pill · none), colors (text, outline/box, highlight word), position (lower third · center · safe-zone above platform UI), max words per line.
5. **Recurring elements:** logo placement/watermark rule, lower-third name cards, intro sting, end card + CTA text, music feel, legal/disclaimer line, handles/URL.
6. **Never:** words, claims, colors, music or visual tropes to avoid.

Then write:

**`<client>/brand.md`**: the human-readable brand brief that video-use reads before asking style questions:

```markdown
# <Brand> — video brand

## Identity
- What: <one sentence>
- Audience: <who>
- Platforms → default format: <TikTok/Reels 1080×1920@30>, ...
- Handles / URL / CTA: ...

## Voice & pacing
- Tone: <3 adjectives>
- Pacing: <tight | conversational | cinematic>; filler policy: <cut all | keep natural>
- Never say: ...

## Look
| Role | Hex | Use |
|---|---|---|
| Primary | #...... | lower thirds, end card bg |
| Accent | #...... | highlight word, progress bars |
| Text | #...... | captions |
| Background | #...... | title cards |
- Fonts: heading <Font> (<path or "installed">), body <Font>
- Grade: <preset or filter>

## Captions
<style, case, words/line, colors, position> (encoded in style.json)

## Recurring elements
- Logo: brand/logos/<file> — <corner>, <size % width>, <opacity>, <always | intro+outro only>
- Lower third: <font/colors/animation>
- Intro / end card: brand/images/<file> — <duration>, CTA "<text>"
- Music: <feel>; approved beds in brand/music/

## Do / don't
- ...

## Sources
- Canva brand kit "<name>", <website>, Drive "<doc>", interview <date>
```

**`<client>/style.json`**: caption style render.py applies to every shoot under this client. Use the per-field shape so unspecified fields inherit the default:

```json
{"subtitle": {"FontName": "Montserrat", "FontSize": "20", "Bold": "1",
              "PrimaryColour": "&H00FFFFFF", "OutlineColour": "&H00000000",
              "BorderStyle": "1", "Outline": "2", "MarginV": "60"}}
```

ASS colours are `&HAABBGGRR` (alpha first, 00 = opaque, then **blue-green-red**): `#FF6A1A` → `&H001A6AFF`. For a filled box behind captions use `BorderStyle=3` with the box colour in `BackColour` (and `OutlineColour` for some libass builds); semi-transparent alpha `80`.

**Fonts fail silently.** libass resolves `FontName` through the system: after choosing a font, run `fc-match "<FontName>"`. If it returns a different family, install the TTF (copy to `~/Library/Fonts/` on macOS) and re-check. Note the outcome in brand.md.

## Phase 6 — Prove it

1. **Caption test:** follow the safe-zone method in `<video-use>/SKILL.md` (Subtitles section): render the style on a plain black frame at the default format with a long sample line, burn a `drawbox` at the platform safe zone, save to `<client>/brand/style-test.png`, `Read` it, and show it. Adjust `MarginV`/`FontSize` until it sits right.
2. **Logo test:** composite `brand/logos/<primary>` onto a sample frame in the chosen corner at the chosen size → `<client>/brand/logo-test.png`; show it.
3. Ask for a yes on both, or iterate.

## Phase 7 — Hand off

- Offer to try the Studio right away: `python <video-use>/studio/make_demo.py <client>/demo-shoot` then `python <video-use>/studio/server.py <client>/demo-shoot/edit` (run in background). In the Studio's Assets tab the brand folder is listed, and Claude in the panel can pull more from the connected apps.
- Tell them the routine from now on: drop footage into `<client>/<date>/`, say "edit this with video-use". video-use reads brand.md and style.json automatically.
- Close with a table: Install ✔/✘ · Workspace path · Apps connected / needing auth · Brand assets pulled (count by type) · brand.md ✔ · style.json ✔ · Tests approved ✔ · Still missing (e.g. "no music beds", "Dropbox needs auth").
