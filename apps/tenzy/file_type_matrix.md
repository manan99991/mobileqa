# File Viewer — file-type matrix (the standard expected)

The File Viewer's promise is: open ANY file and **read its content on-device**, not just
list or describe it. This is the per-type contract the QA agent checks and the
`verify_types.py` logic gate + the CI emulator file-type smoke enforce. It is generated from
the harness corpus — regenerate with:  `python3 verify_types.py` then update this table.

| Kind | Expected on-screen behavior | Header type label | Sample exts tested |
|------|------------------------------|-------------------|--------------------|
| **PDF** | Renders the pages (PdfRenderer) — scrollable, zoomable. | PDF document | pdf |
| **IMAGE** | Shows the image; pinch-zoom / pan. | Bitmap image, DNG camera RAW, GIF image, HEIC image, JPEG image, PNG image, WebP image | bmp, dng, gif, heic, jpg, png, webp |
| **EBOOK** | Opens the book text in the in-app reader (spine order). Open-with for a full e-reader. | EPUB book | epub |
| **DOC** | Extracts and shows the document's text on-device (docx body, pptx per-slide, xlsx strings, odf content). Open-with for full formatting. | Excel spreadsheet, OpenDocument file, PowerPoint deck, Word document | docx, odp, ods, odt, pptx, xlsx |
| **TEXT** | Shows the text/code, formatted, scrollable. | Kotlin source, TXT text, Text | (none), kt, txt |
| **JSON** | Shows pretty-printed JSON. | JSON data | json |
| **XML** | Shows the XML/markup. | XML document | xml |
| **CSV** | Shows a table; scroll sideways for more columns. | CSV table, TSV table | csv, tsv |
| **SQLITE** | Lists the tables and shows the first rows of a chosen table. | SQLite database | db |
| **APK** | Shows the app label, package, version, min/target SDK, permissions, signature. | Android app (APK) | apk |
| **AUDIO** | Audio player (metadata + play). | MP3 audio | mp3 |
| **VIDEO** | Video player (metadata + play). | MP4 video | mp4 |
| **ARCHIVE** | Lists the entries inside the container (this is the ONLY kind that should show a listing). | Java archive (JAR), ZIP archive | jar, zip |
| **BINARY** | Shows a real type label + a hex/byte view or an 'open with an extractor' note — never a dead-end. | 7-Zip archive, ELF executable, Gzip archive, RAR archive, Unknown binary, Windows executable (PE) | (none), 7z, elf, exe, gz, rar |

## Verification layers

1. **Off-device logic gate** (`verify_types.py`, runs before every build): generates a real
   sample of each type, mirrors `FileViewer.probe()` + the text extractors, and asserts each
   maps to the expected Kind and readable types extract non-empty text. Hard-fails the build.
2. **Emulator file-type smoke** (in `mobileqa.yml`, after the route smoke): pushes the same
   corpus to the device, deep-links each file into the viewer via an ACTION_VIEW intent, and
   asserts no crash, the correct header type label on screen, and — for EPUB/Office/ODF — the
   reader note (proving it READS, not lists). Screenshots in `reports/filetype/`.
3. **Deep QA agent**: the `goals:` in `app.android.yaml` steer it to exercise every tool with
   its accepted file types and flag any readable document shown as an archive listing
   (`playbook: container-as-listing`).

