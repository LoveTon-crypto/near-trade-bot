import ast, re, sys, json
CYR = re.compile(r"[А-Яа-яЁё]")
out = {}
for fn in sys.argv[1:]:
    tree = ast.parse(open(fn).read())
    skip = set()
    for node in ast.walk(tree):
        # docstrings
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            b = node.body[0]
            if isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant):
                skip.add(id(b.value))
        # log.x(...) calls
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) \
                and node.func.value.id in ("log", "logging"):
            for a in ast.walk(node):
                skip.add(id(a))
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and CYR.search(node.value):
            for part in node.value.split("\n"):
                if CYR.search(part):
                    out.setdefault(part, fn)
print(json.dumps(out, ensure_ascii=False, indent=0))
