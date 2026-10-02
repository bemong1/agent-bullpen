# Screen guide

**English** · [한국어](guide.ko.md)

What each part of the screen means. For installing and running see the [README](../README.md); for options see [configuration](configuration.md).

The dashboard draws what Claude Code and Codex leave on disk and only **reads** it. It needs no hooks, and sessions that are already running show up right away.

| Address | Screen |
|---|---|
| `/` | Dashboard |
| `/game` | Pixel-art office, full screen |
| `/?session=<session id>` | Open that session (same for `/game`) |
| `/?session=<session id>#agent=<agent id>` | Open that agent's details panel |
| `/?demo=<name>`, `/game?demo=<name>` | Play a demo (see "Demo" below) |
| `/?lang=<code>` | Pick the screen language (`en`, `ko`; same for `/game`; combines with other parameters such as `session` and `demo`; see "Language of the screen and of the terminal" below) |

`/api/…` addresses are internal to the screen and may change without notice.

## Screen layout

| Area | What it shows |
|---|---|
| Header | Project selector, whether the session process is alive, counts of working · done agents, and chips that appear only when there is something to count: possibly stalled · interrupted · unknown · failed agents, and Diagnostics (it opens the list, see "Diagnostics"). Then the theme and language selectors |
| Alerts | Only what needs your judgment (see "Alerts"). Hidden when empty. The count is in the header chip and in the browser tab title as `(N)` |
| Agent office | The pixel-art strip (see "Office view"). Collapsed at first when the window is narrower than 1500px |
| Orchestrator | Working / waiting for results / waiting for a usage limit to reset, last action, current context |
| Token usage | Per provider (Claude Code / ◆ Codex): summary tiles (cost, input, output, agent count) and a table (Orchestrator / Working / Finished agents). The footer has cache, calls and per-model figures plus what only that provider has (Claude advisor model; Codex reasoning, approval review and weekly limit) |
| Progress | One line per topic: which round, how many submitted, who is left / the next topic and what it waits for / stall warnings |
| You ↔ Orchestrator | Your instructions (purple, right) and the orchestrator's reports (left) as speech bubbles in time order, including choice questions and the answer you picked. Click a bubble for the full text, "View all" for a wide window, "Load earlier" for 80 more. Scrolled to the bottom it follows new messages; reading further up it only shows "New messages ↓". A usage limit or an API error appears there as a faint centered line (see "System lines"), not as a bubble |
| Agent talk | The card to the right of the office card (see "Agent talk card") |
| Debates | Topic cards: Brief → Round 1 → Round 2 → Final steps and a participant × round table. Working agents that are in no debate cell go to the "Other work" card at the end |
| Agents | Live cards: the tool and target in use right now, activity over the last 30 minutes, tool call count, context, model and effort. A run that stopped on a limit or an error stays here with its reason; a run launched by another agent stands under it, one step in; a folded group "N child runs not linked" at the end lists runs that could not be tied to this session (see "Child runs started from Bash") |
| Activity timeline | One row per agent with tool calls (read · write · command · web), instructions received, reports saved and final reports, in time order |
| Message flow | Instructions, messages and final reports between you, the orchestrator and the agents, with filters, plus faint system lines (see "System lines") |
| Bottom bar | Plan and usage limits (see "Plan and usage limits"). Yellow from 70%, red from 90% or when a limit is reached |

Click an agent card or an agent row in a table to open the agent details panel on the right: first instruction, messages received, final report, activity, and the documents it wrote and read. Click a person on `/game` to open the dashboard's details panel. If the server does not answer within 30 seconds the screen shows "Disconnected" and keeps retrying.

## Language of the screen and of the terminal

The screen (dashboard and `/game`) supports English and Korean. The screen language is chosen in this order:

1. `?lang=<code>` in the address (`en`, `ko`). An unknown code is ignored and not stored.
2. The value picked in the language selector in the header. Only what you picked yourself is remembered, in this browser.
3. The first supported language in the browser's language list (`navigator.languages`).
4. English.

- The selector (`English` · `한국어`) sits next to the theme button on the dashboard and next to the "Dashboard ↩" link on `/game`; it is also shown on the diagnostic panel. Picking a language stores it and reloads the screen with `?lang=` in the address, keeping `session`, `demo` and the `#agent=…` hash. The links between the dashboard and `/game` carry `?lang=` along. With only one language installed the selector is hidden.
- Only the text of the screen is translated: titles, buttons, legends, alerts, the diagnostic panel, demo captions and date formats. Your instructions, reports, agent descriptions, model names and paths are shown exactly as recorded.
- The rules that decide alerts understand English and Korean sentences whatever the screen language is, so the same transcript raises the same alerts in either language.

The language of the terminal output (start-up summary, error messages, warnings, `--help`) is chosen in this order:

1. `--lang <code>`
2. the environment variable `AGENT_BULLPEN_LANG`
3. `LC_ALL`
4. `LC_MESSAGES`
5. `LANG`
6. English

- The first variable that has a value wins: `LC_ALL=C LANG=ko_KR.UTF-8` gives English. An empty value and `auto` count as not set; an unsupported language (`fr_FR`) gives English and does not fall through to the next variable.
- `--help` follows the same language, e.g. `python3 server.py --help --lang ko`. Sentences that `argparse` itself prints (`error: unrecognized arguments` and the like) are always English.
- The 403 message has English on the first line and Korean on the second.
- One file `static/locales/<code>.json` is one language. How to add a language: [development](development.md).

## First screen and the diagnostic panel

When there is no session to open, the screen shows a **diagnostic panel** instead of an error ("No sessions to open yet"). It has one line per provider (Claude Code, ◆ Codex).

```
Claude Code  ~/.claude/projects   default   Folder not found
◆ Codex      ~/.codex/sessions    default   0 sessions in the last 7 days
```

| Line | Meaning and what to do |
|---|---|
| Folder read | `projects` (Claude) / `sessions` (Codex) under the config folder |
| Source | `default`, `env CLAUDE_CONFIG_DIR` · `CODEX_HOME`, `flag --claude-config-dir` · `--codex-home` |
| Folder not found | Normal if you have not used that tool. If your transcripts are elsewhere, tell the server with the option or environment variable above |
| 0 sessions | The folder exists but holds no conversation. Start one and it appears within 5 seconds. Codex only looks at the last 7 days |

- It re-checks every 5 seconds and opens a session by itself as soon as there is one.
- Instead of the folder list you may see: "Can't reach the server" when the server is off or the tunnel is down; "The server isn't responding" when the connection works but nothing answers for over 10 seconds (the server is busy reading transcripts or has stopped); the 403 text (with the fix) when the Host is not allowed; "Couldn't open the session" when the session id in the address is not found. The first two retry after 5 seconds.
- A break after a session has opened shows as "Disconnected" in the header.

