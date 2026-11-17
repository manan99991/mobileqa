# Get It Listed — automated QA engine

Turns `mobileqa` into the product behind getitlisted.app: point it at a customer's
APK (no source needed) and it installs the app on a free Linux emulator, explores
it end-to-end, and publishes a polished QA report to the customer's dashboard.

## Pieces
| file | what it does |
|---|---|
| `app.blackbox.yaml` | black-box mobileqa config — generic goals, package injected at runtime |
| `.github/workflows/getitlisted.yml` | the pipeline: APK → emulator → explore → report → publish (free GitHub Actions) |
| `report.py` | renders the customer-facing HTML report from a mobileqa run |
| `push.py` | uploads screenshots + report to Supabase Storage, inserts the `reports` row |
| `schema.sql` | the `reports` table + public storage bucket (run once in Supabase) |

## One-time setup
1. Run `schema.sql` in the Supabase SQL editor (project `ljdedtgsijrettyrggpy`).
2. Add three repo secrets (Settings → Secrets → Actions):
   - `GEMINI_API_KEY` — free Google key (the vision brain)
   - `SB_URL` — `https://ljdedtgsijrettyrggpy.supabase.co`
   - `SB_SERVICE_KEY` — Supabase service-role key
3. In the customer status page, show the latest `reports` row for the app
   (`report_url` links to the full report).

## Run a test
- **By hand:** Actions tab → "Get It Listed — test an app" → give an APK URL + the client's `app_key`.
- **Automatically:** `POST /repos/manan99991/mobileqa/dispatches` with
  `{"event_type":"gil-test","client_payload":{"apk_url":"…","app_key":"…","app_name":"…"}}`.

## Honest limits
- **Android only.** Automated iOS needs a Mac + Apple signing (not free) — iOS stays manual for now.
- **The emulator is x86_64.** A customer's arm-only release APK may not run; a universal / debug APK
  (what most Flutter/RN/Expo builds produce) does. Ask for a build that includes x86, or test what runs.
- **Black-box = exploratory.** It finds crashes, dead controls, blank outputs, UX/UI, a11y, broken links,
  slow screens and policy flags — not your app's intended business logic. A strong first line, not a full QA.
- This does **not** satisfy Google's 12-real-testers rule; it's the value-add report on top of it.
