# Test harness: run Agent Bullpen from a source folder (this one, or an older copy for a comparison) without anything that reads credentials or writes the real link cache.
# usage: python3 run_board.py <source dir> --port 8811 --host 127.0.0.1
import sys, os
src = sys.argv[1]; sys.path.insert(0, src)
import server
if hasattr(server, 'CL_USAGE'):               # an older source with the account usage thread: never read ~/.claude/.credentials.json
    server.CL_USAGE.loop = lambda: None
sys.argv = ['server.py'] + sys.argv[2:]
if '--no-link-cache' not in sys.argv and hasattr(server, 'LINK_CACHE'):
    sys.argv.append('--no-link-cache')            # never write the real link cache from a comparison run on the real HOME (old sources have no such flag)
server.main()
