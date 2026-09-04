"""The two promises the packaging makes, checked rather than intended.

sutando will consume a *version* of this package, not a copy of it, and it runs
on Python 3.9 and takes no dependency trees. Both are therefore contract, and
both are the kind of contract that erodes one convenient import at a time — so
the dependency ledger below is explicit, and adding to it is meant to be a
deliberate act.

The third check is I3: no gateway URL is compiled into this package. The base
URL is discovered at provisioning time and travels with the credential; a
default here is what let one module keep talking to a renamed service.

Run: python3 tests/test_packaging.py
"""
import _bootstrap  # noqa: F401 — distribution root on sys.path
import ast
import importlib
import re
from pathlib import Path

import ag2_relay_client

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


ROOT = _bootstrap.ROOT
PACKAGE = ROOT / "ag2_relay_client"
SOURCES = sorted(PACKAGE.glob("*.py"))

#: Everything this library is allowed to import. Standard library only, and a
#: short list of it — every entry here is a deliberate decision.
ALLOWED = {
    "__future__", "base64", "collections", "contextlib", "ctypes", "dataclasses", "email",
    "errno", "fcntl", "hashlib", "http", "io", "json", "logging", "mimetypes", "msvcrt",
    "math", "os", "pathlib", "queue", "re", "shutil", "socket", "ssl", "stat", "sys",
    "tempfile", "threading", "time", "traceback", "types", "typing", "urllib",
    "uuid",
}
#: `fcntl` and `msvcrt` are platform-specific halves of the shared lock module;
#: each is imported only by the backend selected for that platform.

# --- no third-party imports, and no asyncio
for source in SOURCES:
    tree = ast.parse(source.read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    stray = roots - ALLOWED - {"ag2_relay_client"}
    check(not stray, f"{source.name} imports only from the ledger" +
          (f" (stray: {sorted(stray)})" if stray else ""))
    check("asyncio" not in roots, f"{source.name} is free of asyncio")

# --- every module imports cleanly (this suite runs under the 3.9 floor)
for source in SOURCES:
    if source.name == "__init__.py":
        continue
    imported = True
    try:
        importlib.import_module(f"ag2_relay_client.{source.stem}")
    except Exception as exc:  # noqa: BLE001
        imported = False
        print(f"    ({exc})")
    check(imported, f"ag2_relay_client.{source.stem} imports")

# --- I3: no gateway URL is compiled in
def string_constants(tree):
    """Every string literal that is not a docstring."""
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docstrings]


for source in SOURCES:
    literals = string_constants(ast.parse(source.read_text(encoding="utf-8")))
    urls = [s for s in literals if re.search(r"https?://\S", s)]
    check(not urls, f"{source.name} compiles in no gateway URL" +
          (f" (found: {urls})" if urls else ""))
    hosts = [s for s in literals if "ag2.space" in s]
    check(not hosts, f"{source.name} names no deployment host")

# --- the packaging metadata says what the docstring says
pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
check('requires-python = ">=3.9"' in pyproject, "the declared floor is 3.9")
check("dependencies = []" in pyproject, "the dependency list is empty")
check('name = "ag2-relay-client"' in pyproject, "the distribution is ag2-relay-client")
check('attr = "ag2_relay_client.__version__"' in pyproject,
      "the version has one source — the package, read dynamically")
check(re.fullmatch(r"\d+\.\d+\.\d+", ag2_relay_client.__version__) is not None,
      f"__version__ is a release version ({ag2_relay_client.__version__})")
check((ROOT / "README.md").is_file(), "the readme pyproject points at exists")

print("\n" + ("PASS — packaging green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
