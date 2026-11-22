#!/usr/bin/env python3
"""
Load the test phone with realistic content before the crawl, so any tool that needs
something to work on (photo, video, audio, document, contact) gets real input instead
of an empty picker. Runs on the CI runner; needs adb, ffmpeg, Pillow.

  photos      6 camera JPEGs WITH GPS + camera EXIF (so location-stripping can be checked)
  screenshots 4 PNGs in Pictures/Screenshots
  video       2 short MP4s (with an audio track)
  audio       an M4A voice-memo-like file and an MP3
  documents   PDF, TXT, CSV, a vCard and a ZIP in Download
  contacts    6 contacts: normal, email-only, no-phone, duplicate pair, work email
"""
from __future__ import annotations
import io, os, shlex, subprocess, sys, tempfile, zipfile
from pathlib import Path


def adb(*a, check=False):
    return subprocess.run(["adb", *a], capture_output=True, text=True, check=check).stdout


def sh(cmd):
    return adb("shell", cmd)


def jpeg_with_gps(path: Path, i: int):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (1600, 1200), (40 + i * 30, 90, 160 - i * 15))
    d = ImageDraw.Draw(im)
    d.ellipse([500, 300, 1100, 900], fill=(255, 201, 60))
    d.text((60, 60), f"Test photo {i + 1}", fill="white")
    exif = Image.Exif()
    exif[0x010F] = "Google"            # Make
    exif[0x0110] = "Pixel 6"           # Model
    exif[0x0132] = f"2026:11:{10 + i:02d} 10:2{i}:00"
    gps = {1: "N", 2: (41.0, 52.0, 42.0 + i), 3: "W", 4: (87.0, 37.0, 25.0 + i)}  # near Chicago
    exif[0x8825] = gps
    im.save(path, "JPEG", quality=88, exif=exif)


def tiny_pdf() -> bytes:
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    text = b"BT /F1 24 Tf 72 700 Td (Test invoice #1042 - Total $128.40) Tj ET"
    objs[3] = f"<< /Length {len(text)} >>\nstream\n".encode() + text + b"\nendstream"
    out, offs = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for n, o in enumerate(objs, 1):
        offs.append(out.tell())
        body = o if isinstance(o, bytes) else o.encode()
        out.write(f"{n} 0 obj\n".encode() + body + b"\nendobj\n")
    x = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for o in offs:
        out.write(f"{o:010d} 00000 n \n".encode())
    out.write(f"trailer << /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF\n".encode())
    return out.getvalue()


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args], check=False)


CONTACTS = [
    ("Alex Rivera", "+1 312 555 0101", "alex.rivera@example.com"),
    ("Priya Patel", "+1 630 555 0142", ""),
    ("Sam Work", "", "sam@acme-corp.example"),          # email only, work address
    ("Jordan Lee", "", ""),                              # no phone, no email
    ("Maria Gomez", "+1 773 555 0199", "maria@example.com"),
    ("Maria Gomez", "+1 773 555 0199", ""),              # duplicate
]


def add_contact(name, phone, email):
    sh("content insert --uri content://com.android.contacts/raw_contacts --bind account_type:n: --bind account_name:n:")
    ids = [int(x) for x in __import__("re").findall(r"_id=(\d+)", sh("content query --uri content://com.android.contacts/raw_contacts --projection _id"))]
    if not ids:
        return False
    rid = max(ids)
    def data(mime, *binds):
        sh("content insert --uri content://com.android.contacts/data --bind raw_contact_id:i:%d --bind mimetype:s:%s %s"
           % (rid, mime, " ".join(binds)))
    data("vnd.android.cursor.item/name", "--bind data1:s:" + shlex.quote(name))
    if phone:
        data("vnd.android.cursor.item/phone_v2", "--bind data1:s:" + shlex.quote(phone), "--bind data2:i:2")
    if email:
        data("vnd.android.cursor.item/email_v2", "--bind data1:s:" + shlex.quote(email), "--bind data2:i:1")
    return True


def main():
    tmp = Path(tempfile.mkdtemp())
    for d in ("/sdcard/DCIM/Camera", "/sdcard/Pictures/Screenshots", "/sdcard/Download", "/sdcard/Music",
              "/sdcard/Movies", "/sdcard/Recordings"):
        sh(f"mkdir -p {d}")
    n = 0
    try:
        for i in range(6):
            p = tmp / f"IMG_202611{10 + i:02d}_10{i}000.jpg"; jpeg_with_gps(p, i)
            adb("push", str(p), "/sdcard/DCIM/Camera/"); n += 1
        from PIL import Image, ImageDraw
        for i in range(4):
            p = tmp / f"Screenshot_202611{18 + i}-0930{i}0.png"
            im = Image.new("RGB", (1080, 2400), (245, 245, 250)); ImageDraw.Draw(im).text((60, 120), f"Screenshot {i + 1}", fill="black")
            im.save(p); adb("push", str(p), "/sdcard/Pictures/Screenshots/"); n += 1
    except ImportError:
        print("Pillow missing — skipping images", file=sys.stderr)
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=8", "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(tmp / "VID_20261120_101500.mp4"))
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=720x1280:rate=30:duration=5", "-f", "lavfi", "-i", "sine=frequency=660:duration=5",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(tmp / "VID_20261121_183000.mp4"))
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=300:duration=12", "-c:a", "aac", str(tmp / "Voice memo 1.m4a"))
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=520:duration=15", "-c:a", "libmp3lame", str(tmp / "Song sample.mp3"))
    for f, dest in (("VID_20261120_101500.mp4", "/sdcard/DCIM/Camera/"), ("VID_20261121_183000.mp4", "/sdcard/Movies/"),
                    ("Voice memo 1.m4a", "/sdcard/Recordings/"), ("Song sample.mp3", "/sdcard/Music/")):
        if (tmp / f).exists():
            adb("push", str(tmp / f), dest); n += 1
    docs = {
        "Invoice_1042.pdf": tiny_pdf(),
        "Notes.txt": b"Grocery list:\n- milk\n- eggs\n- spinach\nCall Alex at 312-555-0101 on Friday 3pm.\n",
        "Expenses.csv": b"date,item,amount\n2026-11-01,Coffee,4.50\n2026-11-02,Groceries,62.10\n2026-11-03,Gas,48.00\n",
        "Contacts_backup.vcf": b"BEGIN:VCARD\nVERSION:3.0\nFN:Chris Test\nTEL:+13125550111\nEND:VCARD\n",
    }
    for name, data in docs.items():
        (tmp / name).write_bytes(data); adb("push", str(tmp / name), "/sdcard/Download/"); n += 1
    with zipfile.ZipFile(tmp / "Photos.zip", "w") as z:
        z.writestr("readme.txt", "zip test")
    adb("push", str(tmp / "Photos.zip"), "/sdcard/Download/"); n += 1
    # make the media visible to pickers and galleries
    sh("content call --uri content://media --method scan_volume --arg external_primary")
    sh("am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file:///sdcard/DCIM/Camera")
    c = sum(1 for ct in CONTACTS if add_contact(*ct))
    print(f"seeded {n} files and {c} contacts")


if __name__ == "__main__":
    main()
