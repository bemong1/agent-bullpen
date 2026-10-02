# Configuration

Agent Bullpen has no config file. Everything is a command-line option, and the two folders it reads also follow the standard environment variables of the tools that write them.

```
python3 server.py [--claude-config-dir DIR] [--codex-home DIR] [--port PORT] [--host ADDR]...
                  [--allow-host NAME]... [--session ID] [--lang CODE]
                  [--claude-usage-api] [--no-link-cache]
```

Options must be spelled out in full (`--allow` is rejected, not read as `--allow-host`).

## Where it reads

The folder for each tool is chosen once, at start, in this order:

| | 1. option | 2. environment variable | 3. default |
|---|---|---|---|
| Claude Code | `--claude-config-dir DIR` | `CLAUDE_CONFIG_DIR` | `~/.claude` |
| Codex | `--codex-home DIR` | `CODEX_HOME` | `~/.codex` |

An empty value counts as not set. `~` is expanded and relative paths are made absolute.

What is read under those folders (read-only, never written):

| Path | Used for |
|---|---|
| `<claude>/projects/<project>/<session>.jsonl` and `<session>/subagents/agent-*.jsonl`, `*.meta.json` | sessions, agents, conversations, tokens |
| `<claude>/sessions/<pid>.json` | which Claude sessions have a live process |
| `<codex>/sessions/**/rollout-*.jsonl` | Codex threads (the last 7 days are listed) |
| `<codex>/session_index.jsonl` | Codex thread names |
| `.claude.json` (next section) | plan tier and cached usage numbers only |

It never opens `auth.json`, `config.toml`, `history.jsonl`, Codex sqlite files or `settings*.json`. `<claude>/.credentials.json` is opened only with `--claude-usage-api`.

### Where `.claude.json` is

Claude Code keeps `.claude.json` outside the `projects` folder, and where it sits depends on how you pointed at the config folder. Agent Bullpen follows the same rule:

| How the Claude folder was chosen | `.claude.json` read from |
|---|---|
| default (nothing set) | `~/.claude.json` |
| `CLAUDE_CONFIG_DIR=DIR` | `DIR/.claude.json` |
| `--claude-config-dir DIR` | `DIR/.claude.json`; if `DIR` is named `.claude` and that file is missing, the one next to it (`DIR/../.claude.json`) |

If the file is not there, the plan bar simply has no Claude plan values.

The startup output shows what was chosen and where each value came from:

```
  Claude Code   ~/claude-cfg/projects        sessions 2 · with agents 1  (--claude-config-dir)
  ◆ Codex       ~/codex-cfg/sessions         last 7 days: 0  (--codex-home)
```

