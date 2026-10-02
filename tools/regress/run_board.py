# Test harness: run Agent Bullpen without the Claude usage thread (no credentials read, no API calls).
# usage: python3 run_board.py <source dir> --port 8811 --host 127.0.0.1
import sys, os
src = sys.argv[1]; sys.path.insert(0, src)
import server
server.CL_USAGE.loop = lambda: None          # never read ~/.claude/.credentials.json
sys.argv = ['server.py'] + sys.argv[2:]
if '--no-link-cache' not in sys.argv and hasattr(server, 'LINK_CACHE'):
    sys.argv.append('--no-link-cache')            # never write the real link cache from a comparison run on the real HOME (old sources have no such flag)
server.main()
