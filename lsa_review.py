#!/usr/bin/env python3
"""LSA call review - daily incremental runner.

Pulls Google Local Services Ads leads + conversations (Google Ads API v23),
downloads call recordings, transcribes them locally (faster-whisper), looks the
caller up in Leap / JobProgress (read-only GET), classifies each lead, and
writes sheet rows for leads not yet processed.

Usage:
  python3 lsa_review.py                # new leads since last run (default lookback 3 days)
  python3 lsa_review.py --since 2026-10-13
  python3 lsa_review.py --reprocess    # ignore state, redo all leads in window

Configuration (environment variables override config.json):
  LSA_CONFIG            path to a JSON config file (otherwise ./config.json or
                        config.json next to this script)
  LSA_CUSTOMER_ID       LSA Ads customer ID, digits only (required)
  LSA_DATA_DIR          working directory (default ./data)
  LSA_TOKEN_FILE        OAuth token JSON with refresh_token
                        (default ~/.config/lsa/token.json)
  LSA_LOGIN_CUSTOMER_ID optional manager ID; if set, sent as login-customer-id
  LEAP_API_BASE         CRM API base (default https://api.jobprogress.com/api/v3)
  LSA_LEADS_CSV         optional LSA dashboard CSV (city + charge status)
  LSA_GOOGLE_CLIENT_ID / LSA_GOOGLE_CLIENT_SECRET / LEAP_API_KEY  secrets, env only

Outputs (all under the data directory):
  recordings/<conversation_id>.mp3, transcripts/<conversation_id>.txt
  pending_rows.json   rows (same column order as the "Calls" tab) to append to
                      the review sheet. The agent appends them, then empties
                      the file.
  state.json          processed lead IDs + last run time
Classification overrides (written by the reviewing agent / a human) live in
classifications.json: {lead_id: {classification, summary, city, service, dispute}}.
Without an override a keyword/metadata heuristic is used, marked "(auto)".
Nothing secret is printed.
"""
import argparse, csv, datetime as dt, json, os, re, subprocess, sys, time
import requests

# Filled in by load_config() before any API work.
BASE = "./data"
CID = ""
ADS = ""
LEAP = "https://api.jobprogress.com/api/v3"
TOKEN_FILE = os.path.expanduser("~/.config/lsa/token.json")
LEADS_CSV = ""
LOGIN_CUSTOMER_ID = ""
COLUMNS = ["Date/time (Pacific)", "Lead ID", "Lead type", "Service category", "Charged?",
           "Lead status", "Call length", "Caller name", "Caller phone", "Classification",
           "Summary", "City", "Leap match", "Leap stage", "Should dispute/review?", "Transcript"]
CLASSES = ["Real lead", "Existing customer", "Spam / sales pitch / vendor",
           "Wrong number / wrong service", "Missed / no conversation"]
# Example Leap (JobProgress) won-stage names; override in config.json.
WON_STAGES = {"Signed / No Production", "Ready to Produce", "Job Completed", "Q.C. and billed", "Paid", "Work"}
# Example LSA service_id map for a roofing / siding advertiser; override in config.json.
SERVICE_NAMES = {"roof_installation": "Roof installation", "roof_repair": "Roof repair",
                 "roof_inspection": "Roof inspection", "repair_maintenance_siding_pro": "Siding repair & maintenance"}


