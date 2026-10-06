"""Static check: every global name a function uses must exist in its module (or be a builtin).
Catches the mistake a refactor makes silently: moving code away from the import it needs.
Run from the repository root:  python gwamm_tests/check_names.py"""
import ast, builtins, dis, os, sys, types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS = [os.path.join(ROOT, "GWAMM_Vanquish.py")]
for d, _s, files in os.walk(os.path.join(ROOT, "Sources", "gwamm")):
    TARGETS += [os.path.join(d, f) for f in files if f.endswith(".py")]


def module_names(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            out |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, ast.Global):
            out |= set(node.names)
    for node in tree.body:
        for sub in ast.walk(node) if not isinstance(node, (ast.FunctionDef, ast.ClassDef)) else [node]:
            if isinstance(sub, (ast.FunctionDef, ast.ClassDef)):
                out.add(sub.name)
            elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                out.add(sub.id)
    return out


def globals_used(code):
    for ins in dis.get_instructions(code):
        if ins.opname in ("LOAD_GLOBAL", "LOAD_NAME") and code.co_name != "<module>":
            yield ins.argval, code.co_name, ins.positions.lineno if ins.positions else code.co_firstlineno
    for c in code.co_consts:
        if isinstance(c, types.CodeType):
            yield from globals_used(c)


bad = 0
for path in sorted(TARGETS):
    src = open(path, encoding="utf-8").read()
    known = module_names(ast.parse(src)) | set(dir(builtins)) | {"__file__", "__name__", "__class__"}
    seen = set()
    for name, fn, line in globals_used(compile(src, path, "exec")):
        if name not in known and (name, fn) not in seen:
            seen.add((name, fn))
            bad += 1
            print(f"{os.path.relpath(path, ROOT)}:{line}: '{name}' is used in {fn}() but not defined in this file")
print("names ok" if not bad else f"{bad} problem(s)")
sys.exit(1 if bad else 0)
