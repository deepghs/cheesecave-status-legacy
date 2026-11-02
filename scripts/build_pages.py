#!/usr/bin/env python3
"""Assemble the files of the Pages site: the page, config.json, and a fallback copy of the data.

usage: build_pages.py --out DIR [--data-dir DIR]
The live data is NOT part of the deployment: the page reads it from the `status` branch; the copy of it
that goes along (status.json and view/) is only the fallback when the branch cannot be read.
"""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # also under `python3 -I`
import statuslib as s  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--data-dir", help="a checkout of the status branch (optional)")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    args = ap.parse_args(argv)
    if not args.repo:
        ap.error("--repo or GITHUB_REPOSITORY is required")
    cfg = s.load_config(ROOT / "status.config.json")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for f in (ROOT / "site").iterdir():
        shutil.copy(f, out / f.name)
    (out / "config.json").write_text(json.dumps(s.pages_config(cfg, args.repo), indent=1), encoding="utf-8")
    data = Path(args.data_dir) if args.data_dir else None
    if data and (data / "status.json").exists():
        shutil.copy(data / "status.json", out / "status.json")
        if (data / "view").is_dir():
            shutil.copytree(data / "view", out / "view", dirs_exist_ok=True)
    else:
        (out / "status.json").write_text("{}", encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    sys.exit(main())
