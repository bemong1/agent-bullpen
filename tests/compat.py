"""Helpers that let a test use server (single-file code or the board/ package) with the same calls. AB_SRC can point at another copy of the tree.

A test that changes module globals (HOME, DENY_FILES, LINKS ...) changes every module that holds that global:
in single-file code that is server alone, in the package it is server and whichever board.* modules have that name."""
import contextlib
import os
import sys
from unittest import mock

sys.path.insert(0, os.environ.get('AB_SRC') or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402


def holders(name):
    """The modules that hold a global called name."""
    mods = [server] + [m for n, m in sorted(sys.modules.items()) if n == 'board' or n.startswith('board.')]
    return [m for m in mods if name in vars(m)]


@contextlib.contextmanager
def patched(**values):
    """with patched(HOME='/x', LINKS=idx): replaces the global in every module that holds that name, then puts it back."""
    with contextlib.ExitStack() as st:
        for name, value in values.items():
            mods = holders(name)
            if not mods:
                raise AttributeError('no module defines %r' % name)
            for m in mods:
                st.enter_context(mock.patch.object(m, name, value))
        yield


def start_patches(case, **values):
    """For unittest setUp: turns patched(...) on and off again at cleanup."""
    cm = patched(**values)
    cm.__enter__()
    case.addCleanup(cm.__exit__, None, None, None)


@contextlib.contextmanager
def terminal_lang(lang='ko'):
    """Pins the terminal language of an in-process run like AGENT_BULLPEN_LANG pins a server process: the variable is set (main() reads it), the module settings
    are set as if main() had run (for callers of bind_servers, host_hint ... that skip main), and everything is put back afterwards: main() rebinds the
    module settings, which would leak into the next test. lang=None only does the restoring."""
    from board import i18n          # here, not at the top: AB_SRC may name a copy that predates board/i18n.py (the other helpers still work there)
    with contextlib.ExitStack() as st:
        st.enter_context(mock.patch.object(server, 'CLI_LANG', server.CLI_LANG))
        st.enter_context(mock.patch.object(server, 'cli_t', server.cli_t))
        st.enter_context(mock.patch.object(i18n, 'CLI_LANG', i18n.CLI_LANG))
        if lang:
            st.enter_context(mock.patch.dict(os.environ, {'AGENT_BULLPEN_LANG': lang}))
            server.set_cli_lang(lang)
        yield
