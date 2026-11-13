"""Count broad `except Exception` handlers in proxy/ and how many log.
Definition: handler is 'logged' if its body contains a call like
<name>.<level>(...) where name matches *log* / logger / logging and
level in {debug,info,warning,warn,error,exception,critical}.
Scope: proxy/ only (624 baseline per task brief; spider-runner excluded).
Usage: python3 /tmp/count_except.py [root]"""
import ast, sys, pathlib, re
root = pathlib.Path(sys.argv[1] if len(sys.argv)>1 else "proxy")
LOG_ATTRS = {"debug","info","warning","warn","error","exception","critical","log"}
def is_log_name(n: str) -> bool:
    return bool(re.search(r"log", n, re.I))
def has_log(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute) and f.attr in LOG_ATTRS:
                v = f.value
                if isinstance(v, ast.Name) and is_log_name(v.id):
                    return True
                if isinstance(v, ast.Attribute) and is_log_name(v.attr):
                    return True
            if isinstance(f, ast.Name) and f.id == "log":
                return True
    return False
def is_broad(handler):
    t = handler.type
    if t is None: return "bare"
    if isinstance(t, ast.Name) and t.id == "Exception": return "Exception"
    if isinstance(t, ast.Tuple):
        names = [e.id for e in t.elts if isinstance(e, ast.Name)]
        if "Exception" in names: return "tuple-with-Exception"
    return None
total=0; logged=0; per={}
for p in sorted(root.rglob("*.py")):
    if "_vendor" in p.parts: continue
    try: tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError: continue
    c=l=0
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            if is_broad(node):
                c+=1
                if has_log(node): l+=1
    if c:
        per[str(p)] = (c,l)
        total+=c; logged+=l
print(f"TOTAL broad-except: {total}  with-log: {logged}  logged-ratio: {logged/total*100:.1f}%")
for k,(c,l) in sorted(per.items(), key=lambda kv:-kv[1][0]):
    print(f"{c:4d} logged={l:4d} ({l/c*100:4.1f}%)  {k}")
