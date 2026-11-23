"""When the app froze (ANR), pull the app's main-thread stack from Android's ANR dump into anr.json,
so the report can show the exact code that was blocking instead of just "the app froze"."""
import json
import re
import sys
from pathlib import Path


def main_stack(text: str, pkg: str) -> list:
    # one ANR file holds every thread of the process: take the "main" thread block
    if f"Cmd line: {pkg}" not in text:
        return []
    m = re.search(r'\n"main".*?\n(.*?)\n\n', text, re.S)
    if not m:
        return []
    return [l.strip() for l in m.group(1).splitlines()
            if l.strip().startswith(("at ", "- waiting", "- locked", "- sleeping"))][:40]


def run(run_dir: str, pkg: str):
    out = []
    for f in sorted(Path(run_dir, "anr").rglob("*")):
        if f.is_file():
            st = main_stack(f.read_text("utf-8", "ignore"), pkg)
            if st:
                skip = ("android.", "java.", "kotlin.", "kotlinx.", "androidx.", "com.android.", "dalvik.")
                own = [x for x in st if not x[3:].startswith(skip)] or st[:3]
                out.append({"file": f.name, "main_thread": st, "app_frames": own[:10]})
    Path(run_dir, "anr.json").write_text(json.dumps(out, indent=1))
    print(f"anr stacks: {len(out)}")


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2])
