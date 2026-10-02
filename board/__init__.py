"""Server modules of the Claude Code · Codex multi-agent board (server.py bundles them and serves them over HTTP)."""

# values of the --claude-config-dir and --codex-home command arguments. When server.py is run directly, it fills this in before importing board.util:
# util fixes the paths once at import time, and other modules copy those values by name.
PATH_FLAGS = {}
