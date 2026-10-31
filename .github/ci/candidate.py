"""Read the canonical candidate version without importing the application."""

import ast
import re
from pathlib import Path

tree = ast.parse(Path("src/dashboard.py").read_text(encoding="utf-8"))
versions = [node.value.value for node in ast.walk(tree) if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets)]
assert len(versions) == 1 and re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", versions[0])
wheels = list(Path("dist").glob("*.whl"))
assert len(wheels) == 1
print(f"version={versions[0][1:]}")
print(f"wheel={wheels[0].resolve()}")