def load_config():
    """Read config.json and environment variables. Env wins. No secrets required here."""
    global BASE, CID, ADS, LEAP, TOKEN_FILE, LEADS_CSV, LOGIN_CUSTOMER_ID, WON_STAGES, SERVICE_NAMES
    cfg = {}
    candidates = []
    if os.environ.get("LSA_CONFIG"):
        candidates.append(os.path.expanduser(os.environ["LSA_CONFIG"]))
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(os.getcwd(), "config.json"))
    candidates.append(os.path.join(here, "config.json"))
    seen = set()
    for p in candidates:
        p = os.path.abspath(p)
        if p in seen or not os.path.isfile(p):
            continue
        seen.add(p)
        with open(p) as f:
            cfg = json.load(f) or {}
        break

    def pick(env, key, default=""):
        if os.environ.get(env):
            return os.environ[env]
        val = cfg.get(key, default)
        return default if val is None or val == "" else val

    BASE = os.path.abspath(os.path.expanduser(pick("LSA_DATA_DIR", "data_dir", "./data")))
    CID = str(pick("LSA_CUSTOMER_ID", "customer_id", "")).replace("-", "")
    ADS = f"https://googleads.googleapis.com/v23/customers/{CID}/googleAds:search" if CID else ""
    LEAP = pick("LEAP_API_BASE", "leap_api_base", "https://api.jobprogress.com/api/v3").rstrip("/")
    TOKEN_FILE = os.path.abspath(os.path.expanduser(pick("LSA_TOKEN_FILE", "token_file", "~/.config/lsa/token.json")))
    LEADS_CSV = pick("LSA_LEADS_CSV", "leads_csv", "")
    if LEADS_CSV:
        LEADS_CSV = os.path.abspath(os.path.expanduser(LEADS_CSV))
    LOGIN_CUSTOMER_ID = str(pick("LSA_LOGIN_CUSTOMER_ID", "login_customer_id", "")).replace("-", "")
    if cfg.get("won_stages"):
        WON_STAGES = set(cfg["won_stages"])
    if cfg.get("service_names"):
        SERVICE_NAMES = dict(cfg["service_names"])


def _ads_headers(tok):
    h = {"Authorization": "Bearer " + tok}
    if LOGIN_CUSTOMER_ID:
        h["login-customer-id"] = LOGIN_CUSTOMER_ID
    return h


# ---------------- Google Ads ----------------
def ads_token():
    rt = json.load(open(TOKEN_FILE))["refresh_token"]
    r = requests.post("https://oauth2.googleapis.com/token", data=dict(
        grant_type="refresh_token", refresh_token=rt,
        client_id=os.environ["LSA_GOOGLE_CLIENT_ID"], client_secret=os.environ["LSA_GOOGLE_CLIENT_SECRET"]))
    if r.status_code != 200:
        sys.exit(f"Token refresh failed ({r.status_code}). If 'invalid_grant', redo the one-time sign-in.")
    return r.json()["access_token"]

