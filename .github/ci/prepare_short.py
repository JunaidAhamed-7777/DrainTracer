"""A valid two-second recipe with zero warm-up and an unfillable ten-second window."""

import json
import sys
from pathlib import Path

path = Path(sys.argv[1] if len(sys.argv) > 1 else ".github/ci/workload.json")
recipe = json.loads(path.read_text(encoding="utf-8"))
configuration = (path.parent / recipe["config"]).read_text(encoding="utf-8")
assert 'warmup = "2s"' in configuration
(path.parent / "short.toml").write_text(configuration.replace('warmup = "2s"', 'warmup = "0s"'), encoding="utf-8")
recipe.update(timeout=2, config="short.toml")
(path.parent / "short.json").write_text(json.dumps(recipe), encoding="utf-8")
