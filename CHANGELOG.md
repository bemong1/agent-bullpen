# Changelog

Agent Bullpen is in **beta** (0.x): the formats it reads are the private on-disk transcripts of Claude Code and Codex, which can change with any release of those tools, and options and screens may change between 0.x releases. Please report what looks wrong with the bug template (it asks for the output of `python3 tools/harvest.py <session id>`, which holds no transcript text). `python3 server.py --version` prints the release you run.

## 0.1.0 — first public release

- A live, read-only view of Claude Code and Codex sessions: the orchestrator, its sub-agents, `claude -p` and `codex exec` runs it started (grandchildren under their launcher), with status, current tool, context use, model and an API-price cost estimate.
- Who launched whom from evidence: sub-agent metadata, the child's session id in the launch's output file, process lineage and environment, the instruction text matched against a running launch; guesses are marked as such and ambiguous cases are held instead of guessed. Unlinked candidates and a diagnostics list show what could not be decided.
- Debates and other group work from the disk: topic × round × participant tables for debate folders (`brief.md`, `r1/`), with final reports found automatically; people who work together in a folder without that shape (a meeting with an agenda, a brief, a README) get a room of their own.
- Run status per run: working, done, failed, interrupted (usage limit with reset time, API error, time limit, exited early), unknown, and an orchestrator waiting for a usage limit; limit and API-error lines appear as system lines, not as the orchestrator's words.
- Plan bar: Claude usage from Claude Code's own status line (`statusline.py`; no network request, no token) or the `.claude.json` cache, Codex usage from its rollouts. The dashboard never calls Anthropic and has no code that reads a login or token.
- Reach it from other devices: `--host` takes any address or host name (`0.0.0.0`, `::`, a LAN or VPN address). Every address that is not loopback requires an access token: random at each start and printed with the addresses to open (`--token` or `AGENT_BULLPEN_TOKEN` fixes it, `--no-auth` switches the check off with a loud warning). The first visit with the token sets an `HttpOnly`, `SameSite=Strict` cookie and removes the token from the address; without the token or the cookie every page and API answers 401. Loopback needs no login, and its `Host` allow list stays. An SSH tunnel needs no option.
- One folder, one debate: the same folder reached by a link, and the copies of a repository's folder in its linked worktrees, are listed once (the copy your agents work in stands for them); an instruction-only folder inside another debate is no debate of its own. A room or a review without rounds says "Done" once every seat has handed in and nobody works, and a conclusion document of the bundle (`CLOSING.md` …) closes the topics it names.
- Pixel-art office view (people walk in through the door when they arrive, also when their arrival rearranges the rooms), agent talk card, timeline, English and Korean UI (one dictionary file per language).
- Tested on Linux (Python 3.9+), with a scenario generator that checks the board against independently computed answers; the test suite never reads your real home or `~/.cache`. macOS is experimental: the unit tests run there in CI, but it is not yet verified on a Mac.

### Known limitations

- The team view assumes a Claude Code orchestrator. Runs that a Codex orchestrator starts from its shell are not yet gathered under it; they appear as separate sessions or unlinked candidates.
- macOS is experimental (unit tests in CI, not yet verified on a Mac); native Windows is not supported.
