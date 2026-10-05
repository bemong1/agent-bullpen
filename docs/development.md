# Development

Python 3.9+ for the server and the tests, standard library only. Node.js is needed only for the optional screen checks in `tools/regress`. There is no build step, no package manager and no lock file.

## Layout

```
server.py        HTTP entry point: argument checks, the request handler, static files, start-up
statusline.py    optional Claude Code status line command that keeps the plan usage for the bottom bar (does not import board/; see docs/configuration.md)
board/           reading and interpreting transcripts, and the shapes the pages read
static/          the pages: plain HTML, CSS and <script> files (no bundler); fonts in static/fonts/; the text of the screen in static/locales/
tests/           unittest suites
tools/           regression, synthetic-data, scenario and measurement helpers
docs/            guides
```

`python3 server.py` is the only way in; everything else is imported from it. The version is written once, in `board/__init__.py` (`server.py --version` prints it; `CHANGELOG.md` starts with the same number, and a test compares them).

| Module | Role |
|---|---|
| `board/access.py` | the access token: making one, the `Gate` a listener checks (cookie, `Authorization: Bearer`, `?token=`; constant-time comparison), the 401 page, the redirect that takes the token out of the address, the loopback test, the addresses of this machine to print. Imports `i18n` only |
| `board/util.py` | folders and their sources (`resolve_paths`), the never-open list (`DENY_FILES`), the file-opening policy (`hidden_or_secret`, `stat_regular`, `stat_plain`, `open_safe`), text and time helpers, incremental transcript reading (`Tail`), shared thresholds |
| `board/procs.py` | one place that answers "is this process alive?" with yes / no / unknown (`/proc` on Linux, `ps` elsewhere); it never raises |
| `board/tokens.py` | price table (`PRICES`) and `TokenMeter` (Claude and Codex) |
| `board/codex_parse.py`, `board/codex_facts.py`, `board/codex_index.py` | reading Codex rollout files; what a thread is (`kind`: root, native sub-agent, approval-review guardian or an unknown shape hidden as `internal`; the line where a sub-agent's copied history ends), the commands a thread ran (`CommandExecution`), the collaboration events between threads and the places where the command facts may be missing (`gaps`), kept within a text budget (`codex_facts.py`); the list of Codex threads and the read accessors of those facts (`CODEX.cmds`, `collab`, `gaps`, `cmd_text`, `root_of`) |
| `board/link.py` | reading Bash calls into facts (what each call launched, where its output went, when it ran: one redirect reader for all uses), and attaching `codex exec` and `claude -p` runs started from Bash, by an agent or by another run, or from the shell of a Codex thread (its `CommandExecution` records), to the session, thread and agent that launched them (including ones started by a script file the command runs, one level deep). It publishes the one graph of who started whom (`owner_of`, `page_of`, `descendants`) that the pages, the list and the session opener read. A background pass reads the transcripts of the agents themselves. The judgment itself is in `board/affil.py` |
| `board/lineage.py` | the most certain link: a live `claude -p` child or a `codex` process's open rollout is attached to the Claude session named by its environment (`CLAUDE_CODE_SESSION_ID`, cross-checked with `CLAUDE_PID`, or `CODEX_THREAD_ID` and `CODEX_SESSION_ID` for a child of a Codex shell; only those four names are read from `environ`) or else to the nearest Claude ancestor that has a `sessions/<pid>.json`; remembered in memory, and the certain links also in `links.json` (format version 6) unless `--no-link-cache` (see [configuration](configuration.md#what-the-server-writes)) |
| `board/facts.py` | the shapes the judgments pass around (`Span`, `Redirect`, `Turn`, `Run`, `Evidence`, `Relation`, `Unit`, `Assignment`, `Artifact`) and the rule names; definitions only, no logic |
| `board/affil.py` | who launched a child run: the ranked evidence, ties held rather than broken, the agent inside the session, who instructed a resumed run. A pure function of facts: it reads no file and no process |
| `board/fingerprint.py` | the instruction-text match: normalizing, 32-character anchors, how much of an instruction a command's text covers, and a bounded in-memory text cache (never written to disk) |
| `board/runstate.py` | run and turn boundaries in a transcript, error classification, `judge` (status, reason, reset time) from the facts of a run, the orchestrator's wait on a usage limit, grouping of limits. Pure functions |
| `board/units.py` | the debate list from the disk (folders, briefs, a bounded walk of a repository), the report mentions in a text, seat assignment from the evidence, cell states. The disk is read through one catalog; the judgment is a pure function of the agents' facts |
| `board/diag.py` | gathers a session's diagnostics for `/api/diag` (`/api/state` carries only the counts): a code, a level, a subject and short parameters, never transcript text |
| `board/i18n.py` | registers and reads the dictionaries in `static/locales/`, picks the terminal language (`--lang`, `AGENT_BULLPEN_LANG`, locale variables) and fills `{name}` placeholders; the start-up output, error messages and `--help` come from the `cli.*` keys. It imports no other `board` module, so any module can print a terminal text through it |
| `board/agents.py` | `Agent`, `CodexAgent` (a `codex exec` thread or a native sub-agent thread; `ForkSkip` leaves out the parent's history copied into the front of a sub-agent's transcript), stall detection; every transcript line also goes to a run tracker and a notice ledger from `runstate.py`; the markdown files a Bash call writes by a redirect or `tee` are kept apart from the Write and Edit ones (`shell_writes`) and count as writes in the debate judgment |
| `board/debates.py` | debate structure (`<topic>/r<N>/<who>.md`) from the units and seats of `units.py`, the brief table, automatic final-result lookup |
| `board/views.py` | what the pages read: `state`, `alerts`, `agent_detail`, `timeline`, `allowed_file`; also the system lines that stand for usage-limit and API-error records, and the `link` of an agent with the reason a `content_short` guess is one (`incomplete`, `assumed`) |
| `board/sessions.py` | `Session` and `CodexSession` (a Claude session, or a Codex thread nothing started, as the orchestrator of a page): read a transcript, accumulate events, read the team of the page from the graph of `link.py` (`LINKS.descendants`: `claude -p` children with the sub-agents they started themselves, `codex exec` threads, native sub-agent threads with the events of their collaboration), judge each agent's status with `runstate.judge`, place child runs (grandchildren too) and cache the debate judgment (reused until an agent's record, a status or a debate file on disk changes; a Bash write counts as a change of the record) |
| `board/catalog.py` | session list (with the size of each page's team), per-project representative, `sources` for the diagnostic panel, and the session registry (an id that something started opens the page of the top orchestrator) |
| `board/plans.py` | plan and usage numbers (the status line file, the `.claude.json` cache), rate-limit hits; it makes no request and opens no credential file |

There are no import cycles; a module imports only from an earlier group of this chain: `facts` · `fingerprint` · `procs` · `i18n` · `tokens` · `codex_facts` (these import no other `board` module) → `access` (imports `i18n`) · `util` → `codex_parse` · `lineage` · `runstate` · `units` → `affil` · `codex_index` · `debates` → `link` · `agents` → `diag` · `plans` → `views` → `sessions` → `catalog` → `server.py`. The data flow: a background loop (every 2 s) scans, each session polls its files, and a request to `/api/state` renders the current shapes. The pages load their scripts in this order: `i18n.js`, `common.js`, `game-art.js`, `game.js`, `game-demo.js`, `board.js` (`game.html` has no `board.js`). If you add a static file, add it to `STATIC_FILES` in `server.py`; unlisted files are 404.

## Dictionaries and languages

All the text of the screen and of the terminal output lives in one dictionary file per language, `static/locales/<code>.json`. The pages read it through `static/i18n.js` (`t('key', {params})`), the server through `board/i18n.py` (the start-up output, errors and `--help`). The server does not translate what it sends to the pages: it sends codes and parameters (`title_i18n`, `error_info`, `text_i18n`, status codes) next to the old Korean fields, and the page turns them into text in the language it is showing. Text taken from the transcripts (instructions, reports, agent descriptions, model names, paths) is never translated.

### File format

```json
{
  "meta":     {"code": "en", "name": "English", "locale": "en-US"},
  "messages": {"common._": "", "common.you": "You", "unit.agent": {"one": "{count} agent", "other": "{count} agents"}},
  "formats":  {"time": {"hour": "2-digit", "minute": "2-digit", "hourCycle": "h23"}},
  "limits":   {"office.tag.empty": 9}
}
```

- `meta.code` must equal the file name; `name` is what the language selector shows; `locale` is the tag handed to `Intl`.
- `messages` are flat dotted keys, the same keys in the same order in every file, named by meaning and place (`board.card.tokens`; a narrow place gets a `.short` key). They are grouped in areas, each opened by an empty marker key `<area>._`, in this order: `common status kind time unit`, `board`, `office demo`, `diag page`, `cli alert event`. A key starts with its area's name.
- A value is a sentence with named placeholders (`{name}`), never a fragment to be glued to another. A plural value is an object `{"one": …, "other": …}` chosen with `Intl.PluralRules` from `count`; `other` is required. Two formatters exist: `{name:subject}` adds the Korean subject particle, `{n:number}` groups digits for the language.
- A key missing in the chosen language falls back to English, then to `[key]`.
- `formats` are `Intl.DateTimeFormat` options for `time`, `timeSec`, `dateTime` and `monthShort`. `limits` maps a key to the largest number of characters its English text may have (a placeholder counts as two), for the narrow spots of the office; the check enforces it for English and only notes it for other languages.

### Adding a key

1. Put the key in `en.json` and in `ko.json`, at the same place, right under its area's marker. To merge a whole area without editing the layout by hand: `python3 tools/i18n_merge.py <area> <fragment.json>` (it takes a lock, replaces only that area in both files and writes them back; pass the area's full set; see `--help` for the fragment format).
2. Use it. JavaScript: `t('area.key', { name })`; escape data values with `esc()` before passing them into a string that goes to `innerHTML`. HTML: `data-i18n="area.key"` (also `data-i18n-title`, `-placeholder`, `-aria-label`), with the English text written in the HTML as the fallback shown when the dictionary cannot be loaded. Python: `cli_t('cli.some.key', value=…)` in `server.py`, or `board.i18n.translator(lang)`.
3. `python3 tools/regress/i18n_check.py --strict` must report nothing (see below). Do not type screen text in the page code: the check reports Hangul outside comments in `static/*`, except on a line marked `i18n-ok` (the patterns that read Korean wording in transcripts and the layer for old responses).

