# Undefined-global check with stdlib symtable: every global a function reads must be defined at module level or be a builtin.
# usage: python3 names_check.py server.py board/*.py
import sys, symtable, builtins
def check(path):
    src = open(path, encoding='utf-8').read()
    top = symtable.symtable(src, path, 'exec')
    defined = {s.get_name() for s in top.get_symbols() if s.is_assigned() or s.is_imported() or s.is_namespace()} | {'__file__', '__name__', '__doc__', '__spec__'}
    bad = []
    def walk(t):
        for s in t.get_symbols():
            if s.is_global() and s.is_referenced() and s.get_name() not in defined and not hasattr(builtins, s.get_name()):
                bad.append((t.get_name(), t.get_lineno(), s.get_name()))
        for c in t.get_children():
            walk(c)
    walk(top)
    return bad
rc = 0
for p in sys.argv[1:]:
    for fn, line, name in check(p):
        print('%s:%d %s uses undefined global %r' % (p, line, fn, name)); rc = 1
print('ok' if rc == 0 else 'FAILED')
sys.exit(rc)
