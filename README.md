# Agent Bullpen

**English** · [한국어](README.ko.md)

A live, read-only office view of your Claude Code and Codex agents: who is working on what, how a debate is going, what it costs. It reads the transcripts the tools already write to disk. No hooks, no install, nothing to configure.

![Dashboard](docs/images/en/dashboard.png)

![Pixel-art office view](docs/images/en/office.png)

## What it shows

- **Orchestrator and agents**: status (working, done, stalled, ended), the tool each one is using right now, context fill, model and effort. Work that stopped is told apart: **interrupted** by a usage limit (with the reset time), an API error, a time limit or an early exit, or **unknown** when there is no process to check. A `claude -p` run started by an agent or by another run stands under the one that started it.
- **Debates**: a topic × round × participant table that follows `<topic>/r<N>/<who>.md` report files on disk, with the reports readable in the page.
- **A pixel-art office** (`/game` for full screen): agents sit at desks while they work, walk to the lounge when done, and carry messages to each other.
- **Conversations**: you ↔ orchestrator, and agent ↔ agent.
- **Alerts** for things that need you: a pending question, a stalled or failed agent, a usage limit (one alert for everything that waits on the same reset).
- **Tokens and cost** per agent (API-price equivalent, not your bill), an activity timeline, and a plan / usage bar.
- **Codex** next to Claude Code: `codex exec` runs that Claude launches join the team; Codex-only conversations are listed too.
- Plain Claude sessions with no agents are listed as well.
- **Diagnostics**: what the dashboard noticed but could not settle (a tie between two possible launchers, a path it could not work out, an unknown transcript format), in a list that never contains transcript text.

Every panel is explained in the [screen guide](docs/guide.md).

![Agent list: a run that launched two levels of runs, and runs that stopped (usage limit, time limit, early exit)](docs/images/en/agents.png)

![Debate table: topic × round × participant](docs/images/en/debates.png)

## Status / Support

