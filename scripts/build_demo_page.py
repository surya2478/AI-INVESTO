"""Render the home-screen preview by injecting real computed data into the template.

Generated rather than hand-written so every number on the page traces back to
reports/today.json, and regenerating after an ingest keeps the mockup honest.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "app" / "demo" / "home_demo.template.html"
PAYLOAD = ROOT / "reports" / "today.json"
OUT = ROOT / "reports" / "home_demo.html"


def main() -> int:
    if not PAYLOAD.exists():
        print("run `python -m engine.demo_data` first", file=sys.stderr)
        return 1

    data = json.loads(PAYLOAD.read_text(encoding="utf-8"))

    # Sparklines dominate the payload; thin them to keep the page small.
    for theme in data["themes"]:
        for key in ("india_spark", "global_spark"):
            theme[key] = [round(v, 1) for v in theme[key]]

    html = TEMPLATE.read_text(encoding="utf-8").replace(
        "__DATA__", json.dumps(data, separators=(",", ":"))
    )
    OUT.write_text(html, encoding="utf-8")

    print(f"wrote {OUT}  ({OUT.stat().st_size / 1024:.0f} KB)")
    print(f"  as of {data['as_of']} | {data['securities']} securities "
          f"| {len(data['themes'])} themes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