The label at the end is `default`, the environment variable name (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`) or the option name. A folder that does not exist reads `folder not found`. The page shows the same thing in its diagnostic panel when there is nothing to open. The lines above are English, the default; with `--lang ko` or a Korean locale the same lines are in Korean.

## Options

| Option | Default | Notes |
|---|---|---|
| `--claude-config-dir DIR` | see above | Transcripts are read from `DIR/projects`. |
| `--codex-home DIR` | see above | Transcripts are read from `DIR/sessions`. |
| `--port PORT` | `8790` | If the port is taken, one line is printed (with a `--port` suggestion) and the process exits with status 1. Nothing is scanned before the port is bound. |
| `--host ADDR` | `127.0.0.1` | Repeatable (one listener per address). Accepted: `127.0.0.0/8`, `::1`, `localhost`, `100.64.0.0/10`, `fd7a:115c:a1e0::/48`. Anything else, including `0.0.0.0` and LAN addresses, is refused at start. A non-loopback address prints a "no authentication" warning on stderr. `--host ::1` opens an IPv6 listener; use `http://[::1]:8790/`. |
| `--allow-host NAME` | none | Repeatable. Adds a name accepted in the `Host` header: a full name (`my.box`) or a suffix with a leading dot (`.example.net`, also written `*.example.net`). A suffix does not match the bare domain. The port in the header is ignored, and IP addresses are compared in canonical form (`fd7a:115c:a1e0:0:0:0:0:1` equals `[fd7a:115c:a1e0::1]`). Always accepted: `localhost`, `127.0.0.1`, `::1`, and each address given to `--host`. There is no built-in suffix. A non-loopback name prints the same warning as a non-loopback `--host`. |
| `--session ID` | most recently active orchestration, else the most recent plain conversation | UUID of the session to open when the URL has no `?session=`. A malformed id, or an id with no transcript, stops the server with an error. |
| `--lang CODE` | `auto` | Language of the terminal output and `--help`. See [Language](#language). |
| `--claude-usage-api` | off | See below. |
| `--no-link-cache` | off | Do not keep the certain session links in `links.json`. See [What the server writes](#what-the-server-writes). |

### `--claude-usage-api`

Off by default. Without it the server reads no credentials, makes no network call and writes no usage file; the Claude part of the bottom bar shows the values Claude Code cached in `.claude.json` ("recorded HH:MM · refresh with /usage"), or one hint line ("Open /usage in Claude Code to see this") if there is no cache yet.

With it, every 60 seconds the server reads the access token from `.credentials.json` in the Claude config folder (`<claude>`: `~/.claude` by default, or `CLAUDE_CONFIG_DIR` / `--claude-config-dir`) and calls Anthropic's internal usage endpoint (`https://api.anthropic.com/api/oauth/usage`, the one behind `/usage` in Claude Code). It is unofficial and undocumented, may change or stop working, and is used at your own risk. Details:

- A notice is printed on stderr at start.
- The token is never shown in the page, the API responses or the logs, and is never refreshed by this program.
- **macOS and other setups without a `.credentials.json`.** The server reads that one file and nothing else. If Claude Code keeps your login elsewhere (on macOS it normally uses the Keychain), there is no token to read: the bottom bar keeps showing the cached values from `.claude.json` ("recorded HH:MM") with a yellow "No login found" note, and no request is made. The Keychain is never read. In that case simply leave the flag off.
- The last numbers (usage values, reset times and the time of the last query; no token, no identity) are kept in `$XDG_CACHE_HOME/agent-bullpen/usage.json`, or `~/.cache/agent-bullpen/usage.json` if that variable is unset. The file is created with mode 0600, and only after the API has actually answered: with no usable login there is no call and no file.

## Language

The terminal output (start-up summary, error and warning lines, `--help`) and the screen have separate languages. English is the default for both, and Korean is the other supported language.

**Terminal**, in this order: `--lang CODE`, `AGENT_BULLPEN_LANG`, `LC_ALL`, `LC_MESSAGES`, `LANG`, English. The first one that has a value decides:

| Setting | Language |
|---|---|
| `--lang ko` | Korean |
| `AGENT_BULLPEN_LANG=ko` (with `LANG=C.UTF-8`) | Korean |
| `LANG=ko_KR.UTF-8` | Korean |
| `LC_ALL=C LANG=ko_KR.UTF-8` | English: `LC_ALL` comes first and `C` is not a supported language, so the next variable is not tried |
| `LANG=fr_FR.UTF-8` | English (unsupported language) |
| nothing set | English |

An empty value and `auto` count as not set. `--lang` takes any code that has a dictionary file in `static/locales/`; an unknown code gives English. `--help` follows the same language (`python3 server.py --help --lang ko`), except the sentences `argparse` prints itself (`error: unrecognized arguments` and the like), which are always English. The HTTP 403 text has English on its first line and Korean on its second, whatever the setting.

**Screen**, in this order: `?lang=<code>` in the address, the language last picked in the header selector (stored in this browser), the browser's language list, English. There is no server-side option for it; see the [screen guide](guide.md#language-of-the-screen-and-of-the-terminal). A language is added with one JSON file: [development](development.md#adding-a-language).

## What the server writes

Never to your Claude Code or Codex folders or to any transcript. It keeps at most two small files of its own, both in `$XDG_CACHE_HOME/agent-bullpen/` (`~/.cache/agent-bullpen/` when that variable is unset; the folder is created with mode 0700 and the files with 0600):

| File | When | What it holds |
|---|---|---|
| `links.json` | By default, once a certain link is found (process lineage, an environment variable, an output file, a long matching instruction). `--no-link-cache` turns it off | For each link the session ids (child, parent), the rule (`proc`, `env`, `out` or `content`), the time it was first seen and, for a child of an agent, that agent's id. No paths, prompts, text fingerprints or environment values. At most 2000 links and 90 days, the oldest dropped first |
| `usage.json` | Only with `--claude-usage-api`, after the API has actually answered | Usage values, reset times and the time of the last query |

`links.json` lets a restart remember which `claude -p` or Codex session belongs to which parent, as the screen guide describes ([child runs](guide.md#child-runs-started-from-bash)). It is read when the server starts (up to 2 MiB), and ignored if it is not a regular file, belongs to another user, is writable by others or is malformed; the dashboard then lists `cache_error` in its Diagnostics. A remembered link shows on screen as "saved link" or "remembered link" and counts as certain, but it never overrides what the transcripts or the processes say. Links made by a guessing rule (a short instruction, a launching script that could not be read, time and working folder) are not kept. With `--no-link-cache` the file is neither read nor written and its folder is not created. On an empty or fresh `HOME` nothing is created until a link is found. The file has a version number (now 2); this version reads version 1 files too, but a server from before the version 2 format ignores a version 2 file and loses the remembered links.

## Environment variables

| Variable | Effect |
|---|---|
| `CLAUDE_CONFIG_DIR` | Claude Code config folder (below an option). Also moves `.claude.json` and `.credentials.json` into it. |
| `CODEX_HOME` | Codex folder (below an option). |
| `XDG_CACHE_HOME` | Parent of the `agent-bullpen/` folder that holds `links.json` (the certain session links; off with `--no-link-cache`) and `usage.json` (only with `--claude-usage-api`). |
| `AGENT_BULLPEN_LANG` | Language of the terminal output and `--help` (`en`, `ko`), below `--lang` and above the locale variables. |
| `LC_ALL`, `LC_MESSAGES`, `LANG` | The locale variables, read for the terminal language only when `--lang` and `AGENT_BULLPEN_LANG` give none. |
| `AGENT_BULLPEN_LOG` | Debug switch, off by default. Any non-empty value makes the server print one line per request to stdout: the client address, the request line and the status, e.g. `127.0.0.1 "GET /api/plans HTTP/1.1" 200 -`. Request lines contain session ids and the paths of documents you open, so do not post this output publicly. |

Apart from these, the server only uses `HOME` (where `~` points, so where the default folders and `~/.claude.json` are) and, on systems without `/proc`, `PATH` to find `ps`. There are no other `AGENT_BULLPEN_*` settings.

## Process detection

Whether a session's process is alive decides "working" versus "ended". On Linux this uses `/proc`; elsewhere (macOS) it runs `ps -axww -o pid=,command=` (cached for 3 seconds). If neither works the answer is "unknown", and an unknown process is never treated as ended: the dashboard falls back to whether the transcript is still growing, and its Diagnostics lists `proc_unknown`. The start-up line says which method is in use: `process check /proc`, `process check ps (cannot see files Codex has open)` or `process check none (session state shows as unknown)`.
