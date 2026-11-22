#!/usr/bin/env python3
"""Read an APK's identity for the report: label, package, version, SDK levels, icon.
usage: appinfo.py app.apk <aapt badging output file> <out_dir>   -> out_dir/app.json (+ app_icon.png)"""
import json, re, sys, zipfile
from pathlib import Path

apk, badging, out = sys.argv[1], Path(sys.argv[2]).read_text(errors="ignore"), Path(sys.argv[3])
g = lambda rx: (re.search(rx, badging) or [None, ""])[1]
info = {
    "package": g(r"package: name='([^']+)'"),
    "version_name": g(r"versionName='([^']*)'"),
    "version_code": g(r"versionCode='([^']*)'"),
    "min_sdk": g(r"sdkVersion:'(\d+)'"),
    "target_sdk": g(r"targetSdkVersion:'(\d+)'"),
    "label": g(r"application-label:'([^']*)'") or g(r"application: label='([^']*)'"),
    "permissions": sorted(set(re.findall(r"uses-permission: name='([^']+)'", badging))),
    "native_code": g(r"native-code: (.+)").replace("'", "").strip(),
    "icon": "",
}
# best icon: a PNG/WebP from the declared icons (largest density), else any ic_launcher bitmap in the APK
cands = re.findall(r"application-icon-(\d+):'([^']+)'", badging)
with zipfile.ZipFile(apk) as z:
    names = set(z.namelist())
    pick = [p for d, p in sorted(cands, key=lambda t: -int(t[0])) if p in names and p.lower().endswith((".png", ".webp"))]
    if not pick:
        bm = [n for n in names if re.search(r"(ic_launcher|app_icon|icon)[^/]*\.(png|webp)$", n, re.I) and "round" not in n]
        pick = sorted(bm, key=lambda n: -z.getinfo(n).file_size)
    if pick:
        ext = Path(pick[0]).suffix.lower()
        (out / f"app_icon{ext}").write_bytes(z.read(pick[0]))
        info["icon"] = f"app_icon{ext}"
(out / "app.json").write_text(json.dumps(info, indent=1))
print("app info:", info["label"] or "?", info["version_name"], "icon" if info["icon"] else "no icon")
