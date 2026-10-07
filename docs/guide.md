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
| Agent office | The pixel-art strip (see "Office view"). Open at first when the window is 768px wide or more (a tablet), collapsed below that (a phone); a collapsed office says so in one line inside the card, and pressing the line opens it. What you pick with the Collapse / Expand button or that line is remembered |
| Orchestrator | Working / waiting for results / waiting for a usage limit to reset, last action, current context |
| Token usage | Per provider (Claude Code / ◆ Codex): summary tiles (cost, input, output, agent count) and a table (Orchestrator / Working / Finished agents). The footer has cache, calls and per-model figures plus what only that provider has (Claude advisor model; Codex reasoning, approval review and weekly limit) |
| Progress | One line per topic: which round, how many submitted, who is left / the next topic and what it waits for / stall warnings |
| You ↔ Orchestrator | Your instructions (purple, right) and the orchestrator's reports (left) as speech bubbles in time order, including choice questions and the answer you picked. Click a bubble for the full text, "View all" for a wide window, "Load earlier" for 80 more. Scrolled to the bottom it follows new messages; reading further up it only shows "New messages ↓". A usage limit or an API error appears there as a faint centered line (see "System lines"), not as a bubble |
| Agent talk | The card to the right of the office card (see "Agent talk card") |
| Debates | Topic cards: Brief → Round 1 → Round 2 → Final steps and a participant × round table. Working agents that are in no debate cell go to the "Other work" card at the end |
| Agents | Live cards: the tool and target in use right now, activity over the last 30 minutes, tool call count, context, model and effort. A run that stopped on a limit or an error stays here with its reason; a run launched by another agent stands under it, one step in; a folded group "N child runs not linked" at the end lists runs that could not be tied to this session (see "Child runs started from Bash") |
| Activity timeline | One row per agent with tool calls (read · write · command · web), instructions received, reports saved and final reports, in time order. The window is the last 30 minutes, 2 hours or 12 hours, this debate, or 24 hours, 3 days, 7 days or all of the session (on a narrow screen the longer ones are in a box), or a range of your own (two date-time inputs, at most 31 days); ◀ ▶ move the window by its own length into the past and back toward now. A window over 13 hours is thinned on the server (one mark of each kind in each of about a thousand time columns, a number says how many a mark stands for) and shows the 60 rows that were most recently at work in the range until you press Show all (the window of a debate holds the debate's own rows) |
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
| ✓ Submitted | The owner's file is there and, if the owner was asked to save it again in this run, it did. Click to open. "Read by: T3-B" lists the agents that read it (cross-review) |
| ✎ Draft | The file is there and the agent is still writing that round. With the agent interrupted or unknown: "Interrupted · draft exists", the round stays open |
| ✎ Writing · ⏸ Paused · ! Missing | The agent was asked to save this file (`-o`, `>`, a seat tag) and has not: it is running · it was interrupted · it has ended |
| Previous file | Grey, opens, never a submission: a file nobody owns, or one the owner was asked to save again and has not (also a line under "Writing") |
| Pending | No file, and nobody was asked for one ("—" in a round nobody wrote in, beside a final) |

Small marks: how the first write was seen (**tool** · **shell** · **to be saved** · **tag**), "fixed by X", "Guess · probably written by X". Under the table: "Working · estimated (why)" and grey "Ended · no file" for agents with no cell, and the end of the topic: "Final ready", "Closed by the bundle's final", or **"Closing not confirmed"** with reasons and candidates. A room that is all in reads "All N submitted · closing not confirmed" until it can be closed, then "Done". Rules: "How debates are recognized".

## Office view

The "Agent office" strip of the dashboard and `/game` (full screen) draw the same picture from the same data. The legend is under the picture in both places. Nothing is drawn while the window is hidden or the strip is collapsed.

**Layout** (from the left): your office (red carpet, a user wearing a crown) → Control room (the orchestrator, with a monitor and a laptop) → one room per debate topic (a whiteboard with a cell per round: green = submitted, blue = writing, gray = pending) → Lounge. People always move door → corridor (→ the vertical passage that joins the rows) → the destination room's door → seat.

**Who is where**
- A working agent types at its own desk. When it finishes it rests in the lounge (waiting for the next instruction) and walks back to a desk when it gets work again. A newly assigned agent enters through the building entrance. An interrupted agent rests in the lounge too, with a pause icon; hover for when it resets, or why.
- An orchestrator waiting for a usage limit to reset dozes at its desk, and the sign on the Control room wall reads "Resumes 15:03", "Resets 15:03" or "Usage limit". The orchestrator says a new limit line in a bubble; other system lines stay silent.
- A child run launched by another agent sits next to its parent when both are in the "Other work" room.
- A finished topic loses its room: the judgment can close it (a confirmed final, nobody tied to it working, not an estimated room) and 20 minutes have passed since its last activity, or the topic has no participants. Those people are counted as "N earlier". An agent that is thought to work in a topic (no file yet) sits in its room; the others that were launched together sit side by side in "Other work". The Debates card keeps every topic.
- When you give an instruction a "!" appears over your head and the orchestrator walks to your office to listen. It also goes to your office to report, or to ask a choice question (AskUserQuestion), with a "?" mark.
- A letter flies for an orchestrator → agent message, a document for a final report. For a cross-review (reading another participant's `r<N>/<X>.md`) the document flies from the author to the reader; when an agent sends SendMessage to another agent it walks next to the receiver and tells it in a bubble.

**Lounge**: people who are resting pick a facility every 150 seconds (20 in a demo) and walk to a new place when the slot changes. The facilities are sofas, tea tables, game machines, treadmills, dumbbells and a coffee machine, and how many there are depends on the width (one sofa unit per 6 people, sized for up to 24). Those who do not fit are counted as "+N more". Someone who finished more than 30 minutes ago picks the sofa more often and may doze on it. People who failed, were stopped or ended take a sofa first (their status icon stays).

**Letters and icons**
- A brass nameplate on the front of the desk has the seat letter (`A`, `B`, `codex1`); the name tag under the desk has a status dot · model (`opus5.5`) · title. The full description shows when you hover.
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

### A Codex orchestrator

A Codex thread that nothing else started (a TUI, Desktop or exec thread, in the project list with the ◆ mark) has a team on its page like a Claude orchestrator: everything it started, directly or through others, stands under it, and the page is the one of the top orchestrator whichever of its runs you open by id.

| What | How |
|---|---|
| Native sub-agents | Each sub-agent thread of Codex is a card. Its name tag is the end of its path (`/root/s1` → `s1`) and its title its nickname. The instruction it got is not on screen: Codex stores what the parent told it as ciphertext, so the spawn card says so (the plain note stored beside it, which only names a path, is never shown). The front of a sub-agent's transcript is the parent's history, copied when it was spawned; it is not read as the sub-agent's own turns, tools, instruction or tokens |
| Conversation | The parent's transcript tells what passed between the threads: the spawn, the messages the parent sent (the first one is the spawn itself), the messages between agents, and the sub-agent's final report, the first message it sends its parent after it is done. Waiting for an agent is no event. Read them in the "Agent talk" card |
| State | From the sub-agent's own transcript: *done* when its turn ended (whatever its parent does, and an interrupt recorded after that changes nothing), *failed* on an error, *interrupted* when its parent interrupted it while it worked, working while its turn is open. A sub-agent has no process of its own: its life is the Codex process of the root thread, so an open turn with no such process is *ended* |
| `claude -p` and `codex exec` runs | Started by the root thread's shell or by a sub-agent's shell (then the card stands under that sub-agent). They are linked by the evidence of the ranking below: the output file, the environment of the child (`CODEX_THREAD_ID` names the thread whose shell ran it, a sub-agent too, and `CODEX_SESSION_ID` its root), the process lineage, and the command text (Codex writes each command as a `CommandExecution` with the command line and the folder when its process ends; the child's instruction is matched against that text). When both providers' names are in one environment (a Claude session started the Codex run whose shell started the child), the nearest agent decides |
| Tokens | A sub-agent's tokens are on its own card; the approval-review threads of the orchestrator are its "Approval review" tokens and nothing else. The page total is the sum of the cards, as before |
| Debates | Cells like any debate, from what these runs wrote: a `claude -p` that writes `talk/r1/A.md`, a `codex exec -o talk/r1/B.md`. While a `codex exec` runs, its `-o` file is known from its own command line (Linux) or from the call that started it; a relative `-o` counts from the folder the thread started in, not when the command moves with `-C` / `--cd`. A `claude -p … > FILE` of a foreground call of a Codex shell is known while it runs when that call holds one literal command. Otherwise the file is known when the command has ended |

What a Codex orchestrator's page cannot show:

- A Codex shell ends the child of a command started with `&` (or `nohup … &` without `setsid`) when the call returns. Nothing is left to read: it shows as an `orphan_launch` count in Diagnostics and gets no card. A child started with `setsid nohup … &`, in the foreground or in a Codex PTY session is found.
- A command's record is written when its process ends. While a foreground `codex exec` still runs, it is known by its environment or process only, and a command that outlived its turn has no record. While a Codex thread has a command that cannot be read yet, a Claude run of the same folder that was not started by that thread, and that rests on its instruction text alone, is a guess (`fingerprint_incomplete`) until the record arrives.
- A Codex thread of a kind the dashboard does not know (a sub-agent whose transcript does not say where the parent's copied history ends, say) is hidden, and command records it could not read are skipped: both are `format_drift` in Diagnostics.
- Where there is no `/proc` (macOS) the processes of the children cannot be told, and only the environment (read with `ps`) and the records link them.

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

When a Claude session, or one of its agents, runs `claude -p …` (or `--print`) through Bash, the Claude session that appears is attached as an **agent** of the session that started it. It is not listed on its own and is counted in the parent session's agent count. The agent that launched it is the one whose transcript holds the command: a child started by an agent stands under that agent, and a child started by another child (a `claude -p` inside a `claude -p`) stands under that child, one step further in per level (eight levels at most), and so do the sub-agents (Agent tool) that a child started itself. An agent whose parent is not in the list on screen, for instance with the "This debate" filter, stands alone with a "↳ launched by …" line.

**How a child is linked.** The evidence is ranked, and the highest rank that gives one answer decides. Which session started it (the tree) and which agent inside that session did (the node) are decided separately.

| Evidence | Rule name | Shown as |
|---|---|---|
| The launching command's output file (`> out.json`, `-o`) holds this run's session id, or its text names the id (`--session-id`, `--resume`) | output file · id | certain |
| A live process: its parent chain leads to the session, or `CLAUDE_CODE_SESSION_ID` / `CLAUDE_PID` in its environment name it (for a child started by a Codex shell, `CODEX_THREAD_ID` / `CODEX_SESSION_ID` name the thread and its root); or a link remembered in `links.json`. A Claude session is settled by these alone (which agent inside it launched the child is then read from the command, and falls back to the main session); the environment of a Codex shell also names the thread whose shell ran the child, so a native sub-agent is the node as well. Names that a tmux server carried on from an older shell are not taken as proof: when the session they name ran nothing that could start the child and another one's launch fits its words, they count for nothing and `evidence_conflict` says so; a Codex thread nothing is known of makes a match of the words a guess | process lineage · environment variable · saved link | certain |
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
- A detached child (`setsid nohup claude -p … &`) has init (pid 1) as its parent and cannot be found by ancestry, but the environment of a process started by the Bash tool still holds the id of the Claude session that started it (`CLAUDE_CODE_SESSION_ID`) and its pid (`CLAUDE_PID`). **Only these four names** (`CLAUDE_CODE_SESSION_ID`, `CLAUDE_PID`, `CODEX_THREAD_ID`, `CODEX_SESSION_ID`) are read from the environ of a live `claude -p` child and a Codex process (no other environment value is stored, logged or put in a response). The environment's session id is not used if it is that process's own session or if the live Claude behind `CLAUDE_PID` has a different session (the session was switched). On Linux it reads `/proc/<pid>/environ`, elsewhere `ps -E`, for processes of the same user only, and "unknown" when that does not work.
- Several children started by one loop line (`for x in a b c; do … claude -p … & done`): one launch per iteration when the count is known (`for x in a b c`, `{1..5}`, nested loops multiply up to 64), any number when it is not (`while`, `until`, `$(…)`, a glob). A command cannot take more children than it can start, so look-alikes beyond that stay unlinked.
- Reading scripts: when a Bash command runs a script file in a command position (`bash`, `sh`, `zsh`, `source`, `.`, `./file`, running by path) the `claude -p` and `codex exec` inside that file are examined by the rules above (the working folder follows the `cd` scope of that call; `$HOME`, positional arguments and `VAR=value` are expanded inside the script only). Only one level deep, so a script that calls a script is not followed. The file is opened under the same policy as the document viewer: regular files only, up to 256 KB, dot paths and secret-looking names are not read, and anything unreadable is skipped silently. A script that could not be read might or might not start a session, so an instruction found in that session's text then makes only a guess, never a certain link.
- A launch command that left no session record at all (a deleted transcript, `--no-session-persistence`, a child that a Codex shell ended with its call) shows only as `orphan_launch` in Diagnostics. No card and no seat is made for it.
- With no role tag the model name (`opus5.5`…) is used and models that repeat are numbered in start order, so a `claude -p` session can shift the numbers of the other agents of the same session.
- Conversation: the first user instruction of the child session becomes the body of a `spawn` event, an instruction continued with `--resume` shows as `orch_msg`, and the last assistant text when a turn ends with `end_turn` shows as `handback`. Read them in the "Agent talk" card.
- Debate cell assignment does not change (the name is for the screen).

## How debates are recognized

The board reads **which folders exist and which files were really written, and by whom**. It reads no sentence of an instruction, a brief or a report (only the first heading of a brief and the table of its documents are shown). So there is nothing to word in a particular way: have each agent write its own file into the folder, and start them together.

```
research/                    ← common brief (optional): title, table of topics
  brief.md
  t1_naming/                 ← topic folder: it has round folders
    brief.md                 ← optional: its first heading is the topic's title
    r1/A.md  r1/B.md         ← round 1: one file for each participant (the file name is the row)
    r2/A.md  r2/B.md         ← round 2
    rulings.md               ← the end: a document written after the last report
```

**What is a debate.** A folder on disk with a round folder in it (`r1`, `r01` or `round1`). It is listed when an agent of this session wrote a file of a round (or was asked to, below) · an agent carries a room tag for it · the orchestrator itself wrote a guide or a round into it (Claude Code: `Write`, `Edit`, `MultiEdit`, or a `Bash` redirect, `tee` or `mkdir` that worked; Codex: a completed `FileChange`, a command that exited 0; the last 64 folders are kept) · the background walk of the repository found it (about once a minute; never `/tmp`, your whole HOME, or a folder of many repositories; when its budget runs out Diagnostics notes `listing_capped`). A `brief.md` alone or a review without qualifying rounds is no debate: give it an `r1/` folder, or start two or more of its agents with `BULLPEN_ROOM` (below). Reading a folder lists nothing. A write the shell may not have run (after `||`, in an `if`, a `{ }` group, a function that may not have run …) does not count.

**What counts as a write.** A `Write`, `Edit`, `MultiEdit` or `NotebookEdit` whose result is not an error · a Codex `FileChange` that completed and added or changed a file · a shell `>`, `>>`, `tee` or heredoc in a command that worked (when the exit status cannot vouch for it, because other commands follow, the file must also have been saved while the command ran). The output of a launch is not the launcher's write: `codex exec -o FILE` and `claude -p … > FILE` ask the **launched run** to save a file, and count when that run ended well and the file was saved in its time; `claude -p … | tee X`, `codex exec … > X`, what follows a launch in the same pipe, and the redirect of a `( )`/`{ }` group or `bash -c` that holds a launch are nobody's write (a write inside the group, after the launch, is still the launcher's; behind a prefix the board cannot read, `env -S` or `xargs`, a launch is taken from its words and its redirect is no sure write either). Not seen: Python's `write_text`, `cp`, `mv`, anything outside the records. A write that failed or whose result is not known is no evidence.

Codex native agents spawned in the same parent turn are launched together; without round folders, `round<N>_<seat>.md` / `r<N>_<seat>.md` (case-insensitive) also qualify with two seat names and two session agents’ confirmed tool/shell writes, except in broad folders such as `/tmp`.

**A cell** is (topic, round, seat name); in round folders the seat is the name before `.md`. Whoever's **first successful write created the file** owns it. Anyone else who wrote it is "fixed by" (the orchestrator too, who never owns; a file taken over lists the owner it took it from first, then the others by their first write). A file that was only edited or appended to has no owner. Nobody owns a file when two agents created it within a second, or when an earlier attempt or an unread stretch of a record could have come first (Diagnostics `seat_tie_held`, `history_lost`). A later run takes a file over from an earlier one only when the earlier one was over before the later started and the later was asked to save that file and wrote it. A cell is **submitted** when its owner's file is there and, if the owner was asked to save it in its current run (an `-o` or `>` of its launch command, a seat tag), it was. A resumed run with no new request leaves what it submitted before as it was. The grey **previous file** is a file nobody owns, or one the owner was asked to save again and has not: it opens and is never a submission.

**Agents that hold no cell** are shown under the table as "Working · estimated (why)", or in grey "Ended · no file", when one of these points at the topic: launched together with an agent that holds a cell there (the same message, or for Codex the same `exec` call or native spawns in the same parent turn) · the call that launched it made the folder · it read the guide of the topic or of its bundle (`brief.md`, `README.md`, `index.md`). An agent that wrote a file of the project elsewhere, one that points at two bundles (Diagnostics `launch_split`) and one that wrote the confirmed final of the topic is not listed. An estimate changes nothing: no cell, no current debate, no final; it only keeps the topic from looking finished while somebody works, and is not counted as a member of the debate (token card, filter). A room tag makes it sure ("Working · room tag"). Agents launched together and not placed stand together on "Other work".

**A file nobody owns** shows "probably written by X" (a guess marked as one) when it was saved while exactly one command of one agent was running and that command did not read it. It owns nothing, submits nothing.

**Rooms: work that is not shaped like a debate.** A folder with no round folder where people work together. It is a **room with a room tag** (sure) when two or more agents carry `BULLPEN_ROOM=<that folder>`; it is an **estimated room** when two or more agents launched together each wrote a `.md` of their own in the folder (not a file everybody writes, not an instruction file, and not when most of them changed the project outside the folder), or, with no file written at all, when they message each other and all read one `.md` of the folder. A room has one round and a cell for each member's own file; its folder is the one that holds those files themselves. An estimated room is marked, is never the current debate and never closes.

**Tags (optional): choosing the room by hand.** Put the variables on **the launch command itself**:

```
BULLPEN_ROOM=/path/to/folder BULLPEN_SEAT=r2/B codex exec …
```

`BULLPEN_ROOM` names the folder (it must exist) and makes the agent a sure participant of the debate it names; two agents with it in a folder with no round folder make a room. `BULLPEN_SEAT` names its file: `B` (the file `B.md` of a room) or `r2/B` (round 2 of a debate; a bare `B` in a debate only makes it a participant, since the round is not guessed); the cell waits for that file ("Writing") from the start. A `ROOM` that names a bundle (a folder whose subfolders are the topics) is no room and its seat makes no cell: name the topic. The board takes only what the command **passes to the run**: assignments in front of the launch, or an `export` before it in the same straight line of the shell (not an assignment that was never exported, and nothing after `unset`, `env -u` or `env -i`, from a branch that may not have run, or from a subshell that ended). When one Bash call launches several runs, each takes the values of its own piece; if the pieces cannot be told apart and the values differ, nobody gets a tag. A value the run only inherited from the orchestrator or from the agent that launched it is no tag, and a run resumed without the tag loses it. A running process is read from its environment (Linux `/proc`, macOS `ps -E`), a run that has ended from its launch command. Limits: an agent started with the **Agent tool** (Claude) and a **Codex native sub-agent** cannot carry variables; a value in the command with a `$` left in it is not read; on **macOS** only a folder whose path has no space is read from a running process. A tag naming no folder is ignored.

**The end of a topic.** The topic is closed by a **confirmed final**: one `.md` directly in the topic folder (for a bundle, in its folder or its `final/`; in a room a member's own file is no candidate, but one written or changed around or after the final competes) that an agent or the orchestrator wrote **with a tool or a shell command** after the last report (a file an `-o` or a launch's `>` saved is a cell, never the end), with every cell handed in, no empty round folder, nobody tied to the topic still working, and not an estimated room. Another document that is there, or that appeared or changed with no write event, competes (`more than one document could be it`); a write to a report or to a candidate after it undoes it. Anything else reads **"Closing not confirmed"** with the reason (`nothing handed in yet`, `a cell is not handed in`, `no document confirmed after the last report`, `more than one document could be it`, `a round folder holds no report`, `someone tied to it is still working`, `an estimated room has no final`, `part of a record could not be read`) and the documents that could be it (names like `final`, `ruling`, `conclusion` come first: a sort and nothing else). When the final of the bundle is confirmed, a topic of it that nobody tied to it still works on reads "Closed by the bundle's final". The stage "Final ready", the "Done" of a room and the closing of its office room (20 minutes after the last activity) go by the judgment alone. A final in a dot folder or with a secret-looking name is not opened by the page, so for the page it is not confirmed.

**The brief table** of the common `brief.md` (the first row is the header; a row is read when its folder cell is a backticked `` `t1_naming/` ``) gives the prerequisites ("depends on" column) and the file it names as the final (`` `final/t1_naming.md` ``). It is shown as "Named in the brief", nothing more.

```
| Topic | Folder | Depends on | Final deliverable |
|---|---|---|---|
| Naming | `t1_naming/` | — | `final/t1_naming.md` |
```

**One folder is shown once.** The same real folder reached by two paths (a link into it) is one debate. The same place of a repository in its linked worktrees is not folded: each copy that has a cell, a tag, a room or a guess of its own stays, and one with nothing of its own is hidden when a copy with something is there. When no copy has anything of its own, one stands for them (the main checkout's, else the shortest real path). The tab says how many copies were folded. A folder that holds only an instruction (no cell, no final) inside the folder of a debate that shows something is a part of that debate's records.

**Names**: the topic name is the first heading of the topic `brief.md`; a row is named for its file; the agent's name on screen is its tag (the report name for Codex), else the model name (`opus5.5`, `sol6.1`). Nothing is taken from the words of an instruction.

## Token accounting

| Value | Meaning |
|---|---|
| Current context | Tokens in the last API call (new input + cache reads + cache writes). How full the window (200k or 1M) is |
| Total input | Input tokens summed over all calls so far. Mostly cache reads |
| Output | Tokens generated, thinking included |
| Advisor model | An advisor model called inside a response (`advisor_message` in `usage.iterations`). It is not in the parent's totals, so it is counted separately |
| Cost | Dollars at each model's API list price. Cache writes are priced separately for 5 minutes and 1 hour, with the fast-mode (×2) and US-only inference (×1.1) multipliers. Advisor model cost is included |

A Codex sub-agent's tokens are counted on its own card, not in its orchestrator's "Approval review" tokens, and the page total does not change.

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
| `path_unresolved` | Check | The path of a launch could not be worked out (it uses a variable). |
| `alias_collision` | Check | One round has two folders (`r01` and `r1`). |
| `seat_tie_held` | Check | More than one agent could have been first to write this cell (created within a second, or an earlier try), so it has no owner. |
| `launch_split` | Check | Launched in a way that points at more than one debate folder, so it is placed in none. |
| `history_lost` | Check | Part of its record could not be read, so who first wrote a cell is left open. |

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
- **A cut last line is skipped.** A process that died while writing leaves a half line at the end of its record. It is no hole: if it was the first write of a cell, another agent may be shown as its owner. Any other unreadable write line holds back what was first written after it (`history_lost`).
- **A big Codex rollout is scanned from the front once.** The front of a rollout over 64 MB is not scanned again while the file only grows (the same file, larger): that assumes Codex only appends. A file that shrank, was replaced or changed in place is scanned again, and until then its front counts as unread.
- **A very big session opens more slowly.** The events of the shell commands are made at the first read: a session of about 300 agents took about 4 s longer than in 0.2.1, most sessions under a second. Making them after the first screen is left for 0.3.1.
- **A running agent keeps its last round open.** An agent that is still running and whose only cell is in an earlier round shows that cell as a draft until it ends or writes in a later round; a file changed with no write event during a resumed run stays "Submitted".
- **A cell, and a final, look at the file, they do not open it.** Presence, size and time are all they ask, so a report in a hidden folder (a working folder joined by a link, for example) or named like `token_budget.md` still shows as "Submitted". The document itself does not open when you click it because of the rule above (403). A link that points at a credential file, and anything that is not a regular file (FIFO, device, folder), counts as no file. If a link points at a dot path or a secret-looking name the cell still stands, but the line count is counted by opening the file, so it shows 0. A final in such a place is not a confirmed final for the page (the page could not open it), and what is named in the brief table is only shown.
- **What is found by listing a folder is strict.** Dot-path and secret-named files do not appear among the candidates for a final, in the list of documents of a topic folder, or in the list of the `final/` folder.
- A session id accepts only the UUID shape (`session=*` and `../x` give 404; `--session` is the same). A bad numeric parameter (`t`, `since`, `before`, `limit`, `idx`) gives 400 (`limit` of `/api/talk` is 1–300, `scope` only none, `user` or `agents`).
- A Codex thread with more than 200 turns still shows the events of every turn (instructions, final reports) in the flow. The turn list in the details panel has only the last 200.
- The same user instruction recorded again within 120 seconds (a queued/human double record) counts once. Only when the whole text is the same and the times are within 120 seconds.
- An agent's thinking is not in the transcript and is not visible. Judge by tool calls, remarks, messages and reports.
- "Read by" (cross-review) catches only what was read with the `Read` tool; what Bash `cat` read is not known. For Codex it uses the command interpretation Codex itself records (read in `parsed_cmd`).
- A `claude -p` or `codex exec` started by an agent's own Bash call is found in that agent's transcript and stands under it; the main transcript's Bash calls are read the same way. A Desktop session shows "Status unknown" when its process cannot be identified.
- The completion notice of an agent started by another agent (a grandchild: meta has `parentAgentId` and `spawnDepth`) is kept in **the parent agent's transcript**, not in the main one. Its status comes from that notice (task-notification) and from the completion result the parent's `Agent` call got back (`status` completed, failed or killed, an error result counts as failed; the background start acknowledgement `async_launched` is not a completion); if the parent has ended and no notice exists it counts as "Session ended", but not while the parent is only Interrupted. A detached `claude -p` run keeps its own process, so it goes on when its parent ends.
- Done and failed are decided from the notices in the main transcript, not by the agent itself, so a late notice leaves it "Working" for a moment.
- The transcript format is that of Claude Code 2.1.28x. Some judgments rest on markers that Claude Code does not document (the closing record of a process, the usage-limit records, the start of a run); they are known for versions 2.1.235 to 2.1.286. A version outside that range says nothing by itself (every Claude Code release would). When a marker or field that the version is known to carry is gone, or the record's version is not a version number, Diagnostics notes `format_drift`, the dashboard falls back to weaker evidence (the process, the last lines), and some items may look empty. Line types it does not know are ignored.
