import ast
from pathlib import Path


def test_lifespan_seeds_default_rules_before_polling():
    tree = ast.parse(Path("main.py").read_text(encoding="utf-8"))
    lifespan = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan"
    )
    calls = [
        node.value.func.id
        for node in lifespan.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
    ]

    assert "seed_default_rule" in calls
    assert calls.index("init_db") < calls.index("seed_default_rule")
