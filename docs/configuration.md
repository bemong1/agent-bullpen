# Configuration

Agent Bullpen has no config file. Everything is a command-line option, and the two folders it reads also follow the standard environment variables of the tools that write them.

```
python3 server.py [--claude-config-dir DIR] [--codex-home DIR] [--port PORT] [--host ADDR]...
                  [--token VALUE | --no-auth] [--allow-host NAME]... [--session ID]
                  [--lang CODE] [--no-link-cache] [--version]
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

It never opens `auth.json`, `.credentials.json`, `config.toml`, `history.jsonl`, Codex sqlite files or `settings*.json`: no code of the server reads a login or a token of yours, and it makes no network request. Besides those, it reads `statusline.json` in its cache folder if the optional `statusline.py` command has left one ([below](#statuslinepy-plan-usage-from-the-status-line)).

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
  ◆ Codex       ~/codex-cfg/sessions         standalone sessions, last 7 days: 0  (--codex-home)
```

The label at the end is `default`, the environment variable name (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`) or the option name. A folder that does not exist reads `folder not found`. The page shows the same thing in its diagnostic panel when there is nothing to open. The lines above are English, the default; with `--lang ko` or a Korean locale the same lines are in Korean.

## Options

| Option | Default | Notes |
|---|---|---|
| `--claude-config-dir DIR` | see above | Transcripts are read from `DIR/projects`. |
| `--codex-home DIR` | see above | Transcripts are read from `DIR/sessions`. |
| `--port PORT` | `8790` | If the port is taken, one line is printed (with a `--port` suggestion) and the process exits with status 1. Nothing is scanned before the port is bound. |
| `--host ADDR` | `127.0.0.1` | Repeatable (one listener per address). Any IP address (`0.0.0.0`, `::`, a LAN or VPN address) or a host name, with no port; `[::1]` with brackets is fine. A loopback address (`127.0.0.0/8`, `::1`, `localhost`) has no login; every other address requires an access token, which the server makes at start and prints (see [remote.md](remote.md)). An address or name that cannot be opened ends the start with a one-line message. `--host ::1` opens an IPv6 listener; use `http://[::1]:8790/`. `--host ::` answers IPv4 clients too. |
| `--token VALUE` | a new random token at every start | The access token to use instead: at least 16 characters, letters, digits and `. _ ~ -`. The environment variable `AGENT_BULLPEN_TOKEN` does the same and the option wins. A token given this way turns the check on for every listener, loopback included. The cookie a browser gets is tied to one start of the server, so after a restart open the address with `?token=` once more. It is visible in the process list when given as an option; prefer the variable. |
| `--no-auth` | off | No token check, even on a non-loopback address. It wins over `AGENT_BULLPEN_TOKEN`, cannot be combined with `--token`, and prints a framed warning on stderr at start (the addresses open without a login). On loopback it changes nothing. |
| `--allow-host NAME` | none | Repeatable. Only matters where there is no token check (loopback, or `--no-auth`); with a token every `Host` name is accepted. Adds a name accepted in the `Host` header: a full name (`my.box`) or a suffix with a leading dot (`.example.net`, also written `*.example.net`). A suffix does not match the bare domain. The port in the header is ignored, and IP addresses are compared in canonical form (`fd7a:115c:a1e0:0:0:0:0:1` equals `[fd7a:115c:a1e0::1]`). Always accepted: `localhost`, `127.0.0.1`, `::1`, and each address given to `--host`. There is no built-in suffix. A non-loopback name prints the same framed warning as `--no-auth` on a non-loopback `--host`. |
| `--session ID` | most recently active orchestration, else the most recent plain conversation | UUID of the session to open when the URL has no `?session=`. A malformed id, or an id with no transcript, stops the server with an error. |
| `--lang CODE` | `auto` | Language of the terminal output and `--help`. See [Language](#language). |
| `--no-link-cache` | off | Do not keep the certain session links in `links.json`. See [What the server writes](#what-the-server-writes). |
| `--version` | | Prints `Agent Bullpen <version>` (the version is written once, in `board/__init__.py`) and exits. |

### `statusline.py`: plan usage from the status line

Claude Code can run a command to draw its status line and pipes a JSON document to that command's stdin. Claude Code puts your 5-hour and weekly usage (`rate_limits`) in it, worked out from its own API responses. `statusline.py` (at the top of the repository, standard library only, it does not import the dashboard) keeps just those numbers in a small file, and the server reads that file. There is no extra request, no token is read, and the server needs no option.

**Set it up.** Run `python3 statusline.py --print-config`. It prints the `statusLine` piece to merge into `~/.claude/settings.json` (`CLAUDE_CONFIG_DIR` is followed), and keeps your current status line, if you have one, as the chain so it does not disappear. It only reads the settings file and prints; it never edits it. You merge the piece yourself. For example, with no status line yet:

```json
{
  "statusLine": { "type": "command", "command": "python3 /path/to/agent-bullpen/statusline.py" }
}
```

and with an existing one (`~/.claude/my-statusline.sh` here), which keeps drawing exactly as before:

```json
{
  "statusLine": { "type": "command", "command": "python3 /path/to/agent-bullpen/statusline.py --chain '~/.claude/my-statusline.sh'" }
}
```

If another tool manages your status line, give its command to `--chain` in the same way. Other keys of the `statusLine` object (such as `padding`) stay as they were.

| Option | Meaning |
|---|---|
| `--chain CMD` | Also runs `CMD` through the shell with the same stdin and prints its output as it is. The environment variable `AGENT_BULLPEN_STATUSLINE_CHAIN` does the same, and the option wins. `CMD` gets 5 seconds by default; on a timeout it is killed (with anything it started) and nothing is printed. A failing `CMD` still has its output printed. |
| `--chain-timeout SEC` | Another time limit for `CMD`. |
| `--show` | With no chain, draws a short usage text (`5h 42% · 7d 18%`). Without it the command prints nothing. |
| `--print-config` | Prints the settings piece described above and exits. |

The command never fails: whatever it is given, it exits with status 0 and does not print an error. It reads at most 1 MiB of stdin (a real input is a few KiB); a bigger input is not looked at, but is still handed whole to the chain. It is light: about 12 ms per call on a typical Linux machine, most of it Python's own start-up.

**What it writes.** `$XDG_CACHE_HOME/agent-bullpen/statusline.json` (`~/.cache/agent-bullpen/statusline.json` when that variable is unset; the folder is created with mode 0700 and the file with 0600, replaced atomically). It refuses to write into a folder or over a file that belongs to someone else or that others can write, and a symbolic link at the file is never followed: nothing is written through it. An unchanged reading is not written again within 30 seconds. The file holds this and nothing else:

```json
{"v":1,"at":1790906121.178,"rate_limits":{"five_hour":{"used_percentage":42.3,"resets_at":1790910000},"seven_day":{"used_percentage":18.0,"resets_at":"2026-10-08T03:00:00.000Z"}}}
```

`at` is when the command received the reading. Of `rate_limits` only `five_hour` and `seven_day` (and, with a gateway account, `spend_limit` with its percentage, reset time and dollar amounts) are kept, and in each only numbers and a date-time text of a fixed shape: any other text in the input, such as a path, a folder, a session id, the model, the cost, an instruction or something shaped like a token, is dropped, and so is a window whose values are not of that shape. An input without usable `rate_limits` (for example before Claude Code has any numbers, or for an account that has no such limits) writes nothing and leaves the last reading in place.

**What the server does with it.** It only reads the file, and only if it is a regular file (not a link) of yours that no one else can write, in a folder of yours that no one else can write, up to 64 KiB, in the shape above; otherwise the file is ignored. The bottom bar shows its numbers in gray as "status line · HH:MM" (the time is `at`). The Claude usage comes from the first source that has it:

1. the status line file,
2. the cache in `.claude.json` (Claude Code refreshes it when you open `/usage`), and the limit hits found in the transcripts.

A status line reading is used while it is less than 10 minutes old, even if the cache has a newer one, so the bar does not flip between sources. After that the newest of the two is used, so if Claude Code is closed and you later open `/usage`, the fresher cache is shown; with nothing newer, the old status line reading stays (with its time). A window whose reset time has passed shows `—` (and for the week the estimated reset time) instead of the old percentage. The plan tier, the model weeks (such as Sonnet) and extra usage always come from `.claude.json`, which the status line does not carry: with the status line as the source they follow the 5-hour and weekly values with their own "recorded HH:MM" (the time that cache was written), and they are left out when that time is not known, so a value from the cache never sits under the status line's time.

**Without it** the bottom bar shows the values Claude Code cached in `.claude.json` ("recorded HH:MM · refresh with /usage"), or one hint line ("Open /usage in Claude Code to see this") when there is no cache yet. Those numbers change when you open `/usage`; the status line ones change whenever a session draws its status line. Nothing asks Anthropic for your usage: the dashboard has no code that reads your login or makes a request.

The numbers are those of the Claude Code session that last drew its status line, and each session reports what it has seen last, so when several sessions run the one that drew last wins. Claude Code includes a window only while its reset time is still ahead, so after a reset the file stays as it was until a session reports again (the bar shows `—` for that window in the meantime).

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

Never to your Claude Code or Codex folders or to any transcript. It keeps at most one small file of its own, in `$XDG_CACHE_HOME/agent-bullpen/` (`~/.cache/agent-bullpen/` when that variable is unset; the folder is created with mode 0700 and the files with 0600). A second file in the same folder, `statusline.json`, is written by the optional `statusline.py` command, not by the server, which only reads it:

| File | When | What it holds |
|---|---|---|
| `links.json` | By default, once a certain link is found (process lineage, an environment variable, an output file, a long matching instruction). `--no-link-cache` turns it off | For each link the session ids (child, parent), the rule (`proc`, `env`, `out` or `content`), the time it was first seen and, for a child of an agent, that agent's id. No paths, prompts, text fingerprints or environment values. At most 2000 links and 90 days, the oldest dropped first |
| `statusline.json` | Only if you register `statusline.py` as your Claude Code status line; written by that command each time Claude Code draws its status line and the usage has changed (or after 30 seconds) | The 5-hour and weekly usage percentages, their reset times and the time received ([details](#statuslinepy-plan-usage-from-the-status-line)) |

`links.json` lets a restart remember which `claude -p` or Codex session belongs to which parent, as the screen guide describes ([child runs](guide.md#child-runs-started-from-bash)). It is read when the server starts (up to 2 MiB), and ignored if it is not a regular file, belongs to another user, is writable by others or is malformed; the dashboard then lists `cache_error` in its Diagnostics. A remembered link shows on screen as "saved link" or "remembered link" and counts as certain, but it never overrides what the transcripts or the processes say. Links made by a guessing rule (a short instruction, a launching script that could not be read, time and working folder) are not kept. With `--no-link-cache` the file is neither read nor written and its folder is not created. On an empty or fresh `HOME` nothing is created until a link is found. The file has a version number (now 2); this version reads version 1 files too, but a server from before the version 2 format ignores a version 2 file and loses the remembered links.

## Environment variables

| Variable | Effect |
|---|---|
| `CLAUDE_CONFIG_DIR` | Claude Code config folder (below an option). Also moves `.claude.json` into it. |
| `CODEX_HOME` | Codex folder (below an option). |
| `XDG_CACHE_HOME` | Parent of the `agent-bullpen/` folder that holds `links.json` (the certain session links; off with `--no-link-cache`), and `statusline.json` (left by `statusline.py`). |
| `AGENT_BULLPEN_TOKEN` | The access token to use (same as `--token`, which wins); see [remote.md](remote.md). Turns the check on for loopback listeners too; `--no-auth` overrides it. |
| `AGENT_BULLPEN_STATUSLINE_CHAIN` | Read by `statusline.py`, not by the server: the command it chains, if `--chain` is not given. |
| `AGENT_BULLPEN_LANG` | Language of the terminal output and `--help` (`en`, `ko`), below `--lang` and above the locale variables. |
| `LC_ALL`, `LC_MESSAGES`, `LANG` | The locale variables, read for the terminal language only when `--lang` and `AGENT_BULLPEN_LANG` give none. |
| `AGENT_BULLPEN_LOG` | Debug switch, off by default. Any non-empty value makes the server print one line per request to stdout: the client address, the request line and the status, e.g. `127.0.0.1 "GET /api/plans HTTP/1.1" 200 -`. Request lines contain session ids and the paths of documents you open, so do not post this output publicly. A `token=` value in a request line is replaced by `…`. |

Apart from these, the server only uses `HOME` (where `~` points, so where the default folders and `~/.claude.json` are) and, on systems without `/proc`, `PATH` to find `ps`. The server has no other `AGENT_BULLPEN_*` settings.

## Process detection

Whether a session's process is alive decides "working" versus "ended". On Linux this uses `/proc`; elsewhere (macOS) it runs `ps -axww -o pid=,command=` (cached for 3 seconds). If neither works the answer is "unknown", and an unknown process is never treated as ended: the dashboard falls back to whether the transcript is still growing, and its Diagnostics lists `proc_unknown`. The start-up line says which method is in use: `process check /proc`, `process check ps (cannot see files Codex has open)` or `process check none (session state shows as unknown)`.