def ads_search(tok, q):
    out, pt = [], None
    while True:
        body = {"query": q, **({"pageToken": pt} if pt else {})}
        r = requests.post(ADS, headers=_ads_headers(tok), json=body, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(r.text[:1500])
        j = r.json(); out += j.get("results", []); pt = j.get("nextPageToken")
        if not pt:
            return out

def pull(tok, since):
    leads = [x["localServicesLead"] for x in ads_search(tok, f"""
      SELECT local_services_lead.id, local_services_lead.lead_type, local_services_lead.lead_status,
             local_services_lead.creation_date_time, local_services_lead.category_id,
             local_services_lead.service_id, local_services_lead.lead_charged,
             local_services_lead.contact_details, local_services_lead.note.description,
             local_services_lead.credit_details.credit_state
      FROM local_services_lead
      WHERE local_services_lead.creation_date_time >= '{since} 00:00:00'""")]
    convs = [x["localServicesLeadConversation"] for x in ads_search(tok, f"""
      SELECT local_services_lead_conversation.id, local_services_lead_conversation.conversation_channel,
             local_services_lead_conversation.participant_type, local_services_lead_conversation.event_date_time,
             local_services_lead_conversation.lead,
             local_services_lead_conversation.phone_call_details.call_duration_millis,
             local_services_lead_conversation.phone_call_details.call_recording_url,
             local_services_lead_conversation.message_details.text
      FROM local_services_lead_conversation
      WHERE local_services_lead_conversation.event_date_time >= '{since} 00:00:00'""")]
    by = {}
    for c in convs:
        by.setdefault(c["lead"].split("/")[-1], []).append(c)
    for l in leads:
        l["conversations"] = sorted(by.get(l["id"], []), key=lambda c: c.get("eventDateTime", ""))
    return leads

def download(tok, conv):
    url = (conv.get("phoneCallDetails") or {}).get("callRecordingUrl")
    if not url:
        return None
    p = f"{BASE}/recordings/{conv['id']}.mp3"
    if not os.path.exists(p):
        r = requests.get(url, headers=_ads_headers(tok), timeout=120)
        if r.status_code != 200 or len(r.content) < 500:
            return None
        with open(p, "wb") as f:
            f.write(r.content)
        os.chmod(p, 0o600)
    return p

# ---------------- Transcription ----------------
_model = None
def transcribe(path):
    out = f"{BASE}/transcripts/{os.path.basename(path)[:-4]}.txt"
    if os.path.exists(out):
        return open(out).read()
    global _model
    import numpy as np
    from faster_whisper import WhisperModel
    if _model is None:
        _model = WhisperModel(os.environ.get("WMODEL", "small.en"), device="cpu", compute_type="int8", cpu_threads=8)
    pcm = subprocess.run(["ffmpeg", "-nostdin", "-i", path, "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
                         capture_output=True).stdout
    audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
    segs, _ = _model.transcribe(audio, beam_size=1, vad_filter=True, language="en")
    txt = " ".join(s.text.strip() for s in segs)
    secs = len(audio) / 16000
    # VAD sometimes drops most of a call (or whisper loops on "Hello"); retry without VAD
    if secs > 60 and (len(txt.split()) < secs / 6 or len(set(txt.lower().split())) < 25):
        segs, _ = _model.transcribe(audio, beam_size=5, vad_filter=False, condition_on_previous_text=False, language="en")
        alt = " ".join(s.text.strip() for s in segs)
        if len(set(alt.lower().split())) > len(set(txt.lower().split())):
            txt = alt
    with open(out, "w") as f:
        f.write(txt)
    return txt

# ---------------- Leap (GET only) ----------------
def leap_get(path, params=None):
    H = {"Authorization": "Bearer " + os.environ["LEAP_API_KEY"], "Accept": "application/json"}
    for _ in range(4):
        r = requests.get(LEAP + path, headers=H, params=params, timeout=60)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", 30))); continue
        r.raise_for_status(); time.sleep(1.05); return r.json()
    r.raise_for_status()

def leap_lookup(phone10, cache, email=""):
    key = phone10 or (email or "").strip().lower()
    if not key or not os.environ.get("LEAP_API_KEY"):
        return None
    if key not in cache:
        q = {"phone": phone10} if phone10 else {"email": key}
        d = leap_get("/customers", {**q, "includes[]": ["jobs", "phones"]})
        cache[key] = [{
            "customer_id": c["id"],
            "name": ((c.get("first_name") or "") + " " + (c.get("last_name") or "")).strip(),
            "created_at": c.get("created_at"),
            "jobs": [{"id": j["id"], "number": j.get("number"), "name": j.get("name"),
                      "created_at": j.get("created_at"), "stage": (j.get("current_stage") or {}).get("name"),
                      "contract_signed_date": j.get("contract_signed_date")}
                     for j in (c.get("jobs") or {}).get("data", [])]} for c in d.get("data", [])]
    return cache[key]

def leap_summary(matches, lead_dt):
    """Returns (match text, stage text, existed_before_call, converted)."""
    if matches is None:
        return "", "", False, False
    if not matches:
        return "No match", "", False, False
    cust_created = min((m["created_at"] or "9999") for m in matches)
    existed = cust_created < (lead_dt - dt.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    jobs = sorted([j for m in matches for j in m["jobs"]], key=lambda j: j["created_at"] or "", reverse=True)
    name = matches[0]["name"] or f"Leap customer #{matches[0]['customer_id']}"
    if not jobs:
        return f"Customer: {name} (no job)", "Customer only", existed, False
    j = jobs[0]
    label = f"{name} - job {j['number']}" + (f" ({j['name']})" if j.get("name") else "")
    if len(jobs) > 1:
        label += f" +{len(jobs)-1} more job(s)"
    label += " [customer before call]" if existed else " [new since call]"
    j_new = [x for x in jobs if (x["created_at"] or "") >= (lead_dt - dt.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")]
    if existed and j_new:
        label += f"; new job {j_new[0]['number']} after call"
    since = (lead_dt - dt.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    converted = any(x["stage"] in WON_STAGES and (x["created_at"] or "") >= since for x in jobs)
    return label, j["stage"] or "", existed, converted

# ---------------- Classification heuristic ----------------
SPAM = r"google (listing|business|maps|profile)|verify your|merchant|funding|line of credit|seo\b|marketing|press (one|1)|" \
       r"recorded (message|line)|extended warranty|capital|we help (roofers|contractors)|lead gen|advertis|calling from .* (agency|solutions)"
EXISTING = r"\b(my|our) (job|roof|project|crew)\b.*(schedul|start|done|finish)|invoice|bill\b|payment|warranty|already (have|had|signed)|" \
           r"you guys (did|put|replaced|installed)|on the schedule|crew"
WRONGSVC = r"\b(solar panel|fence|plumb|hvac|furnace|concrete|tree (removal|trim)|paint(ing)? (my|the) house|gutter cleaning|chimney sweep)\b"

def heuristic(lead, transcript, total_secs, existed):
    t = (transcript or "").lower()
    words = len(t.split())
    if lead.get("leadType") == "MESSAGE":
        return "Real lead"
    if total_secs < 15 or words < 20:
        return "Missed / no conversation"
    if re.search(SPAM, t):
        return "Spam / sales pitch / vendor"
    if existed or re.search(EXISTING, t):
        return "Existing customer"
    if re.search(WRONGSVC, t):
        return "Wrong number / wrong service"
    return "Real lead"

# ---------------- Helpers ----------------
CARD = re.compile(r"(?<!\d)(?:\d[\s-]?){13,19}(?!\d)")
CVV = re.compile(r"(three|3|numbers?|code) on the back( again)?\??\s*[\d\s,.-]{3,12}", re.I)
EXP = re.compile(r"(expiration( date)?\??\s*)([A-Za-z]+ \d{1,2},? ?\d{2,4}|\d{1,2}[-/ ]\d{2,4})", re.I)
def redact(t):
    t = CARD.sub("[card # removed]", t)
    t = CVV.sub(lambda m: m.group(0).split("back")[0] + "back [removed] ", t)
    return EXP.sub(lambda m: m.group(1) + "[removed]", t)

def dispute_flag(charged, cls):
    if charged != "Yes":
        return {"Credited": "Already credited", "In review": "Already in review with Google"}.get(charged, "")
    if cls.startswith("Real lead"):
        return ""
    if cls.startswith("Existing customer"):
        return "Review - existing customer (not new business)"
    return "Yes - charged, not a real lead"

def fmt_secs(s):
    s = int(round(s)); return f"{s//60}:{s%60:02d}"

def phone10(lead):
    p = (lead.get("contactDetails") or {}).get("phoneNumber", "")
    d = re.sub(r"\D", "", p)[-10:]
    return d if len(d) == 10 else ""

def pretty_phone(d):
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if d else ""

def load_csv_export():
    """LSA dashboard export (optional) for city + charge status ("In review", "Credited")."""
    p = LEADS_CSV
    out = {}
    if p and os.path.exists(p):
        for r in csv.DictReader(open(p)):
            d = re.sub(r"\D", "", r.get("Customer", ""))[-10:]
            if d:
                out.setdefault(d, r)
    return out

def build_row(lead, tok, cache, overrides, export):
    created = dt.datetime.strptime(lead["creationDateTime"][:19], "%Y-%m-%d %H:%M:%S")  # already in the Ads account time zone
    convs = lead["conversations"]
    parts, total = [], 0.0
    for c in convs:
        if c.get("conversationChannel") == "PHONE_CALL":
            secs = int((c.get("phoneCallDetails") or {}).get("callDurationMillis", 0)) / 1000
            total += secs
            p = download(tok, c)
            if not p and (c.get("phoneCallDetails") or {}).get("callRecordingUrl") \
                    and dt.datetime.now() - created < dt.timedelta(hours=48):
                return None  # recording not ready yet (Google returns 500 for a while); retry next run
            txt = transcribe(p) if p else "(recording unavailable)"
            parts.append(f"[Call {c['eventDateTime'][5:16]} {fmt_secs(secs)}] {txt}".strip())
        elif c.get("conversationChannel") == "MESSAGE":
            parts.append(f"[Message] {(c.get('messageDetails') or {}).get('text', '')}")
    transcript = " | ".join(parts)
    ph = phone10(lead)
    matches = leap_lookup(ph, cache, (lead.get("contactDetails") or {}).get("email", ""))
    lmatch, lstage, existed, converted = leap_summary(matches, created)
    ov = overrides.get(lead["id"], {})
    cls = ov.get("classification") or heuristic(lead, transcript, total, existed)
    auto = "" if ov.get("classification") else " (auto)"
    ex = export.get(ph, {})
    credit = (lead.get("creditDetails") or {}).get("creditState")
    charged = "Yes" if lead.get("leadCharged") else "No"
    if credit == "CREDITED" or ex.get("Charge status") == "Credited":
        charged = "Credited"
    elif ex.get("Charge status") == "In review":
        charged = "In review"
    dispute = ov.get("dispute")
    if dispute is None:
        dispute = dispute_flag(charged, cls)
    ncalls = sum(1 for c in convs if c.get("conversationChannel") == "PHONE_CALL")
    length = fmt_secs(total) + (f" ({ncalls} calls)" if ncalls > 1 else "") if ncalls else ""
    return {
        "_converted": converted and cls == "Real lead",
        "row": [created.strftime("%Y-%m-%d %H:%M"), lead["id"],
                {"PHONE_CALL": "Phone call", "MESSAGE": "Message"}.get(lead.get("leadType"), lead.get("leadType", "")),
                ov.get("service") or SERVICE_NAMES.get(lead.get("serviceId"), "Roofer (no job type)"),
                charged, (lead.get("leadStatus") or "").title(), length,
                (lead.get("contactDetails") or {}).get("consumerName", "") or (matches[0]["name"] if matches else ""),
                pretty_phone(ph), cls + auto, ov.get("summary", ""), ov.get("city") or ex.get("Location", ""),
                lmatch, lstage, dispute, redact(transcript)[:1000]],
    }

def main():
    load_config()
    ap = argparse.ArgumentParser(description="Pull, transcribe, and classify new Google LSA leads.")
    ap.add_argument("--since"); ap.add_argument("--reprocess", action="store_true")
    ap.add_argument("--keep-cache", action="store_true", help="reuse cached Leap lookups")
    ap.add_argument("--out", default=None, help="pending rows path (default: <data_dir>/pending_rows.json)")
    a = ap.parse_args()
    if not CID:
        sys.exit("Set LSA_CUSTOMER_ID or customer_id in config.json (LSA Ads customer ID, digits only).")
    if a.out is None:
        a.out = os.path.join(BASE, "pending_rows.json")
    os.makedirs(f"{BASE}/recordings", exist_ok=True); os.makedirs(f"{BASE}/transcripts", exist_ok=True)
    try:
        os.chmod(f"{BASE}/recordings", 0o700)
    except OSError:
        pass
    state = json.load(open(f"{BASE}/state.json")) if os.path.exists(f"{BASE}/state.json") else {"processed": []}
    since = a.since or (dt.date.today() - dt.timedelta(days=3)).isoformat()
    tok = ads_token()
    leads = pull(tok, since)
    done = set() if a.reprocess else set(state["processed"])
    new = sorted([l for l in leads if l["id"] not in done], key=lambda l: l["creationDateTime"])
    cache_p = f"{BASE}/leap_cache.json"
    cache = json.load(open(cache_p)) if os.path.exists(cache_p) and (a.keep_cache or not a.reprocess) else {}
    if not a.reprocess and not a.keep_cache:  # refresh Leap stage for new leads' phones
        for l in new:
            cache.pop(phone10(l), None)
    overrides = json.load(open(f"{BASE}/classifications.json")) if os.path.exists(f"{BASE}/classifications.json") else {}
    export = load_csv_export()
    built = [(l, build_row(l, tok, cache, overrides, export)) for l in new]
    deferred = [l["id"] for l, r in built if r is None]
    new = [l for l, r in built if r is not None]
    rows = [r for l, r in built if r is not None]
    json.dump(cache, open(cache_p, "w"), indent=1)
    pending = json.load(open(a.out)) if os.path.exists(a.out) and not a.reprocess else []
    pending += [r["row"] for r in rows]
    json.dump(pending, open(a.out, "w"), indent=1)
    state["processed"] = sorted(set(state["processed"]) | {l["id"] for l in new})
    state["last_run"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    json.dump(state, open(f"{BASE}/state.json", "w"), indent=2)
    print(json.dumps({"window_since": since, "leads_in_window": len(leads), "new_leads": len(new),
                      "pending_rows": len(pending), "new_lead_ids": [l["id"] for l in new],
                      "deferred_recording_not_ready": deferred}))

if __name__ == "__main__":
    main()