### Adding a language

One JSON file, no code change:

1. Copy `static/locales/en.json` to `static/locales/<code>.json` (a lowercase language code such as `fr` or `pt-br`; the code is the file name).
2. Set `meta.code` to that code, `meta.name` to the language's own name (shown in the selector) and `meta.locale` to a tag `Intl` knows. Translate the values in `messages`; keep every key, every `{placeholder}` and the plural categories your language needs (`other` is always required); adjust `formats` if its date or time style differs. A key you leave out falls back to English.
3. Run `python3 tools/regress/i18n_check.py --strict`. It compares your file with English: same keys in the same order, same placeholders, valid plural objects, limits that name real keys.
4. Reload the page. The server rescans the folder on every request, so a new file shows up in the selector without a restart; `?lang=<code>` and `--lang <code>` accept it (the terminal reads the file when the server starts). A file that is not a regular file `<code>.json` directly in the folder, or whose `meta.code` or shape is wrong, is not registered (a well-named file that is rejected gets a warning when the server starts). Names that start with `_` are never registered.

Look at the office strip in your language: some places are narrow (name tags, whiteboard cells, the demo captions), and the `limits` are only a guide; the real test is the pixel width on screen.

## Tests

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
```

The test process never reads your real home: `tests/compat.py` points `HOME` and `XDG_CACHE_HOME` at a throwaway folder before `board` is imported (a status line reading in your real `~/.cache/agent-bullpen/` would otherwise change the plan bar), removes `CLAUDE_CONFIG_DIR`, `CODEX_HOME` and `AGENT_BULLPEN_TOKEN`, and gives every server it starts its own home and cache through `isolated_env`. `tests/test_sandbox.py` checks that. A test that imports `board` imports `compat` first.

The suites use temporary folders and ephemeral ports. They cover parsing, linking (process lineage, environment, scripts, loops, grandchild agents, the instruction-text match, ties, the link record file), run and status judgments (boundaries, errors, usage limits), debate recognition and seat assignment, the diagnostics, the scenario generator (`test_scenarios.py`, see below), the Codex facts, the linking from Codex shells and the page of a Codex orchestrator (`test_codex_facts.py`, `test_codex_link.py`, `test_codex_page.py`), the harvester and the benchmark tool (`test_harvest.py`, `test_bench.py`), the `/api/file` limits, path and option handling, the start-up sequence, the dictionaries and the language order (`test_i18n*.py`, `test_server_i18n.py`), the English decision-request detection (`test_decide_detect.py`), that no source holds code that opens a credential file or makes a network request, the access token (`test_access.py`: 401, cookie, redirect, Bearer, constant-time comparison, no token in a log or a response, `--token`, `--no-auth`, `--host` with any address or name, `--version`; `test_access_robust.py`: odd requests, odd files and odd shapes of valid JSON), that the link scan opens only regular record files (`test_link_files.py`), that the public text matches the program (`test_docs_checks.py` runs `tools/regress/docs_checks.py`), and the throwaway home. Tests that start the server pin the terminal language themselves (`AGENT_BULLPEN_LANG=ko`), so they do not depend on your locale.

Two quick static checks:

```bash
python3 tools/regress/names_check.py server.py board/*.py     # undefined global names (a typo after moving code between modules)
python3 tools/regress/i18n_check.py --strict                  # the dictionaries and the pages' hard-coded text; exit 1 on any finding
```

`i18n_check.py` checks, per group: `syntax` (the file parses, no duplicate keys, `meta.code` matches the file name), `zones` (the area markers exist once each, in order, and every key sits in its area), `keys` (every language has English's keys in the same order), `params` (placeholders match English), `plural` (valid categories, `other` present), `limits`, `hangul` (no Hangul in English values), `terms` (English values avoid the banned spellings: *sub-agent*, *Notifications*, *drawer*, *Command room*, a bare *board*), `refs` (every `t('key')` and `data-i18n` in `static/*` names an existing key) and `static-hangul` (Hangul in the pages outside comments). Without `--strict` it reports and exits 0; `--list` prints every finding.

## `tools/regress`

Maintainer tools for comparing a change against a reference copy and for checking the page scripts without a browser. The page checks need a *fixture*: the API responses a page would fetch, saved as `<prefix>_state.json`, `_sessions.json` and so on. Fixtures frozen from real sessions contain conversation text, so none are committed; create them in `/tmp`, use them, delete them. Without any real records, build one from the synthetic HOME:

```bash
python3 tools/make_synth_fixture.py /tmp/synth-fx          # synthetic HOME -> server -> saved API answers
node tools/regress/ui_checks.js static /tmp/synth-fx/synth
node tools/regress/lounge_checks.js static /tmp/synth-fx/synth
```

This is what the CI `ui` job runs. `make_synth_fixture.py` fixes the clock in the saved answers so every run sees the same times, and leaves solo sessions out of the session list (`ui_checks.js` builds its own); `--no-freeze` keeps the real clock (the checks pass either way). It also freezes a second HOME, `home_stopped` (the same scene plus `synth_home.py --stopped --live`), into `synth_stopped_*.json` for `state_checks.js`; that adds about 15 seconds, and `--no-stopped` leaves it out. A third HOME, `home_codex` (`synth_home.py --codex-orch --live`: a Codex orchestrator with two native sub-agents, a guardian thread, two `claude -p` runs and a `codex exec` run, a small debate), is frozen into `synth_codex_*.json` (and `synth_codex_agent.json`, what `/api/agent` says of its `codex exec` run) for `codex_checks.js`; `--no-codex` leaves it out. If the second fixture cannot be made it only says so on stderr and the first one is still written; `state_checks.js` then reports the missing files (a failure in CI).

| Tool | What it does |
|---|---|
| `run_board.py <src> --port N` | starts a copy of the server (this one, or an older one to compare with) with the link record off; for an older source it also switches off the account usage thread, so no credential file is read |
| `api_snap.py <portA> <portB> <session>...` | compares the API responses of two running servers: `ALL OK` or `DIFF`. The new fields that the language work and the status work added (`reason`, `resets_at`, `node`, `by`, `parent`, `runs`, `link.rule_class`, `diag`, a work room's `room` and `guide`, …) are allowed as differences (`API_SNAP_ALLOW`); the fields of the removed account usage query (`usage_api`, `error`, `error_info` of `/api/plans`) are not, so an older server shows a `DIFF` there; new values of old fields (`interrupted`, `unknown`, `limit_wait`) and the system lines in the flow are not, so a session that has such a state is an intended `DIFF`; `--raw` shows everything |
| `make_fixture_home.py <session> <dir>` | freezes one of *your own* sessions into a fixed `HOME` (set `AB_SRC` to the source folder) |
| `render_snap.js`, `game_snap.js`, `demo_snap.js`, `lounge_snap.js`, `snap_diff.js` | run the page scripts under Node against a fixture and print hashes/snapshots to diff |
| `ui_checks.js`, `lounge_checks.js`, `highlight_check.js` | synthetic checks of the page logic (exit status = number of failures); `ui_checks.js` runs in Korean, the Korean wording is its expected values |
| `ui_checks_en.js` | the same fixture in English: cards, details panel tabs, the diagnostic panel in every state, the plan bar, the new server fields; it reads the expected words from the page's own `t('key')` and fails on any Hangul outside data or any `[key]` |
| `i18n_checks.js`, `i18n_apply_checks.js` | the language runtime (`static/i18n.js`: language order, lookup, plurals, dates, picker) and the English fallback text in the HTML when the dictionary cannot be loaded |
| `office_checks.js`, `diag_i18n_checks.js` | the office texts and a whole demo run, who walks in from the door and who is put at a desk when the rooms are laid out again, and the diagnostic card, in both languages |
| `state_checks.js` | the new states in both languages: the orchestrator waiting on a usage limit (four wordings), Interrupted with each reason, Unknown and an unknown status, grandchild order and indentation, the header chips, the paused cell, system lines, alert titles, the details of a resumed run, the three reasons for a text-match guess, the Diagnostics list, and the fallback against an old server without the new fields. It reads a second fixture, `synth_stopped_*`. Without it the check is skipped on a laptop, and fails when `CI` or `REQUIRE_FIXTURES` is set (the CI job sets it), so a missing fixture cannot let CI pass unchecked |
| `codex_checks.js` | the page of a Codex orchestrator in both languages, against the third fixture (`synth_codex_*`): a card for each of the five agents and none for the approval review, the name tag of a native sub-agent, the spawn card that says the instruction is encrypted (read from the dictionary), a sub-agent's final report, the `exec` time of a run a Codex thread started, the two seats of its debate. Without the fixture it is skipped on a laptop, and fails when `CI` or `REQUIRE_FIXTURES` is set |
| `i18n_check.py` | the dictionary checks above |
| `docs_checks.py` | the public text against the program (no browser, no fixture): no remnant of the removed usage request or of an earlier release in the README, changelog, guides and issue templates; the status line file's contents, the macOS wording and the start output shown in the README match the code and the dictionary. The unit tests run it too (`tests/test_docs_checks.py`) |
| `i18n_boot.js` | not a check: the shared loader that puts `i18n.js` and the dictionaries into the fake browser; the loaders run in the language of `AB_LANG` (default `ko`, which is what the saved snapshots expect) |

Each script has a `usage:` comment near the top; `<fixture prefix>` is the path of a fixture without the trailing `_state.json`. Example: `node tools/regress/ui_checks.js static /tmp/synth-fx/synth`.

## Synthetic HOME

`tools/synth_home.py` builds a made-up `HOME` holding Claude Code and Codex transcripts with no real conversation in them. Use it to run, test and screenshot the dashboard without touching your own data. The pictures in `docs/images/` come from a calmer scene built on it, `tools/docs_scene.py` (about ten agents working, a few resting in the lounge, a few stopped runs in the agent list; `--live` and `--stop` work as below). Its options are listed by `python3 tools/synth_home.py --help`. Start the server with `HOME=<that folder>` (`env -u CLAUDE_CONFIG_DIR -u CODEX_HOME HOME=<folder> python3 server.py --port 8811`), not with `--claude-config-dir` / `--codex-home` alone: the records say `~/work/...`, which the server expands with the `HOME` it runs under. `--live` adds fake `claude` processes so sessions show as working. `--stopped` (with `--busy --live`) adds work that stopped and nests: an orchestrator waiting on a usage limit, agents stopped by a limit and by an API error, `claude -p` runs that exited early, hit the background time limit or died, a grandchild and a great-grandchild, a paused debate cell and the diagnostics they raise. `--links` adds `claude -p` children that can only be guessed or not linked at all. `--codex-orch` (with `--live`) adds a Codex orchestrator in `work/acme-ledger`: a TUI thread with two native sub-agents (one finished, one working), an approval-review thread, two `claude -p` runs and a `codex exec` run its shell started, and a small debate folder `talk/`; the Codex records have the shapes of Codex 0.160 (the sub-agent's front copied from its parent, ciphertext beside a plain note, `CommandExecution` records written when the commands end) and the helper's process carries `CODEX_THREAD_ID` and `CODEX_SESSION_ID`. `--stop` stops only those processes and removes only the session files this tool wrote; it refuses a real `HOME` or a folder this tool did not make.

To retake the pictures in `docs/images/en/` (English screen, for `README.md` and `guide.md`) and `docs/images/ko/` (Korean screen, for `README.ko.md` and `guide.ko.md`): `python3 tools/shoot_docs.py` (it builds the docs scene, starts its own servers on that and on an empty HOME and stops them again; it picks the moment of the lounge so that two people sit at a tea table; `--lang en|ko` takes one language; `--scene synth --busy --stopped` takes the crowded stress scene instead; it needs Playwright with Chromium, see its `--help`). Keep each picture under 500 KB, and look at every one before committing: nothing real may appear in it.

The moving picture at the top of the READMEs, `docs/images/en/office.gif` and `docs/images/ko/office.gif` (the office page's "New work" demo on the docs scene: agents walk in, write their reports in a room and walk to the lounge), is retaken with `python3 tools/shoot_gif.py` (same Playwright setup, and `ffmpeg` on the PATH; it plays the page on a clock it steps by hand, so it does not depend on the speed of the machine; `--lang en|ko` takes one language, `--frames <folder>` keeps the PNG frames). Keep each GIF under 3 MB, and look at a few of its frames before committing (`ffmpeg -i office.gif -vf fps=2 f%03d.png`): nothing real may appear in it.

## Scenario generator and measurements

These are maintainer tools. The unit tests run the generator on every `unittest discover`, so a regression shows there; the rest you run by hand.

`python3 -m tools.scenarios.run` builds cases from axis values (who launched the run and how, what its records look like, what the debate folder holds …), writes each one into a synthetic HOME, reads it with the real board code and grades every field of the answer against an oracle that does not import `board/`: pass, miss or wrong. A case id is its axis values (`aff:way=bg;via=stdin;…`). `--list` prints the ids, `--cases '<glob>'` runs some of them (`'aff:*'`, `'sta:*'`, `'deb:*'`, `'cpl:*'`, and `'cxo:*'` for the Codex orchestrator: who started a run or thread from a Codex thread's shell, a native sub-agent's life, the events of its collaboration, the approval review that is no card, the debate of the runs), `--md` prints the red cells as a Markdown table (`--md FILE` writes it to a file). The cells that are known to be red are listed with the reason in `tests/scenarios_xfail.json`, and `tests/test_scenarios.py` fails on a new red cell and on a cell that turned green without the list being updated. Only `python3 -m tools.scenarios.run --write-xfail` rewrites that file: run it on purpose and read the diff before you keep it.

`python3 tools/bench.py run --scale 1 --check tools/bench_baseline.json` measures a synthetic big HOME (100 sessions, 25 000 Bash calls, 200 child runs): the first scan, later scans, `state()` and `/api/state` over HTTP. `--check` fails when a gating number (CPU and in-process times; wall-clock HTTP times only warn, unless `--strict`) is more than 25 % worse than the baseline; `--budget <baseline>` checks the fixed budgets instead. `--what scan` skips the HTTP part, `--real` measures your real HOME read-only (it prints times and counts only), `--json FILE` keeps the numbers and `bench.py baseline` turns several of them into a baseline file. The HTTP part serves on a free port among 8857–8860.

`python3 tools/harvest.py <session id or prefix>` runs only on your machine, on a session tree under `~/.claude/projects` (`--claude-home DIR` for another place). It prints nothing from the records: only the axis values of the scenarios that tree amounts to, and the answers the oracle expects (`--json` for one JSON document). A feature that no axis value can say is printed as `new axis value needed: <name>` and the exit status is 2 (1 when the session is not found). `--check FILE` tests that a file holds only tokens of that vocabulary, which is how the tool is tested for leaks. Use it to turn something odd in your own sessions into a scenario case without copying anything private into the repository.

## CI

`.github/workflows/test.yml` runs on each push to `main` and each pull request: the unit tests on Linux (Python 3.9 and 3.12) and macOS (3.12), and on Linux the page checks (`ui_checks.js`, `lounge_checks.js`, the language and English checks `i18n_checks.js`, `i18n_apply_checks.js`, `office_checks.js`, `diag_i18n_checks.js`, `ui_checks_en.js`, and `state_checks.js` for the new states; Node 22, with `LANG=C.UTF-8`) against fixtures that `tools/make_synth_fixture.py` takes from the server running on a synthetic HOME. The unit job also runs `i18n_check.py --strict`. Nothing needs the network beyond installing Python and Node.

## Conventions

- Keep to the standard library on the server side, and to plain scripts on the page side.
- The server reads and never writes transcripts. Anything that opens a file named by a request, or by a report path found in a transcript, goes through `open_safe` in `board/util.py`: the denial check is made again right before the open, symlinks are not followed, and only regular files open. Checks that only look at metadata use `stat_regular` (debate cells and final files from the brief table: existence only) or `stat_plain` (files found by listing a folder: dot paths and secret-looking names are skipped). The viewer adds `allowed_file` on top.
- A change to what a page reads (API field names) needs the matching change in `static/` and a test.
- Screen text goes in the dictionaries, not in the page code; a new key goes in every language file (English is the fallback). Write new comments and docstrings in English.
- Do not commit real transcripts, fixtures made from them, or screenshots of your own dashboard.