## Choosing a project and session

- The dropdown at the top has one line per project (working folder); picking one opens that project's **primary session**.
- An **orchestration** is a Claude session that has agents, or a Codex-only conversation. A Claude session without agents is a **plain conversation**.
- Primary: among the orchestrations, the session with the most agents that moved in the last 2 minutes (ties go to the most recent). Once chosen it stays until that session's process ends or 30 minutes pass without activity. A project with no orchestration uses its most recent plain conversation.
- A project whose primary is a plain conversation appears in the dropdown group "Plain conversations, no agents", with "Chat ·" in front. Such a conversation shows "Plain conversation, no agents" on the orchestrator card and an explanation in the Debates area.
- Other sessions of the same project are in the "N other · M chats ▾" menu of the orchestrator card, listed by task title (orchestrations first, plain conversations in a section after them).
- The list holds the 30 most recent Claude sessions with agents, Codex sessions of the last 7 days, and the 30 most recent plain conversations. A session outside the list still opens through `?session=<id>`.
- With no session in the address, the primary session of the most recent project whose primary is an orchestration opens; if there is none, the most recent plain conversation (or the `--session` you gave).

## Alerts

| Level | When | Basis |
|---|---|---|
| Needs decision | The orchestrator asked a choice question (AskUserQuestion) and there is no answer yet | Read from the transcript |
| Needs decision | The orchestrator ended its turn and its last two lines ask a question or request a decision ("?", "let me know", "would you like", "should I", "please confirm", and the Korean equivalents such as `원하시면`) | Guessed from the wording |
| Needs attention | An agent report of the last 12 hours has a line such as "user decision", "needs your approval", "decision needed" (the line is shown), or its Korean equivalent | Read from the transcript |
| Needs attention | An agent is possibly stalled, or failed or was stopped within the last 24 hours | Status rules |
| Needs attention | Agents or the orchestrator stopped on a usage limit: one alert for everything that shares the reset time ("Usage limit reached — resets at 15:03", or "reset time unknown" when the record has none) | Read from the transcript |
| Needs attention | A Codex agent is working and the Codex weekly limit (account-wide) is at 90% or more, or reached | Read from the transcript |
| Your turn | No agent is running and the orchestrator ended its turn. Not shown while the orchestrator is waiting out a limit that resumes by itself | Read from the transcript |
| Note | The usage limit alert above when the run resumes automatically after the reset ("… resumes automatically at 15:03"). Nothing is asked of you | Read from the transcript |

- "Dismiss" hides that alert in this browser only. You answer in the window where the orchestrator runs (a terminal and the like).
- A run that stopped on an API error, a time limit or an early exit, an ended run and an unknown state raise no alert of their own; they show in the agent list and in the header chips (see "Status rules").
- The rules that guess from wording can be wrong; they may miss things or catch too many.
- English is caught the same way. The target is a sentence that asks you for a choice, an answer or an approval (`?`, `let me know`, `would you like…`, `should I`, `please confirm/approve/choose`, `needs your approval`, `decision needed`). Past remarks ("I approved"), plain guidance ("if you want to change the port, pass --port") and negations ("does not need your approval") are not caught.

## Agent talk card

The card to the right of the office card (380px wide; below the office when the window is under 1300px). It uses the same bubbles as "You ↔ Orchestrator" and shows only six kinds of event, the ones that are conversation.

| Event | Look |
|---|---|
| `spawn` (task assigned) · `orch_msg` (message) | Orchestrator → agent, left, orange |
| `handback` (final report) · `peer` · `agent_msg` (to the orchestrator) | Agent → orchestrator, right, teal |
| `agent_msg` (to another agent) | Centered, blue |
| `xread` (cross-review) | A faint single line |

- Filter tabs: All · Orch · Peers. The tab you pick is remembered in this browser.
- Names follow the same rule as the office and the Debates card (role name `T1-A` + model `opus5.5`, ◆ for Codex).
- Click a name for the agent's details, a bubble for the full text, "View all" for a wide window, "Load earlier" for 80 more.
- Hovering an item puts a ring under the sender and the receiver in the office (not for people who are not in the office, and not during a demo).
- `/game` does not have this card.

## Debate table cells

| Mark | Meaning |
|---|---|
| ✓ Submitted | The report file exists and the agent in charge has finished that round. Click to open the document. "Read by: T3-B · T3-C" lists the other agents that read the report (cross-review) |
| ✎ Draft | The file exists but the agent is still writing that round |
| ✎ Interrupted · draft exists | The file exists, but the agent was interrupted (a limit or an error) or its state is not known, so nothing is being written. The round counts as still open |
| ✎ Writing | Assigned and the agent is running, but there is no file yet |
| ⏸ Paused | The agent was interrupted (a limit or an error) before it saved a file. The round counts as still open |
| ! Missing | The agent has ended and there is no file |
| Pending | Not assigned yet |

Each participant row shows provider, model and effort on one line under the status (`Claude Code · Opus 5.5 · xhigh`, `◆ Codex · gpt-6.1-sol · high`). The Final cell of a topic card reads "Final ready" once a final result exists, and clicking it opens the document. A room or a review without rounds, where every seat has handed in and nobody is working, reads "Done" (there is no next round to wait for); a debate with round folders keeps "Round N done · next stage waits". The rules for finding the final result are in "How debates are recognized".

## Office view

The "Agent office" strip of the dashboard and `/game` (full screen) draw the same picture from the same data. The legend is under the picture in both places. Nothing is drawn while the window is hidden or the strip is collapsed.

**Layout** (from the left): your office (red carpet, a user wearing a crown) → Control room (the orchestrator, with a monitor and a laptop) → one room per debate topic (a whiteboard with a cell per round: green = submitted, blue = writing, gray = pending) → Lounge. People always move door → corridor (→ the vertical passage that joins the rows) → the destination room's door → seat.

