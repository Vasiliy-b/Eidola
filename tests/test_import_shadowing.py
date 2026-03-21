"""Regression test: detect local imports that shadow module-level names.

Root cause of the exit-1 crash (UnboundLocalError on `time`) was a redundant
`import time` inside analyze_feed_posts() at line 3518. Python's compiler marks
the name as local for the entire function, so earlier `time.sleep()` calls
fail with UnboundLocalError when the branch containing the import hasn't
executed yet.

This test parses the AST of firerpa_tools.py and flags any local import that
shadows a module-level import — the exact pattern that caused the crash.
"""
import ast
from pathlib import Path

FIRERPA_TOOLS = Path(__file__).resolve().parent.parent / "src" / "eidola" / "tools" / "firerpa_tools.py"

# Module-level imports that MUST NOT be re-imported locally
MODULE_LEVEL_NAMES = {"time", "os", "logging", "io", "random"}


def _collect_module_imports(tree: ast.Module) -> set[str]:
    """Return names imported at module level (top-level statements only)."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
    return names


def _find_local_shadowing(tree: ast.Module, protected: set[str]) -> list[dict]:
    """Find local imports inside functions that shadow protected names."""
    violations = []
    
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        func_name = node.name
        for child in ast.walk(node):
            if isinstance(child, ast.Import):
                for alias in child.names:
                    imported_name = alias.asname or alias.name.split(".")[0]
                    if imported_name in protected:
                        violations.append({
                            "func": func_name,
                            "name": imported_name,
                            "line": child.lineno,
                        })
            elif isinstance(child, ast.ImportFrom):
                if child.module:
                    top_module = child.module.split(".")[0]
                    if top_module in protected and child.level == 0:
                        for alias in child.names:
                            as_name = alias.asname or alias.name
                            if as_name in protected:
                                violations.append({
                                    "func": func_name,
                                    "name": as_name,
                                    "line": child.lineno,
                                })
    return violations


def test_no_local_import_shadows_module_time():
    """Critical: no function may have `import time` locally (caused UnboundLocalError)."""
    source = FIRERPA_TOOLS.read_text()
    tree = ast.parse(source, filename=str(FIRERPA_TOOLS))
    
    violations = _find_local_shadowing(tree, {"time"})
    
    assert not violations, (
        f"Local `import time` found — this WILL cause UnboundLocalError!\n"
        + "\n".join(
            f"  {v['func']}() line {v['line']}: import {v['name']}"
            for v in violations
        )
    )


def test_no_local_import_shadows_module_level():
    """Broader check: no local import should shadow any module-level stdlib import."""
    source = FIRERPA_TOOLS.read_text()
    tree = ast.parse(source, filename=str(FIRERPA_TOOLS))
    
    violations = _find_local_shadowing(tree, MODULE_LEVEL_NAMES)
    
    assert not violations, (
        f"Local import(s) shadow module-level names — risk of UnboundLocalError:\n"
        + "\n".join(
            f"  {v['func']}() line {v['line']}: import {v['name']}"
            for v in violations
        )
    )


def test_firerpa_tools_parses_cleanly():
    """Smoke test: the module parses without syntax errors."""
    source = FIRERPA_TOOLS.read_text()
    ast.parse(source, filename=str(FIRERPA_TOOLS))
