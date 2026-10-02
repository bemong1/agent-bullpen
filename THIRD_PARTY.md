# Third-party notices

Agent Bullpen has no runtime dependencies: the server uses only the Python standard
library, and the pages are plain HTML/CSS/JavaScript with no build step and no
external requests. The one third-party asset it ships is a font.

## Galmuri (bitmap font)

| | |
|---|---|
| Version | 2.40.3 (npm `galmuri@2.40.3`, `dist/`) |
| Source | <https://github.com/quiple/galmuri> |
| Copyright | Copyright (c) 2019–2025 Lee Minseo (quiple@quiple.dev), with Reserved Font Name "Galmuri" |
| License | SIL Open Font License, Version 1.1 (OFL-1.1) |
| Files | `static/fonts/Galmuri11.woff2`, `static/fonts/Galmuri11-Bold.woff2`, `static/fonts/Galmuri9.woff2` |
| License text | `static/fonts/OFL.txt` (full text, kept next to the font files) |

The font files are the official release files, unmodified. `static/fonts/galmuri.css`
is this project's own `@font-face` file that points at them. The server serves them from
`/fonts/`, so the pages never fetch a font from a CDN.

The OFL applies to the font files only. Agent Bullpen itself is under the MIT license
(`LICENSE`).
