from __future__ import annotations

import json
from pathlib import Path


def _compact_json(value: str) -> str:
    parsed = json.loads(value)
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))


def convert_env(source: Path, destination: Path) -> None:
    lines = source.read_text().splitlines()
    output: list[str] = []
    index = 0

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if not stripped or stripped.startswith("#"):
            output.append(line)
            index += 1
            continue

        if "=" not in line:
            output.append(line)
            index += 1
            continue

        key, value = line.split("=", 1)
        raw_value = value.strip()

        if raw_value.startswith("[") or raw_value.startswith("{"):
            candidate = raw_value
            index += 1
            while True:
                try:
                    output.append(f"{key}={_compact_json(candidate)}")
                    break
                except json.JSONDecodeError:
                    if index >= len(lines):
                        raise
                    candidate += lines[index].strip()
                    index += 1
            continue

        output.append(line)
        index += 1

    destination.write_text("\n".join(output) + "\n")


def main() -> None:
    source = Path(".env")
    destination = Path(".docker.env")
    convert_env(source, destination)
    print(f"Wrote {destination}")


if __name__ == "__main__":
    main()