**Who is where**
- A working agent types at its own desk. When it finishes it rests in the lounge (waiting for the next instruction) and walks back to a desk when it gets work again. A newly assigned agent enters through the building entrance. An interrupted agent rests in the lounge too, with a pause icon; hover for when it resets, or why.
- An orchestrator waiting for a usage limit to reset dozes at its desk, and the sign on the Control room wall reads "Resumes 15:03", "Resets 15:03" or "Usage limit". The orchestrator says a new limit line in a bubble; other system lines stay silent.
- A child run launched by another agent sits next to its parent when both are in the "Other work" room.
- A finished topic loses its room: no one is working and 20 minutes have passed since the final result appeared, or the topic has no participants. Those people are counted as "N earlier". The Debates card keeps every topic.
- When you give an instruction a "!" appears over your head and the orchestrator walks to your office to listen. It also goes to your office to report, or to ask a choice question (AskUserQuestion), with a "?" mark.
- A letter flies for an orchestrator → agent message, a document for a final report. For a cross-review (reading another participant's `r<N>/<X>.md`) the document flies from the author to the reader; when an agent sends SendMessage to another agent it walks next to the receiver and tells it in a bubble.

**Lounge**: people who are resting pick a facility every 150 seconds (20 in a demo) and walk to a new place when the slot changes. The facilities are sofas, tea tables, game machines, treadmills, dumbbells and a coffee machine, and how many there are depends on the width (one sofa unit per 6 people, sized for up to 24). Those who do not fit are counted as "+N more". Someone who finished more than 30 minutes ago picks the sofa more often and may doze on it. People who failed, were stopped or ended take a sofa first (their status icon stays).

**Letters and icons**
- A brass nameplate on the front of the desk has the seat letter (`A`, `B`, `codex1`); the name tag under the desk has a status dot · model (`opus5.5`) · role. The full description shows when you hover.
- Over the head: book = reading, pencil = writing, black window = running a command, globe = web, letter = instruction received, … = thinking, ? = possibly stalled or unknown, ‖ = interrupted (hover: when it resets, or why), ! = failed. For Codex the same icon is chosen from the inner tool name.
- A **monitor symbol** shows the seat's provider (neutral symbols, not logos): orange `>_` for Claude Code, white ◆ for Codex. A resting seat has a dark screen with the symbol only; while working, green code lines scroll and the symbol shows through. Your desk and a seat not yet assigned have no symbol.
- A Codex agent wears black horn-rimmed glasses and has a ◆ on its name tag.
- People have front, back and side pictures and face the way they walk.

**Bubbles** are not summaries but sentences from the transcript shortened by rule: the `summary` field of a message, the first sentence of a report or remark (56 characters), and only the last two levels of a path. At most 4 at a time, gone after 8 seconds, and one remark per agent every 25 seconds.

**Rules of the screen**
- The dashboard strip lowers its scale a little to stay on one line (1.5 at the least). The full screen uses whole-number scales and wraps into several rows to fit the window height.
- When the server restarts the screen counts event numbers afresh and does not replay old events.
- The Galmuri font is in the repository (`static/fonts/`), so no internet is needed.
- Scrollbars are always visible, and the scroll position stays when the screen redraws every 3 seconds.

### Demo

The "▶ Demo" button picks a scenario and plays it on the screen only; the server and your records are untouched. You can also open one by address: `/game?demo=<name>`, `/?demo=<name>` (`1` is basic). **A session must be open** (the demo is drawn over the office of the open session; on the diagnostic panel with no session the button is hidden and no demo starts).

| Name | Scenario | Length | What it shows |
|---|---|---|---|
| `basic` | One debate cycle | 80 s | Cross-review, visiting a colleague and the reply, your request and the report, coming back from the lounge, everyone finishing and scattering in the lounge |
| `new` | New topic | 55 s | Your request → 3 agents enter by the entrance and get assigned → reading and drafts → submissions in turn → report to you |
| `round2` | Round 2 cross-review | 65 s | A "round 2 starts" letter → reading each other's round 1 reports → rebuttal and counter-rebuttal → round 2 submissions → report |
| `trouble` | Trouble | 56 s | Possibly stalled (?) → failed (red !) → a decision request to you (an alert on the dashboard) → restarted as a new agent |
| `talk` | Agent talk | 57 s | Questions and replies with colleagues in the same and other rooms, agreement sent to the orchestrator → report to you |
| `all` | Play all | about 5 min | The five above in turn (skip with "Next ▶") |

`new`, `round2`, `trouble` and `talk` use a demo-only room (T9) and made-up agents that exist only on the screen, so they play the same whatever your real data looks like. Real updates that arrive while a demo plays are held and applied when it ends.

## Codex

A session with only Claude looks no different. The marks are added on the Codex side only.

| What | How |
|---|---|
| Telling them apart | The dashboard uses the shape ◆ (status dot, letter before the name); the office uses glasses, the white ◆ on the monitor and the ◆ dot on the name tag. Colors stay as the status |
| Mixed team | A `codex exec` that Claude started through Bash is attached as an agent of that session (under the agent that ran it, when one did). One thread = one agent, a turn = an instruction received, the last answer of a turn = the final report. If the content of `-o <file>` equals the last answer, the thread enters the debate table as the author of that file |
| Names | The report name (`codex1`), else the model name (`gpt-6.1-sol` → `sol6.1`). If several share a model they are `sol6.1`, `sol6.1-2`, … in start order |
| Standalone sessions | A TUI, Desktop or exec thread that could not be linked (last 7 days) enters its project as an orchestration like a Claude session. Its office has a blue-gray carpet and the sign "◆ Control room" |
| Tokens | Codex input includes cache reads, so it is split and counted in the same sense as Claude. Reasoning tokens are inside output ("Of which reasoning" in the details); approval review (`codex-auto-review`) goes into the parent thread and shows in the details as "Approval review". The window is 258k |
| Big transcripts | For a rollout over 64 MB only the last 64 MB is read for activity (the details say "Read the end of the transcript only") and the running totals are set from the last total |
| Not read | Codex sqlite, `auth.json`, `config.toml`, `history.jsonl`, account identifiers |

**Rules for attaching a `codex exec` to a session**: only a `codex exec` in a command position counts (text in quotes, heredocs and comments, and quotes inside `bash -c '…'` or `eval`, are not commands; `cd` is followed in order). If one of these matches it is attached.

1. The prompt matches the Codex thread's first message (at least 40 characters apart from variable parts, within 30 seconds, same working folder)
2. The executed command contains the thread id
3. For a short prompt, time + working folder leaves a single candidate: "guessed"
4. When the pairing of call and thread is unknown (a loop, say), and exactly one Claude session ran Codex within 30 seconds of the thread's start, it becomes that session's agent ("guessed")

When several sessions overlap it is not attached and stays standalone.

## Plan and usage limits

The bottom bar shows the plan and usage limits of the whole account. Claude Code and ◆ Codex appear in the same order (plan → 5h → week → extra usage · credits → record time).

| Service | Plan | 5-hour and weekly usage |
|---|---|---|
| Claude Code | The plan tier in `.claude.json` | The usage Claude Code hands to its status line, kept by `statusline.py` (below), shown in gray as "status line · HH:MM". **Otherwise**: the cache Claude Code leaves in `.claude.json` (refreshed when you open `/usage`), shown in gray as "recorded HH:MM · refresh with /usage"; with no cache, one line: "Open /usage in Claude Code to see this". Neither reads a credential file or makes an outside call. |
| Codex | `rate_limits.plan_type` in the rollout | The latest record of `primary` (weekly) and `secondary` (5 hours). Refreshed whenever you use Codex. Some plans have no 5-hour window |

Colors: yellow from 70%, red from 90% or when a limit is reached. A value whose reset time has passed shows `—` (with the estimated reset time) instead of "limit reached" or a percentage. A limit hit recorded in the conversation (`quotaLimits`) counts as past when the usage read after it is under 100%.

**With the status line command (no request).** Claude Code pipes a JSON document to its status line command, and in it are your 5-hour and weekly usage. Register `statusline.py` (in the repository folder) as that command and it keeps just those two windows in `$XDG_CACHE_HOME/agent-bullpen/statusline.json` (else `~/.cache/agent-bullpen/statusline.json`, mode 0600), which the dashboard reads. `python3 statusline.py --print-config` prints the piece to merge into `~/.claude/settings.json` (it never edits the file) and keeps your current status line as `--chain`, so it keeps drawing as before:

```json
{ "statusLine": { "type": "command", "command": "python3 /path/to/agent-bullpen/statusline.py --chain '~/.claude/my-statusline.sh'" } }
```

The file holds `{"v":1,"at":<time received>,"rate_limits":{"five_hour":{…},"seven_day":{…}}}` and nothing else (with a gateway account also its spending limit, `spend_limit`: percentage, reset time, the dollar amounts and the period): no path, session id, model, cost or instruction from the input. The bar shows "status line · HH:MM" after the 5-hour and weekly values (hover for what it is). What the status line does not carry (a model's own week such as Sonnet, extra usage) comes from the `.claude.json` cache and is shown with the cache's own "recorded HH:MM", never under the status line's time. The sources come in this order: the status line, then the `.claude.json` cache. A status line reading under 10 minutes old is used even when the cache has a newer one; after that the newest source is shown, and a window whose reset time has passed shows `—` instead of its old percentage. The numbers change only while Claude Code is drawing its status line; there is no request and no token is read. Options, the exact rules and the file's safety checks: [configuration](configuration.md#statuslinepy-plan-usage-from-the-status-line).

Account identifiers, e-mail addresses and tokens are not sent to the screen.

## Child runs started from Bash

When a Claude session, or one of its agents, runs `claude -p …` (or `--print`) through Bash, the Claude session that appears is attached as an **agent** of the session that started it. It is not listed on its own and is counted in the parent session's agent count. The agent that launched it is the one whose transcript holds the command: a child started by an agent stands under that agent, and a child started by another child (a `claude -p` inside a `claude -p`) stands under that child, one step further in per level (four levels at most). An agent whose parent is not in the list on screen, for instance with the "This debate" filter, stands alone with a "↳ launched by …" line.

**How a child is linked.** The evidence is ranked, and the highest rank that gives one answer decides. Which session started it (the tree) and which agent inside that session did (the node) are decided separately.

| Evidence | Rule name | Shown as |
|---|---|---|
| The launching command's output file (`> out.json`, `-o`) holds this run's session id, or its text names the id (`--session-id`, `--resume`) | output file · id | certain |
| A live process: its parent chain leads to the session, or `CLAUDE_CODE_SESSION_ID` / `CLAUDE_PID` in its environment name it; or a link remembered in `links.json`. These settle the session only; which agent inside it launched the child is then read from the command, and falls back to the main session | process lineage · environment variable · saved link | certain |
| The child's first instruction (32 characters or more, ignoring quotes, backslashes and spacing) is found in the text of a command that was running when the child started, or in a file that command wrote | instruction text | certain |
| The same match with a shorter instruction | short instruction | guess |
| The same match, but the only command in that session that could have started a child is a script that could not be read (missing, too large, binary, a path that depends on a variable) | guessed (launching script unreadable) | guess |
| Only the timing and the folder fit: the one command that ran in that folder just before the child started | guessed (time + cwd) | guess |

- "Was running" means the command had started and had not ended yet, with 10 seconds of grace after the end. There is no fixed time window.
- A command is ruled out when its literal text argument differs from the child's first instruction, when it ran in another folder, when it only prints text (`echo`, `printf`), or when it asked for no session record (`--no-session-persistence`).
- A session whose record merely holds the instruction (it wrote the words into a file or a message) but ran nothing that could start a session is not taken for the launcher. With no other evidence the child stays unlinked, and Diagnostics notes `content_author_differs` with that session.
- When two sessions, or two commands, fit equally well, nothing is linked and a tie is never broken by time. The child is not listed as an agent; it shows in the "not linked" group below.
- A path that depends on a variable is worked out only inside that command or script, never by searching folders. A path that cannot be worked out is reported as `path_unresolved` in Diagnostics.
- In the few seconds after the server starts, the links that rest on instruction text show as a guess, and a run that a sub-agent or another run launched may be missing from the list. The background pass that compares the texts starts once the first screen has been built (10 seconds after the start at the latest); when it is done the links turn certain and the missing runs appear.
- The agent's details show "Linked: <rule> · certain|guess" for a `claude -p` run and for a Codex thread; hover for the reason. A short-instruction guess says which of its three reasons applies: a short instruction, a text comparison that hit its size limit, or a launching script that could not be read. A run started by a plain call of the `Agent` tool needs no label.

**Resumed runs.** A child can be continued with `--resume` or by a message. It stays one agent. Its details say "N runs" and list each run: how it started (first start · new process · resumed by message) and whom the instruction came from ("instruction from the orchestrator", or an agent's name). The one who gave a later instruction may differ from the one who started the child; the parent does not change.

**Not linked.** The folded group "N child runs not linked", at the end of the agent list, shows sessions that started within a minute (Codex: 30 seconds) after one of this session's Bash calls and could not be tied to it: at most 20 per session, started within 14 days, newest first. Each says why: a matching command ran but could not be tied to the run (a tie); it is still running and no command of this session matches; it ended before the dashboard saw its process. Nothing is shown when there are none.

**Remembered links.** The link record file `$XDG_CACHE_HOME/agent-bullpen/links.json` (else `~/.cache/agent-bullpen/links.json`, mode 0600) keeps only the certain links, so a restart does not forget them: for each one the child and parent session ids, the rule (`proc`, `env`, `out` or `content`), the time it was first seen and, for a child of an agent, that agent's id. Never an instruction, a path or an environment value. It holds at most 2000 links for at most 90 days, the oldest dropped first, and the file is read up to 2 MiB. Guesses (short instruction, unreadable script, time) are not kept. It is created only after the first certain link, and a link that is only remembered is shown as "saved link" or "remembered link" and counts as certain; it never overrides what the transcripts or the process say. If the file is not a regular file, is owned by someone else, is writable by others or is malformed, it is ignored and Diagnostics lists `cache_error`. With `--no-link-cache` it is neither read nor written.

- Nothing is linked if the pid in the session file was reused by another process (`procStart` differs), belongs to another user or another pid namespace, if there is no transcript, or if the user opened the same session separately. Where there is no `/proc` (macOS) the Codex side is "unknown", and the Claude side follows parents through `ps` only and compares the owner of the process (`ps -o uid`) but not its start time.
- A detached child (`setsid nohup claude -p … &`) has init (pid 1) as its parent and cannot be found by ancestry, but the environment of a process started by the Bash tool still holds the id of the Claude session that started it (`CLAUDE_CODE_SESSION_ID`) and its pid (`CLAUDE_PID`). **Only these two values** are read from the environ of a live `claude -p` child and a Codex process (no other environment value is stored, logged or put in a response). The environment's session id is not used if it is that process's own session or if the live Claude behind `CLAUDE_PID` has a different session (the session was switched). On Linux it reads `/proc/<pid>/environ`, elsewhere `ps -E`, for processes of the same user only, and "unknown" when that does not work.
- Several children started by one loop line (`for x in a b c; do … claude -p … & done`): one launch per iteration when the count is known (`for x in a b c`, `{1..5}`, nested loops multiply up to 64), any number when it is not (`while`, `until`, `$(…)`, a glob). A command cannot take more children than it can start, so look-alikes beyond that stay unlinked.
- Reading scripts: when a Bash command runs a script file in a command position (`bash`, `sh`, `zsh`, `source`, `.`, `./file`, running by path) the `claude -p` and `codex exec` inside that file are examined by the rules above (the working folder follows the `cd` scope of that call; `$HOME`, positional arguments and `VAR=value` are expanded inside the script only). Only one level deep, so a script that calls a script is not followed. The file is opened under the same policy as the document viewer: regular files only, up to 256 KB, dot paths and secret-looking names are not read, and anything unreadable is skipped silently. A script that could not be read might or might not start a session, so an instruction found in that session's text then makes only a guess, never a certain link.
- A launch command that left no session record at all (a deleted transcript, `--no-session-persistence`) shows only as `orphan_launch` in Diagnostics. No card and no seat is made for it.
- With no role tag the model name (`opus5.5`…) is used and models that repeat are numbered in start order, so a `claude -p` session can shift the numbers of the other agents of the same session.
- Conversation: the first user instruction of the child session becomes the body of a `spawn` event, an instruction continued with `--resume` shows as `orch_msg`, and the last assistant text when a turn ends with `end_turn` shows as `handback`. Read them in the "Agent talk" card.
- Debate cell assignment does not change (the name is for the screen).

## How debates are recognized

A folder on disk with reports laid out as `<topic folder>/r<N>/<participant>.md` is read as a debate.

```
research/                    ← common brief (optional): title, topic table
  brief.md
  t1_naming/                 ← topic folder
    brief.md                 ← topic brief: "# Title" and "**A — role**" lines
    r1/A.md  r1/B.md         ← round 1 reports (one per participant)
    r2/A.md  r2/B.md         ← round 2
    rulings.md               ← final result (found automatically)
  final/t2_api.md            ← a final deliverable named in the table
```

**What counts as a debate.** A folder that exists on disk and holds a round folder (`r1`, `r01` or `round1`) or a `brief.md`. A `README.md` or `index.md` counts as the brief only next to round folders. The folder is found from the paths an agent's work points to (the dashboard checks that folder and the ones above it) and, about once a minute in the background, by walking the top of the repository a session works in (never `/tmp`, your whole HOME, or a folder that holds many repositories; the walk has a budget, and when it runs out Diagnostics notes `listing_capped`). A folder named only in an instruction, which does not exist, makes no debate.

**Rooms: work that is not shaped like a debate.** A meeting, an agenda, a plan that several agents work on together is shown as a room: the same office room and table as a debate, found by structure, never by the words used (*debate*, *meeting*, *agenda* are not read). A folder is a room when the records and the disk say two things together, for agents of one orchestrator:

1. two or more of them have a **first instruction that points at the same guide**, a `.md` of any name that exists on disk (not a file the instruction tells the agent to write, and not a quoted or negated one); and
2. each of them is told to write, or has written, **a file of its own** (`.md`) in the guide's folder or one folder below it. The room is the guide's folder, its title the guide's first heading, the cell of each seat that participant's file, and there is one round and no round folder. Or none of them has any file and they **message each other** (`SendMessage`): the room then has participants and no cell.

The seat is the letter the instruction names to the participant itself (`[TAG-B]`, `B 담당`, `You are participant B`, `You hold seat B`, `Work as B (…)` at the start of a sentence), else the one-letter tag of the description, else the file's name; the bare words `participant B`, `seat B` and `as B (` in another meaning (a user study, a train seat, a language) are no marker. A folder that is a debate already keeps its rules unchanged.

What is **not** a room: a document everybody reads (the guide at the top of a repository, or directly in its `docs` folder), a guide that one of the agents wrote or is told to write (a result the others read), a guide of each one's own, one that is not on disk or that only a later message names, one file that all of them write, files elsewhere (two folders below the guide, another folder, code in other repositories), a single agent, another orchestrator's agents, messages only from the orchestrator, and instructions that merely use a word like *meeting*. Nor is a group that did not work at the same time (each one started after the one before had finished), or most of whose members change files of the project outside the guide's folder: that is parallel work on a plan. Only a file of the project that a tool wrote counts as changed: what a command only saves (a log written by `>`) and files kept in a scratch folder outside the repository (`/tmp`) do not. A guide that is only quoted points at nothing: a code fence, a `>` quote, or the lines after a "read-only quote" sentence show an earlier instruction, while a fence under a line that tells the agent to carry it out ("Execute these instructions:") is the agent's own instruction.

**How an agent gets a seat**
- The report paths come from the first instruction and `SendMessage` bodies given to an agent (absolute paths or `~/…`), from the files the agent actually wrote, and from two kinds of command. The command that launched the agent writes its output to a file (`> r1/B.md`, `-o r1/B.md`): when the instruction names a report for that round, the output counts only if it is that report by the whole path, round folder's spelling included, so an auxiliary `-o B_last.md`, or `r01/B.md` where the instruction says `r1/B.md`, is a file of its own and gives no seat; when the instruction names none, the output file is the evidence. And a Bash command the agent itself ran writes a file by a redirect (`>`, `>>`), `tee` or a heredoc (`cat > r1/B.md <<EOF`): that is a write like a Write or Edit (a Claude agent's Bash only; a Codex agent's own commands are not read for this).
- A relative path ("write `r2/A.md`") is tried against several bases (the agent's folder, the folder of the session that launched it, the git top level) and compared with the debate folders that exist. One match is used. Nothing matching is dropped; several matches give no seat and Diagnostics notes `path_ambiguous`. A path that uses a variable that cannot be worked out is `path_unresolved`.
- The seat is given by, strongest first: a path the agent was told to write (or that the command which launched it writes into, as above) → a marker such as `T1-A` in the first 300 characters of the instruction → a successful write of its own report → the tag at the start of its description, only when a successful write backs it (a bare mention of the report, or a path that could not be worked out, does not). A write is successful when a Write or Edit result is not an error, or when a Bash redirect, `tee` or heredoc did not end in an error and the file is there. A reader of a report, a quote ("write the result to …" inside a document), a negation ("no need to write"), a quoted or negated marker and a failed write give no seat. A marker sits in one debate only. An agent has one file per seat: when its claims for one seat point to two physically different files, the stronger claim keeps the seat, and equal ones leave it empty (`alias_collision`). Where a round has two folders (`r01` and `r1`), a marker or a tag alone cannot choose between them, so the seat stays empty as well.
- When two agents that are both alive claim the same seat it stays empty (`seat_tie_held`); when the earlier one has finished, the later one takes the seat over. A seat keeps its spelling (`r01`, `round1`, `b.md`, `A_flow.md`); two different files for one report name are not merged (`alias_collision`).
- An agent that works in a debate folder but holds no seat is shown under "Other work" and Diagnostics notes `debate_in_misc`.
- **A review without rounds** (a folder with a brief but no round folders) gets cells when its brief names the reviewers and the result file of each (a reviewer's name with its `<name>.md` on the same line), or when it is a room (above); the cells then have no round, or the one round of a room. With neither, only the title is shown, and Diagnostics notes `declaration_missing`. No seats or rounds are made up.
- Codex has no role tag, so a report path in the instruction that comes with "write", "save", "output" (or the Korean equivalents) is taken as that agent's cell. The `-o` file and files actually written count too. Its working folder is one more base for a relative path.
- The document viewer (`/api/file`) is stricter than the debate list: it opens only folders that have `brief.md` or `r<N>/`, and room folders (see "Limits").
- If both the topic folder and its parent have `brief.md` and the parent has no round folder, the parent groups the topics into one debate (`t1_…`, `t2_…` under `research/`).

**Names and roles**: the topic name is read from the first heading of the topic `brief.md` and the role from the `**A — role**` notation (a capital letter, a dash, then words that say who it is; a bold number such as `**C-23**` in an editing brief is not a participant and makes no row); without them only the folder name is used. The agent name on screen is that tag (the report name for Codex), else the model name (`opus5.5`, `sol6.1`). This name is for display and is not used to assign cells.

**The final result** is found in one of three ways.

1. The table in the common `brief.md`. The first row is the header; a row is read when the folder cell looks like `` `t1_naming/` `` and the final-deliverable cell looks like `` `final/…` `` (the column position does not matter). The "depends on" prerequisites are read from the column whose header contains `depend` (or the Korean word for it).

   ```
   | Topic | Folder | Depends on | Final deliverable |
   |---|---|---|---|
   | Naming | `t1_naming/` | — | `final/t1_naming.md` |
   | API shape | `t2_api/` | t1 | `final/t2_api.md` |
   ```

2. **Found automatically**: when the table has no row for that topic, the final result is the newest `.md` directly under the topic folder that is as new as the last submitted report (2 seconds of tolerance) or newer. `brief.md`, `round<N>….md` and `r<N>_….md` (instruction files) are skipped; names that contain `final`, `ruling`, `verdict`, `decision`, `conclusion`, `summary`, `plan`, `closing` (or `결론`, `판정`, `합의`, `최종`, `정리`, `종결`, `마무리`) come first, and among equals the most recent. Nothing is searched while a cell is draft or writing or when no report has been submitted, and when a new round starts after a conclusion its reports are newer, so the topic goes back to in progress by itself.

3. **A conclusion of the bundle, in the folder above the topic.** When neither of the above gives one, a `.md` in the folder above the topic (the folder of the shared `brief.md` that groups the topics) with a conclusion name as in 2 closes the topic if it is not older than the topic's last activity (its submitted reports and the documents written in its folder) and either names the topic's folder (`step2/`, a path, or a name with a digit or an underscore in it; a plain word such as `recheck` only as `recheck/` or in backticks or quotes) or, when it is in the bundle's own folder, is not older than everything the bundle did and names none of the bundle's topics and says nothing is left open (not yet, remaining, incomplete, and the like), so that it is the bundle's closing. A document that names some topics speaks for those only: the others stay open. It is shown as the topic's final result (`../CLOSING.md`), with the same conditions on open cells and submitted reports as in 2.

**One folder is shown once.** The same real folder reached by two paths (a link into it), and the same place of a repository in its linked worktrees (the same git common folder and the same path inside it), are one debate: the copy this session's agents hold a seat in stands for the others, else the one they write in, else the one they read or are tied to, else the main checkout. A copy that holds a seat of this session stays a debate of its own, so no cell is hidden. The tab and the debate title say how many copies were folded into it. A folder that holds only an instruction (no seat, no cell, nobody declared, no final) inside the folder of a debate that shows something is a part of that debate's records and is not listed on its own; with no debate around it it is still listed as a title.

## Token accounting

| Value | Meaning |
|---|---|
| Current context | Tokens in the last API call (new input + cache reads + cache writes). How full the window (200k or 1M) is |
| Total input | Input tokens summed over all calls so far. Mostly cache reads |
| Output | Tokens generated, thinking included |
| Advisor model | An advisor model called inside a response (`advisor_message` in `usage.iterations`). It is not in the parent's totals, so it is counted separately |
| Cost | Dollars at each model's API list price. Cache writes are priced separately for 5 minutes and 1 hour, with the fast-mode (×2) and US-only inference (×1.1) multipliers. Advisor model cost is included |

The price table is `PRICES` in `board/tokens.py`. For Claude it is platform.claude.com/docs/en/about-claude/pricing (checked 2026-09-29; models after 4.6 have no surcharge for the 1M context), for Codex developers.openai.com/api/docs/pricing (checked 2026-09-30); `codex-auto-review` has no price and is counted as "unpriced calls".

Reading the cost:

- **On a subscription plan (Claude Max, ChatGPT and so on) it is not your actual bill.** It shows what the same usage would cost through the API.
- **It is a lower bound.** Some calls are not in the transcript (background calls, for example), and the cache reads on record may be fewer than the real ones.
- Calls of a model without a price are left out and shown as "N unpriced calls excluded".
- A response is stored as one line per content block, each with a usage. In agent transcripts the output tokens of earlier lines are streaming midpoints, so the value of the last line of the same response (`message.id`) is used.
- The totals include finished agents and calls made before a context compaction.

## Status rules

An agent has one status. A `claude -p` or Codex run is judged from its **last run** only (a run is one process: the first start, or a new process after a restart or a resume), so the limit or error of an earlier run never covers a newer one.

| Status | Reason | Criterion |
|---|---|---|
| Working | | No completion notice after the last activity and the session process is alive |
| Stalled? | | Working but no growth in the records for over 10 minutes. If a tool call that waits for a result (`docker run` and the like) is open it shows "<tool> · running N min" on the agent card (from 60 seconds on) for up to 30 minutes and is not counted as stalled. This is only a guess |
| Interrupted | Limit reached | The run's last line is a usage limit (HTTP 429). The card says when it resets ("Resets 15:03"), or "Reset time not recorded" when the record has no time; the time is never made up |
| Interrupted | API error | The last line is an API error on the server side (such as 529). These usually pass |
| Interrupted | Time limit | The run was cut off by the background time limit. Shown only when a notice in the launching transcript says so |
| Interrupted | Exited early | The run closed with no final answer and nothing says why. The dashboard does not claim that anyone stopped it |
| Done | | The `<task-notification>` status in the main transcript, or a final answer followed by the end of the process |
| Failed | | The notice says failed, or an API error that will not pass by itself (a refused request, an expired login) |
| Stopped | | `TaskStop` or a kill is seen, or the notice says killed; for Codex, `turn_aborted` |
| Session ended | | The session process was confirmed gone, or (for an agent started with the `Agent` tool) the session that started it has ended |
| Ended abruptly | | The transcript stops without a final answer and its process is gone |
| Unknown | | There is no process to check and the records have been quiet for over a minute, so it cannot be told whether it still works |

![Agent list: a run that launched two levels of runs, and runs that stopped (usage limit, time limit)](images/en/agents.png)

- An agent that is Interrupted or Unknown is not over: it stays in the main agent list, not under "finished agents", and counts in the header chips. An agent whose launcher is only Interrupted does not become "Session ended".
- The run's own error line is read before the process: a process that is still there but whose last line is a usage limit is Interrupted, not Working. A stop that is seen comes next, then the notices, then the process.
- **Orchestrator.** Working or Idle. When its last line is a usage limit and its process is still there it reads "Usage limit reached", with "Resumes automatically at 15:03" when Claude Code announced that it will continue by itself, "Resets at 15:03" when it will not, and "Waiting for the reset" when no time is recorded.
- **Same limit, one alert.** Agents and the orchestrator that stopped on the same reset time raise one alert, and Diagnostics notes it (`limit_group`). If the reset has passed and the run still has not continued, it notes `not_resumed`.

The process check uses `/proc` on Linux and `ps` elsewhere (macOS). When neither works or the answer cannot be known it is "unknown", and **unknown is not treated as ended**: whether the transcript is still growing decides instead (the start-up output tells which method is used).

A Codex agent is decided by the closing record of its last turn (`task_complete` → done, an attached error → failed, `turn_aborted` → stopped). While a turn is open it looks at the `codex` process that has that rollout open (Linux) and at Claude's background task notices and `TaskStop`. The status names and the stall limits (10 and 30 minutes) are the same as Claude's.

### System lines

A usage limit or an API error that Claude Code writes into the transcript is not something the orchestrator said, so it is not shown as its speech. It becomes a faint **system line**: in the Message flow (under the "Status" filter, not "Orch"), as centered faint text in the You ↔ Orchestrator card (not a bubble, and not the "last remark"), and in the office as a bubble from the orchestrator for a limit only. The wording is fixed text plus a number or a time, never the transcript's own sentence:

| Line | When |
|---|---|
| Usage limit reached · resumes automatically at 15:03 | A limit line, followed within a minute by Claude Code's "continuing automatically" notice (without the notice: "· resets 15:03"; with no time in the record: only "Usage limit reached") |
| Usage limit reset | The notice that the limit is over |
| API error 529 · can be retried | A server-side error that passes by itself |
| API error 400 · request refused | The server refused the request |
| Login expired · sign in again | An authentication failure |

An API error line in an agent's own transcript is not shown as that agent's remark either; its status says it.

## Diagnostics

The header chip "Diagnostics N" (hidden when there is nothing; yellow when something needs checking, plain when it is only news) opens a list of what the dashboard noticed but could not settle for this session. Each line has a level (**Check** or **Info**), one fixed sentence for its code, and its subject: an agent (click to open its details), the orchestrator, a debate folder, or "This session". Counts, ids and short names go only in the tooltip. A line never contains text from a transcript, and at most 200 are kept per session. A code this version does not know shows as the bare code.

| Code | Level | What it says |
|---|---|---|
| `limit_group` | Info | Several runs stopped on the same usage limit (their alerts are shown as one). |
| `not_resumed` | Check | The limit or error has passed, but this run has not continued. |
| `silent_live` | Check | Its process is idle and its transcript has been quiet for a while. |
| `multi_process` | Check | More than one process matches this run. |
| `invisible_child` | Check | A child run is going that leaves no transcript, so it cannot be listed. |
| `torn_lines` | Info | Some transcript lines were cut off mid-write; the dashboard recovered what it could. |
| `parse_errors` | Check | Some transcript lines could not be read and were skipped. |
| `stray_notice` | Info | A background task finished; its notice is not an agent’s report, so it was left out. |
| `format_drift` | Info | The transcript format differs from what this dashboard knows; some judgments rest on weaker evidence. |
| `proc_unknown` | Info | The process list cannot be read here, so whether a run is alive is judged from its transcript alone. |
| `cache_error` | Check | The saved-links file cannot be trusted or written, so it is not used. |
| `listing_capped` | Check | The folder search hit its limit; some debates may be missing. |
| `evidence_conflict` | Check | Two kinds of evidence disagree about which session started this run. |
| `content_author_differs` | Check | The instruction text matches one call, but other evidence points to another launcher. |
| `content_only` | Info | Linked by the instruction text alone (no output file, process or id to back it). |
| `ambiguous_content` | Check | Several calls match this run’s instruction equally well, so it was not linked. |
| `node_unresolved` | Check | Which agent launched it is not known, so it sits under the orchestrator. |
| `orphan_launch` | Check | A launch command left no session record. |
| `fingerprint_incomplete` | Check | The text comparison hit its size limit, so no firm link was made. |
| `path_unresolved` | Check | A report path could not be worked out (it uses a variable). |
| `path_ambiguous` | Check | A path fits more than one debate folder, so no seat was given. |
| `alias_collision` | Check | Two different files share one report name. |
| `seat_tie_held` | Check | Two agents fit the same seat equally well, so it is left empty. |
| `debate_in_misc` | Info | This agent works on a debate folder but holds no seat there; it shows under “Other work”. |
| `declaration_missing` | Info | A review writes into a folder whose instructions name no reviewers or result files, so no cells are drawn. |

## Host name rules

The `Host` header of a request is checked by **name only**. The port value is not looked at, so an SSH tunnel that arrives as `localhost:<another port>` is fine (only digits are accepted in the port position; a shape such as `127.0.0.1:80@evil.test` gets a 403).

- This check applies where there is no access token (a loopback address, or `--no-auth`). Where a token is required (any other `--host`, or a fixed `--token`) every name is accepted and the token or its cookie decides; a page of another site has no cookie for this server.
- Names that pass: `localhost`, `127.0.0.1`, `::1`, the addresses opened with `--host`, and the names or `.suffix` values added with `--allow-host`. There is no built-in suffix.
- Any other `Host` (for example DNS rebinding that points an outside domain at this address) gets a 403, and the body tells the fix (`--allow-host …`) in English and Korean.
- IP addresses are compared in canonical form: IPv6 may be written in any shape in `--host` and `--allow-host`, and `fd7a:115c:a1e0:0:0:0:0:1` and `[fd7a:115c:a1e0::1]` are the same address.
- Opening a non-loopback address asks for an access token; `--no-auth` (or `--allow-host` alone on loopback) prints a framed "no authentication" warning at start. Details: [remote](remote.md).

## Limits

- **The document viewer** (`/api/file`) opens only files the agents wrote and files inside a debate folder (a folder with `brief.md` or `r<N>/`, or a room folder; HOME and its ancestors are excluded). The extension must be `.md`, `.txt`, `.yaml` or `.json`, the size up to 2 MiB (413 above that), and regular files only. It never opens the following, even if an agent wrote the file (403).
  - Credential files (`.credentials.json`, `.claude.json`, `auth.json`), `settings*.json` in the Claude config folder, anything under `~/.codex/`
  - **A path with a component that starts with a dot** (`~/.docker`, `.config/gh`, `.mcp.json`, a project inside a dot folder …). The one exception is these three components of a Claude Code worktree, `<repo>/.claude/worktrees/<name>/`, so a report inside it opens. Dot components below that (`.env`, `.git`, `.config` …) are still refused. It is not an exception if `<name>` starts with a dot, if another dot component comes before it, or if it is the `.claude/worktrees` of the Claude config folder itself.
  - **A secret-looking name** (`*secret*`, `*credential*`, `*password*`, `*passwd*`, `*apikey*`, `*api_key*`, `*api-key*`, `*token*`, `auth.json`, `kubeconfig*`). Whatever the extension, `.md` is closed too, also inside a worktree. Keep these words out of the names of reports you want to open.

  Links are resolved and the real path is checked before opening, and checked once more just before the open. While opening, links are not followed at any component of the path.
- **Debate cells and the final result named in a brief table only check that the file exists.** They look at presence, size and time and do not open the file, so a report in a hidden folder (a working folder joined by a link, for example) or named like `token_budget.md` still shows as "Submitted", and the final result named in the table as present. The document itself, though, does not open when you click it because of the rule above (403). A link that points at a credential file, and anything that is not a regular file (FIFO, device, folder), counts as no file. If a link points at a dot path or a secret-looking name the cell still stands, but the line count is counted by opening the file, so it shows 0 and the document does not open.
- **What is found by listing a folder is strict.** Dot-path and secret-named files do not appear among the candidates for the automatic final result, in the list of documents of a topic folder, or in the list of the `final/` folder.
- **`brief.md`** is a fixed name, so a regular file in place is read even under a dot folder to build the title, roles and table (it cannot be opened on screen, though). A `brief.md` that is itself a link, or whose real file name differs (one that pulls in another file), gets the strict dot-path and secret-name rules applied to its target and is not read. When only a folder is joined by a link (for example `work → .wt/work`) it is still the real `brief.md` of that name and is read like a file in place.
- A session id accepts only the UUID shape (`session=*` and `../x` give 404; `--session` is the same). A bad numeric parameter (`t`, `since`, `before`, `limit`, `idx`) gives 400 (`limit` of `/api/talk` is 1–300, `scope` only none, `user` or `agents`).
- A Codex thread with more than 200 turns still shows the events of every turn (instructions, final reports) in the flow. The turn list in the details panel has only the last 200.
- The same user instruction recorded again within 120 seconds (a queued/human double record) counts once. Only when the whole text is the same and the times are within 120 seconds.
- An agent's thinking is not in the transcript and is not visible. Judge by tool calls, remarks, messages and reports.
- "Read by" (cross-review) catches only what was read with the `Read` tool; what Bash `cat` read is not known. For Codex it uses the command interpretation Codex itself records (read in `parsed_cmd`).
- A `claude -p` or `codex exec` started by an agent's own Bash call is found in that agent's transcript and stands under it; the main transcript's Bash calls are read the same way. A Desktop session shows "Status unknown" when its process cannot be identified.
- The completion notice of an agent started by another agent (a grandchild: meta has `parentAgentId` and `spawnDepth`) is kept in **the parent agent's transcript**, not in the main one. Its status comes from that notice (task-notification) and from the completion result the parent's `Agent` call got back (`status` completed, failed or killed, an error result counts as failed; the background start acknowledgement `async_launched` is not a completion); if the parent has ended and no notice exists it counts as "Session ended", but not while the parent is only Interrupted. A detached `claude -p` run keeps its own process, so it goes on when its parent ends.
- Done and failed are decided from the notices in the main transcript, not by the agent itself, so a late notice leaves it "Working" for a moment.
- The transcript format is that of Claude Code 2.1.28x. Some judgments rest on markers that Claude Code does not document (the closing record of a process, the usage-limit records, the start of a run); they are known for versions 2.1.235 to 2.1.286. A version outside that range says nothing by itself (every Claude Code release would). When a marker or field that the version is known to carry is gone, or the record's version is not a version number, Diagnostics notes `format_drift`, the dashboard falls back to weaker evidence (the process, the last lines), and some items may look empty. Line types it does not know are ignored.
