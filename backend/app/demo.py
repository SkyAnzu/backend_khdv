"""CLI: python -m backend.app.demo backend/samples/feasible.json"""

import argparse
import json
from pathlib import Path

from .models import ScheduleRequest
from .solver import solve_schedule


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the week 1-2 CP-SAT demo")
    parser.add_argument("input", type=Path, help="JSON request file")
    args = parser.parse_args()
    request = ScheduleRequest.model_validate_json(args.input.read_text(encoding="utf-8"))
    # ASCII output keeps the CLI usable with the default Windows cp1252 console.
    print(json.dumps(solve_schedule(request).model_dump(mode="json"), indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
