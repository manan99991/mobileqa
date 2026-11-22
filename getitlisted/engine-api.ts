// Get It Listed — engine API (Supabase Edge Function "engine-api", Verify JWT: OFF).
//
// The test robot runs on GitHub Actions. Instead of storing a database key on GitHub,
// every request carries a GitHub OIDC token: a short-lived ID signed by GitHub that says
// which repository and branch the run came from. We verify it against GitHub's public
// keys and only accept runs from manan99991/mobileqa on our own branches. This function
// then uses Supabase's built-in service key (never leaves Supabase).
//
// POST { op: "pick" }                      -> { ids: number[] }
// POST { op: "fetch", id }                 -> build details + signed download links
// POST { op: "finish", id, ok }            -> {}
// POST { op: "sign_upload", path }         -> { url, public_url }   (reports bucket)
// POST { op: "report", row }               -> {}
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";
import { createRemoteJWKSet, jwtVerify } from "https://esm.sh/jose@5";

const REPO = "manan99991/mobileqa";
const REFS = ["refs/heads/main", "refs/heads/getitlisted-engine"];
const AUD = "getitlisted-engine";
const JWKS = createRemoteJWKSet(new URL("https://token.actions.githubusercontent.com/.well-known/jwks"));
const sb = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!);
const URL_BASE = Deno.env.get("SUPABASE_URL")!;
const DAY_GAP_H = 20, MAX_PICK = 4;

const json = (b: unknown, s = 200) => new Response(JSON.stringify(b), { status: s, headers: { "content-type": "application/json" } });

async function verify(req: Request) {
  const tok = (req.headers.get("authorization") || "").replace(/^Bearer\s+/i, "");
  if (!tok) throw new Error("no token");
  const { payload } = await jwtVerify(tok, JWKS, { issuer: "https://token.actions.githubusercontent.com", audience: AUD });
  if (payload.repository !== REPO) throw new Error("wrong repo");
  if (!REFS.includes(String(payload.ref))) throw new Error("wrong ref");
  if (String(payload.event_name) === "pull_request" || String(payload.event_name) === "pull_request_target") throw new Error("PRs not allowed");
}

type B = { id: number; app_key: string; status: string; last_run_at: string | null; created_at: string };
type C = { app_key: string; status: string; tier: string; start_date: string };

export function due(builds: B[], clients: C[], t: Date): number[] {
  const cl = new Map(clients.map(c => [c.app_key, c]));
  const latest = new Map<string, B>();
  for (const b of [...builds].sort((a, z) => z.created_at.localeCompare(a.created_at))) if (!latest.has(b.app_key)) latest.set(b.app_key, b);
  const out: [number, number][] = [];
  for (const [key, b] of latest) {
    const c = cl.get(key);
    if (!c || c.status !== "active" || c.tier === "pass") continue;
    const last = b.last_run_at ? new Date(b.last_run_at) : null;
    if (b.status === "running" && last && t.getTime() - last.getTime() < 2 * 3600e3) continue;
    if (b.status === "pending") { out.push([0, b.id]); continue; }
    const end = new Date(c.start_date + "T00:00:00Z"); end.setUTCDate(end.getUTCDate() + 14);
    if (t >= end) continue;
    if (!last || t.getTime() - last.getTime() >= DAY_GAP_H * 3600e3) out.push([1, b.id]);
  }
  return out.sort((a, z) => a[0] - z[0]).slice(0, MAX_PICK).map(x => x[1]);
}

function forbidden(notes: string) {
  const re = /\b(?:skip|avoid|don'?t (?:tap|touch|open))\s+(?:the\s+)?([A-Za-z][A-Za-z ]{1,30}?)(?:\s+(?:screen|button|page|tab))?(?=[.,;!\n]|$)/gi;
  return [...(notes || "").matchAll(re)].map(m => m[1].trim().toLowerCase()).filter(Boolean).join(",");
}

Deno.serve(async (req) => {
  if (req.method !== "POST") return json({ error: "POST only" }, 405);
  try { await verify(req); } catch (e) { return json({ error: "unauthorized: " + (e as Error).message }, 401); }
  const body = await req.json().catch(() => ({}));
  try {
    switch (body.op) {
      case "pick": {
        const { data: builds, error: e1 } = await sb.from("builds").select("id,app_key,status,last_run_at,created_at").order("created_at", { ascending: false }).limit(500);
        const { data: clients, error: e2 } = await sb.from("clients").select("app_key,status,tier,start_date");
        if (e1 || e2) throw e1 || e2;
        const ids = due(builds as B[], clients as C[], new Date());
        if (ids.length) await sb.from("builds").update({ status: "running", last_run_at: new Date().toISOString() }).in("id", ids);
        return json({ ids });
      }
      case "fetch": {
        const { data: b, error } = await sb.from("builds").select("*").eq("id", body.id).single();
        if (error) throw error;
        const { data: c } = await sb.from("clients").select("app_name").eq("app_key", b.app_key).maybeSingle();
        const sign = async (p: string | null) => p ? (await sb.storage.from("builds").createSignedUrl(p, 4 * 3600)).data?.signedUrl || "" : "";
        return json({
          apk_url: await sign(b.apk_path), mapping_url: await sign(b.mapping_path),
          app_key: b.app_key, app_name: c?.app_name || b.app_key,
          login_email: b.login_email || "", login_password: b.login_password || "", forbidden: forbidden(b.notes || ""),
        });
      }
      case "finish": {
        const { data: b } = await sb.from("builds").select("runs").eq("id", body.id).single();
        await sb.from("builds").update({ status: body.ok ? "done" : "failed", last_run_at: new Date().toISOString(), runs: (b?.runs || 0) + 1 }).eq("id", body.id);
        return json({});
      }
      case "sign_upload": {
        const path = String(body.path || "");
        if (!/^[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+$/.test(path)) return json({ error: "bad path" }, 400);
        const { data, error } = await sb.storage.from("reports").createSignedUploadUrl(path, { upsert: true });
        if (error) throw error;
        return json({ url: data.signedUrl, public_url: `${URL_BASE}/storage/v1/object/public/reports/${path}` });
      }
      case "report": {
        const r = body.row || {};
        const row = { app_key: r.app_key, platform: r.platform, run_id: r.run_id, score: r.score, critical: r.critical, high: r.high,
          medium: r.medium, low: r.low, issues: r.issues, report_url: r.report_url, status: "ready" };
        const { error } = await sb.from("reports").insert(row);
        if (error) throw error;
        return json({});
      }
      default: return json({ error: "unknown op" }, 400);
    }
  } catch (e) {
    return json({ error: (e as Error).message || String(e) }, 500);
  }
});
