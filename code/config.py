"""Load config.yaml and resolve project paths relative to the repository root."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def load_config(path: Path = ROOT / "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    for key, rel in cfg["paths"].items():
        cfg["paths"][key] = ROOT / rel
    return cfg