| Platform | Status |
|---|---|
| Linux | Verified |
| macOS | Experimental. Verification on a Mac is pending; process detection falls back to `ps`, and `--claude-usage-api` normally has no login file to read there (see [Security & privacy](#security--privacy)). |
| WSL2 | Experimental (runs as Linux, not yet verified). |
| Windows (native) | Not supported. |

Requires **Python 3.9+**. The standard library only: nothing to `pip install`.

> **Languages.** The dashboard and the terminal output are in English by default, and Korean is available: pick it in the language selector in the header, add `?lang=ko` to the address, or let your browser's language decide. The terminal follows `--lang`, `AGENT_BULLPEN_LANG` or your locale (`LANG`). All the screen text lives in one dictionary file per language (`static/locales/<code>.json`), so adding a language is one JSON file (the server picks it up without a restart; reload the page): see [`docs/development.md`](docs/development.md). The documentation is in English (the [screen guide](docs/guide.md)) and Korean ([화면 설명서](docs/guide.ko.md)).

## Quickstart

```bash
git clone https://github.com/bemong1/agent-bullpen.git
cd agent-bullpen
python3 server.py
```

Open <http://localhost:8790>. Start (or already have) a Claude Code or Codex session and it appears; with no sessions yet the page tells you which folders it looked in and checks again every 5 seconds.

On start the server prints the address, the folders it reads and how many sessions it found:

```
Dashboard: http://localhost:8790/
  Claude Code   ~/.claude/projects           sessions 2 · with agents 1  (default)
  ◆ Codex       ~/.codex/sessions            last 7 days: 0  (default)
  Usage API off · process check /proc
Session a11ce000-0000-4000-8000-000000000001 loaded in 0.0s · agents 5
```

### Try it with sample data first

No transcripts of your own yet, or want to see a busy dashboard before pointing it at your work? Build a synthetic `HOME` and run the server on it:

```bash
python3 tools/synth_home.py /tmp/acme-home --busy --live   # made-up sessions (--busy: a full office); --live makes some show as working; add --stopped for stopped work
env -u CLAUDE_CONFIG_DIR -u CODEX_HOME HOME=/tmp/acme-home python3 server.py --port 8791
python3 tools/synth_home.py /tmp/acme-home --stop      # afterwards: stop the fake processes
```

Open <http://localhost:8791>. The demo scenarios (`/game?demo=all`) also play on this data.

### Viewing it from another device

Tunnel over SSH, then open `http://localhost:8790` on the device:

```bash
ssh -L 8790:localhost:8790 you@the-machine
```

The server itself keeps listening on loopback only. More (VPN addresses and their warnings): [`docs/remote.md`](docs/remote.md).

## Options

```
python3 server.py [options]
```

| Option | Default | Meaning |
|---|---|---|
| `--claude-config-dir DIR` | `$CLAUDE_CONFIG_DIR`, else `~/.claude` | Claude Code config folder; transcripts are read from `DIR/projects`. |
| `--codex-home DIR` | `$CODEX_HOME`, else `~/.codex` | Codex folder; transcripts are read from `DIR/sessions` (last 7 days). |
| `--port PORT` | `8790` | Port to listen on. If it is taken, the server says so in one line and exits. |
| `--host ADDR` | `127.0.0.1` | Address to listen on; may be repeated. Only loopback, `100.64.0.0/10` and `fd7a:115c:a1e0::/48` (the ranges Tailscale/NetBird-style VPNs use) are accepted; `0.0.0.0` and LAN addresses refuse to start. Anything non-loopback prints a "no authentication" warning. |
| `--allow-host NAME` | none | Extra names accepted in the `Host` header; may be repeated. A name (`my.box`) or a suffix (`.example.net`, which matches `a.example.net` but not `example.net`). |
| `--session ID` | most recently active orchestration, or the most recent plain conversation if there is none | Session (UUID) to open when the URL names none. |
| `--lang CODE` | `auto`: `AGENT_BULLPEN_LANG`, then `LC_ALL`, `LC_MESSAGES`, `LANG`, else `en` | Language of the terminal output and `--help` (`en`, `ko`). The screen language is separate: the selector in the header, `?lang=`, your browser. |
| `--claude-usage-api` | off | Query Anthropic's unofficial usage API for the Claude plan bar. See [Security & privacy](#security--privacy). |
| `--no-link-cache` | off | Do not keep the certain session links in `links.json` (see [Security & privacy](#security--privacy)). |

Details and precedence rules: [`docs/configuration.md`](docs/configuration.md).

## Security & privacy

- **Read-only.** It never writes to your Claude Code or Codex folders (`~/.claude` and `~/.codex` by default). It reads session transcripts, plus Claude Code's `.claude.json` for the plan tier and cached usage numbers (account id and email are never sent to the page). It does not open `auth.json`, `settings*.json`, `config.toml`, `history.jsonl` or the Codex sqlite files, and it opens `.credentials.json` only if you pass `--claude-usage-api` (below).
- **No authentication.** Anyone who can reach the port can read your conversations. By default it listens on `127.0.0.1` only. If you open it on another address, or add `--allow-host`, it prints a warning, and what happens next depends on your VPN or firewall, not on this program. Do not expose it to the internet.
- **No outbound requests by default.** The server makes no network calls and the pages load nothing from the internet (the font is bundled). The one file it writes by default is a small link record, `links.json` in `$XDG_CACHE_HOME/agent-bullpen/` (default `~/.cache/agent-bullpen/`, mode 0600): when a child session is tied to the session that started it by firm evidence (process lineage, an environment variable, an output file or a long instruction that matches), it keeps the two session ids with the rule and time (no paths, prompts or environment values), at most 2000 links for 90 days, so that a restart does not forget the link, and it creates nothing until such a link is found. Guesses are not kept. `--no-link-cache` turns that off.
- **`--claude-usage-api` is opt-in, unofficial, and at your own risk.** It reads the access token in `.credentials.json` in your Claude config folder (`~/.claude` by default, or `CLAUDE_CONFIG_DIR`) and calls an undocumented Anthropic endpoint (`api.anthropic.com/api/oauth/usage`) every 60 seconds; it can break or change without notice. The token is never shown, logged or sent anywhere else. It keeps the last usage numbers and reset times (no token, no account identity; mode 0600) in `$XDG_CACHE_HOME/agent-bullpen/usage.json` (default `~/.cache/agent-bullpen/`). Without the flag the bottom bar shows the cached values from `.claude.json`, labelled "recorded HH:MM · refresh with /usage", which Claude Code refreshes when you open `/usage`. If Claude Code keeps your login somewhere other than that file (on macOS it normally uses the Keychain), there is no token for the flag to read: the bar keeps showing the cached values with a yellow "No login found" note, and nothing is read from the Keychain.
- **The document viewer is narrow.** It opens only files the agents wrote and files inside debate folders (`.md`, `.txt`, `.yaml`, `.json`; regular files; up to 2 MiB). It never opens credential or settings files, anything under `~/.codex`, anything with a dot folder in its path (the one exception is a Claude Code worktree, `<repo>/.claude/worktrees/<name>/`, and even there `.env`, `.git` and other dot entries stay closed), or any file whose name looks like it holds a secret (`secret`, `credential`, `password`, `token`, `apikey`, `kubeconfig…`), `.md` included. The debate table is looser: it only checks that a report file exists, so a report under a hidden folder, or one named `token_budget.md`, still counts as submitted. It just cannot be opened from the page, so keep those words out of the names of reports you want to read there.
- **Host check.** Requests whose `Host` header is not on the allow list get a 403 (a defense against DNS rebinding).
- **Your transcripts are in the pictures.** A screenshot of your own dashboard shows your prompts. The screenshots in this repository come from synthetic data.

## Debate folders

A debate is recognized from report paths. Lay it out like this and tell your agents to write to these paths:

```
<topic>/
  brief.md          # topic brief: "# Title", and "**A — role**" lines per participant
  r1/A.md           # round 1, one report per participant
  r1/B.md
  r2/A.md           # round 2 …
  rulings.md        # the final result (see below)
```

- The folder must exist on disk. A report path is `<topic>/r<N>/<who>.md` (`r01` and `round1` work too), given as an absolute path (or `~/…`) in the agent's instructions, written by the agent, or named as the target of a command it ran. `<who>` is matched to the agent's tag (`A`, `T1-A`, `opus-1`) or to the file it wrote. An agent that only reads a report, a path that fits two debate folders, and one the instructions say not to write give no seat.
- With `brief.md` in the topic folder you also get the title and roles. A common `brief.md` in the parent folder groups several topics into one debate. A review with no round folders gets cells only when its `brief.md` names the reviewers and their result files.
- **Final result**, either way:
  - in the parent `brief.md` table, a row such as ``| Naming | `t1_naming/` | — | `final/t1_naming.md` |`` (columns: topic, folder, depends on, final deliverable; the folder must be a backticked `name/` and the deliverable a backticked `final/<file>`, and the first row is the header); or
  - automatic: with no such row, the newest `.md` directly in the topic folder that is at least as new as the last submitted report (`brief.md` and `round<N>.md` are skipped, names like `final`, `ruling`, `summary` win). It is ignored while any participant is still writing.

The full rules are in [`docs/guide.md`](docs/guide.md#how-debates-are-recognized).

## Demo

`http://localhost:8790/game?demo=all` plays every scenario on the office view (`basic`, `new`, `round2`, `trouble`, `talk`; `/?demo=<name>` on the dashboard). The demo runs only in your browser and changes nothing, but it draws over a session that is open, so you need at least one session loaded. With none, you get the diagnostic panel instead.

## Troubleshooting

**Blank page, or "No sessions to open yet".** Not an error: the panel lists the folders it read, where each came from (default, environment variable or flag) and how many sessions it found, and rechecks every 5 seconds.

![Diagnostic panel](docs/images/en/empty.png)

- *Folder not found* → your transcripts are elsewhere: start with `--claude-config-dir <dir>` / `--codex-home <dir>`, or set `CLAUDE_CONFIG_DIR` / `CODEX_HOME`.
- *Sessions 0* → the folder is right, but no conversation has been written yet. Codex only lists the last 7 days.
- Sessions exist but not the one you want → open it with `?session=<id>` in the URL (`--session` takes the same UUID). Conversations with no agents are listed apart from the orchestrations: in the "other" menu of the orchestrator card, or in the group "Plain conversations, no agents" of the project selector.

**An agent says "Interrupted" or "Unknown", or a run is missing from the agent list.** Interrupted is work that stopped on a usage limit (the card says when it resets), an API error, a time limit or an early exit. Unknown means there is no process to check and the records went quiet. A `claude -p` run that could not be tied to the session that started it is not an agent: it is in the folded group "N child runs not linked" with the reason. The Diagnostics chip in the header lists everything the dashboard could not settle (see the [screen guide](docs/guide.md#diagnostics)).

**Port in use.**

```
Couldn't start the dashboard on 127.0.0.1:8790 — the port is already in use. If it is a dashboard you already started, open http://localhost:8790/. For another port, pass --port 8791.
```

An old dashboard is probably still running; open it, or pick another `--port`.

**403 "Host not allowed".** You reached the server by a name it does not know (a VPN name, a reverse proxy). Restart with `--allow-host <that name>` or a suffix such as `--allow-host .example.net`. The 403 page tells you the exact flag.

**The screen or the terminal is in the wrong language.** The screen follows `?lang=`, then your last pick in the header selector, then your browser's languages, then English; the terminal follows `--lang`, then `AGENT_BULLPEN_LANG`, `LC_ALL`, `LC_MESSAGES`, `LANG`, then English (details: [screen guide](docs/guide.md#language-of-the-screen-and-of-the-terminal)).

**A server that will not start with `--host`.** `--host` accepts loopback and VPN ranges only. For another device use [an SSH tunnel](#viewing-it-from-another-device).

## Development

Tests are standard-library `unittest`; the layout and the regression tools are in [`docs/development.md`](docs/development.md).

```bash
python3 -m unittest discover -s tests
```

## Disclaimer

Unofficial — not affiliated with Anthropic or OpenAI. "Claude", "Claude Code", "Codex" and "OpenAI" are trademarks of their respective owners. The monitor symbols in the office view (`>_`, ◆) are neutral marks, not logos.

## License

[MIT](LICENSE). The bundled Galmuri font is under the SIL Open Font License; see [`THIRD_PARTY.md`](THIRD_PARTY.md).
