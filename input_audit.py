import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def inspect_inputs():
    manifest = json.loads((ROOT / "data" / "manifest.json").read_text(encoding="utf-8"))
    counts = {}
    for table in manifest["tables"]:
        path = ROOT / "data" / table["file"]
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != table["columns"]:
                raise ValueError(f"{table['file']}: unexpected columns")
            rows = list(reader)
        key = table.get("key")
        if key:
            values = [row[key] for row in rows]
            if len(values) != len(set(values)):
                raise ValueError(f"{table['file']}: duplicate {key}")
        counts[table["file"]] = len(rows)
    return counts

if __name__ == "__main__":
    for filename, count in inspect_inputs().items():
        print(f"{filename}: {count} rows")
