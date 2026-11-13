---
name: google-lsa-lead-review
description: Use when setting up or running a daily Google Local Services Ads (LSA) lead review pipeline: pull leads and call recordings, transcribe and classify them, log to a spreadsheet, check the CRM read-only, and email when real leads are missing from the CRM.
---

# Google LSA lead review

A portable setup and operations guide for reviewing Google Local Services Ads leads (phone calls and messages), logging them to a spreadsheet, checking whether they already exist in your CRM, and emailing the office when a real lead is missing from the CRM.

The working Python reference implementation lives next to this skill as `lsa_review.py`. It is the source of truth for query fields, classification heuristics, redaction, CRM matching, and row layout. Point it at your environment with `config.json` (see `config.example.json`) or environment variables. Do not copy secrets into this skill, into chat, into the spreadsheet, or into email.

The reference CRM is Leap (JobProgress). The script talks to Leap with read-only GET requests. To use a different CRM, swap the lookup functions but keep the same sheet columns and match-text conventions so the Summary formulas and the missing-lead email still work. See [Using a different CRM](#using-a-different-crm).

## Overview

Each weekday morning the agent:

1. Pulls new LSA leads and conversations from the Google Ads API (v23 REST).
2. Downloads call recordings and transcribes them locally.
3. Looks up the caller in the CRM with read-only GET requests (phone first, then email). It does not search the CRM by name.
4. Classifies each lead (human review of the transcript, with a keyword heuristic as fallback).
5. Appends one new row per lead to the Calls tab of the review spreadsheet. Old rows are never edited.
6. If any new **Real lead** is missing from the CRM, sends one email report for that run to the office reviewer.
7. Gives the operator a short recap. If there are no new leads, keep that recap short and do not email the office.

Dedupe is by LSA lead ID in a local `state.json` file. Recordings that are not ready yet are deferred to the next run and are not marked processed.

## Prerequisites

- A Google Cloud project with the **Google Ads API** enabled. Also enable **Local Services API** in the same project if it appears in the API library. The working queries use the Google Ads `googleAds:search` endpoint only; this guide did not separately verify that Local Services API is required for those GAQL queries.
- OAuth 2.0 **Desktop** client credentials for that project, and a Google account that can see the LSA Ads customer.
- **Explorer** access on the Cloud project is enough (2,880 operations per day on production accounts). **Basic** access requires Brand Verification and is not needed for this volume.
- Python 3 with `requests`. For local transcription: `ffmpeg`, `numpy`, and `faster-whisper`. Versions known to work: `faster-whisper 1.2.1`, `numpy 2.5.4`, `requests 2.34.2`, `ffmpeg 7.1.x`.
- A CRM API key with permission to **read** customers and jobs. The reference implementation uses a Leap (JobProgress) key. Never write to the CRM.
- Access to a Google Sheet you will append to, and a way to send email from the company mailbox.

**Access-level note (as of 2026):** Google sunset developer tokens on **September 9, 2026**. Access level now belongs to the Google Cloud project, on the **Google Ads API Overview** page in Cloud Console. Do not use the old API Center developer-token request form. The working script sends only an `Authorization: Bearer` access token. It does not send a `developer-token` header.

## One-time setup

### 1. Cloud project and API access

1. In Google Cloud Console, create or pick a project.
2. Enable Google Ads API (and Local Services API if listed).
3. Open the Google Ads API Overview page for that project and confirm access is at least **Explorer**.

### 2. OAuth client and publishing

1. Create an OAuth **Desktop app** client. Save the client ID and client secret as environment variables, not in this skill or in a git repo:
   - `LSA_GOOGLE_CLIENT_ID`
   - `LSA_GOOGLE_CLIENT_SECRET`
2. In Google Auth Platform (or APIs & Services > OAuth consent screen):
   - User type **External**.
   - Under **Branding**, add a homepage URL and a privacy policy URL. Without both, **Publish app** stays disabled.
   - Publish the app to **In production**. If you leave it in **Testing**, refresh tokens expire 7 days after sign-in.
   - While the app is in Testing, add the Google accounts that will sign in as test users. After it is In production, test users are not required, but Google may still show an unverified-app warning until the app is verified.
3. Add an authorized redirect URI exactly equal to `http://localhost:8765` (no trailing slash unless you registered one).
4. Scope: `https://www.googleapis.com/auth/adwords`.

A client ID is not a secret. The client secret and any refresh token are secrets. The OAuth client JSON file alone grants nothing; each agent must complete its own sign-in and keep its own refresh token.

### 3. Link the Google Ads manager to the LSA customer if needed

The signed-in Google account must be able to select the LSA customer. If the LSA account is under a manager account, link them first:

1. In Google Ads, from the **manager** account, send a link request to the LSA sub-account (in sub-account settings, use the **+** control to add the link). If no request arrives on the client account, the request was not actually sent; resend from the manager, not from the client.
2. The owner of the LSA account may need to approve a **multi-party approval**. Use **View requests** / **Multi-party approvals**, not a generic Accept button that does not complete the link.
3. Confirm access by listing customers the token can see (step 5).

### 4. One-time sign-in (refresh token)

Each agent does this once (and again if the token is revoked or was issued while the app was in Testing).

Build the auth URL (replace the client ID, keep the other parameters):

```
https://accounts.google.com/o/oauth2/v2/auth?client_id=YOUR_CLIENT_ID&redirect_uri=http%3A%2F%2Flocalhost%3A8765&response_type=code&scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fadwords&access_type=offline&prompt=consent
```

Optional: add `login_hint=` with the Google account that administers LSA, so the right user is pre-selected.

Have a person open that URL while signed into the Google account that can see the LSA customer:

1. If Google shows an unverified-app warning, click **Continue** (or Advanced, then continue to the app).
2. Click **Allow**.
3. The browser will go to `http://localhost:8765/?code=...`. A listener on port 8765 is optional. If the page fails to load, copy the full address bar URL anyway; the authorization code is the `code` query parameter. The code is a secret and expires quickly.

Exchange the code for tokens (do not log the response):

```bash
curl -s -X POST https://oauth2.googleapis.com/token \
  -d grant_type=authorization_code \
  -d code=AUTHORIZATION_CODE \
  -d client_id="$LSA_GOOGLE_CLIENT_ID" \
  -d client_secret="$LSA_GOOGLE_CLIENT_SECRET" \
  -d redirect_uri=http://localhost:8765
```

Store at least the `refresh_token` in a JSON file, mode `600`, outside the skill directory and outside git. Default path: `~/.config/lsa/token.json` (override with `LSA_TOKEN_FILE` or `token_file` in `config.json`). The file may also contain a short-lived `access_token`; the reference script reads only `refresh_token` and always requests a new access token.

If you published the app after an earlier Testing-mode sign-in, **sign in again**. Tokens issued while the app was in Testing still expire after 7 days.

### 5. Confirm API access

Refresh an access token:

```bash
curl -s -X POST https://oauth2.googleapis.com/token \
  -d grant_type=refresh_token \
  -d refresh_token=YOUR_REFRESH_TOKEN \
  -d client_id="$LSA_GOOGLE_CLIENT_ID" \
  -d client_secret="$LSA_GOOGLE_CLIENT_SECRET"
```

If this returns `invalid_grant`, redo step 4 and tell the operator. Do not keep retrying a dead refresh token.

List customers the token can access:

```bash
curl -s -H "Authorization: Bearer $ACCESS_TOKEN" \
  https://googleads.googleapis.com/v23/customers:listAccessibleCustomers
```

Then run a small GAQL search against the LSA customer ID (no dashes in the URL). The reference script does **not** send a `login-customer-id` header by default, which is enough when the signed-in user can already select the LSA customer. If searches fail with a permission or "customer not found" error after you signed in with a manager-only user, set `LSA_LOGIN_CUSTOMER_ID` (or `login_customer_id` in `config.json`) to the manager ID, digits only. The script will then send header `login-customer-id: YOUR_MANAGER_ID`.

### 6. CRM API key (Leap reference)

Export `LEAP_API_KEY` in the environment. The reference script calls `https://api.jobprogress.com/api/v3` (override with `LEAP_API_BASE`) with `Authorization: Bearer` and `Accept: application/json`. Test with a GET to `/customers` using a known phone or email. If you get HTTP 429, honor `Retry-After` (the script retries and sleeps about 1.05 seconds between successful calls).

If you are not using Leap, skip this Leap-specific test and implement the lookup described in [Using a different CRM](#using-a-different-crm).

### 7. Spreadsheet

Create a Google Sheet with two tabs: **Calls** and **Summary**.

Calls row 1 headers, in this exact order (the script emits rows to match):

1. Date/time (Pacific)
2. Lead ID
3. Lead type
4. Service category
5. Charged?
6. Lead status
7. Call length
8. Caller name
9. Caller phone
10. Classification
11. Summary
12. City
13. Leap match
14. Leap stage
15. Should dispute/review?
16. Transcript

The date/time header says Pacific because the reference pipeline used an Ads account set to `America/Los_Angeles`. The API value is already in the Ads account time zone. Rename the header if your account uses a different zone; keep the column order.

Share the sheet with the account the agent will use to append rows. Store the spreadsheet ID as `sheet_id` in `config.json` (or in a small `sheet.json` under your data directory). Leave the Calls tab empty below the header, or already filled with historical append-only rows.

The Summary tab formulas read the Calls tab and update on their own after appends. Recreate them as follows (classification matches use a trailing `*` so a daily "(auto)" suffix still counts):

| Cell | Formula or label |
|---|---|
| A1 | LSA Call Review - summary |
| A2 | Window note (plain text) |
| A4 / B4 / C4 | Classification / Leads / % of all |
| A5 | Real lead |
| B5 | `=COUNTIF(Calls!J:J,"Real lead*")` |
| C5 | `=IFERROR(B5/$B$10,0)` |
| A6 | Existing customer |
| B6 | `=COUNTIF(Calls!J:J,"Existing customer*")` |
| C6 | `=IFERROR(B6/$B$10,0)` |
| A7 | Spam / sales pitch / vendor |
| B7 | `=COUNTIF(Calls!J:J,"Spam / sales pitch / vendor*")` |
| C7 | `=IFERROR(B7/$B$10,0)` |
| A8 | Wrong number / wrong service |
| B8 | `=COUNTIF(Calls!J:J,"Wrong number / wrong service*")` |
| C8 | `=IFERROR(B8/$B$10,0)` |
| A9 | Missed / no conversation |
| B9 | `=COUNTIF(Calls!J:J,"Missed / no conversation*")` |
| C9 | `=IFERROR(B9/$B$10,0)` |
| A10 | Total leads |
| B10 | `=COUNTA(Calls!B:B)-1` |
| C10 | `=IFERROR(B10/$B$10,0)` |
| A13 / B13 | % real leads (of all leads) / `=IFERROR(B5/B10,0)` |
| A14 / B14 | % real leads (of leads with a conversation) / `=IFERROR(B5/(B10-B9),0)` |
| A15 / B15 | Charged leads / `=COUNTIF(Calls!E:E,"Yes")` |
| A16 / B16 | Charged but NOT a real lead / `=COUNTIFS(Calls!E:E,"Yes",Calls!J:J,"<>Real lead*")` |
| A17 / B17 | ...of which existing customers / `=COUNTIFS(Calls!E:E,"Yes",Calls!J:J,"Existing customer*")` |
| A18 / B18 | ...of which wrong number / wrong service / spam / `=COUNTIFS(Calls!E:E,"Yes",Calls!J:J,"Wrong*")+COUNTIFS(Calls!E:E,"Yes",Calls!J:J,"Spam*")` |
| A19 / B19 | Flagged "Yes" to dispute / `=COUNTIF(Calls!O:O,"Yes*")` |
| A20 / B20 | Flagged "Review" / `=COUNTIF(Calls!O:O,"Review*")` |
| A21 / B21 | Already credited / in review / `=COUNTIF(Calls!E:E,"Credited")+COUNTIF(Calls!E:E,"In review")` |
| A22 / B22 | Real leads found in Leap / `=COUNTIFS(Calls!J:J,"Real lead*",Calls!M:M,"*job*")+COUNTIFS(Calls!J:J,"Real lead*",Calls!M:M,"Customer:*")` |
| A23 / B23 | Real leads NOT found in Leap / `=B5-B22` |
| A24 | Converted per Leap (see notes below) |
| B24 | `=SUMPRODUCT(COUNTIFS(Calls!J:J,"Real lead*",Calls!N:N,{"Signed / No Production","Ready to Produce","Job Completed","Q.C. and billed","Paid","Work"},Calls!M:M,"*new since call*"))+SUMPRODUCT(COUNTIFS(Calls!J:J,"Real lead*",Calls!N:N,{"Signed / No Production","Ready to Produce","Job Completed","Q.C. and billed","Paid","Work"},Calls!M:M,"*after call*"))` |
| A25 / B25 | Real leads with appointment set / estimate sent / `=SUMPRODUCT(COUNTIFS(Calls!J:J,"Real lead*",Calls!N:N,{"appointment set","Est Sent/Follow Up","Lead"}))` |

B24 and B25 use **example** Leap (JobProgress) stage names (`Signed / No Production`, `Ready to Produce`, `Job Completed`, `Q.C. and billed`, `Paid`, `Work`, `appointment set`, `Est Sent/Follow Up`, `Lead`). Change the stage lists if your CRM uses different names. The same example names are the default `won_stages` in the reference script and in `config.example.json`.

### 8. Local data directory

Pick a private directory (default `./data`, override with `LSA_DATA_DIR` or `data_dir` in `config.json`). Create `recordings/` and `transcripts/` under it, mode `700` on the recordings directory. Start `state.json` as `{ "processed": [] }` if you are beginning from scratch. If you are taking over an existing pipeline, copy the existing `state.json` so historical lead IDs are not appended again.

Other working files the script reads or writes (all under the data directory unless noted):

- `pending_rows.json`: array of row arrays in Calls-tab column order, or `[]` when empty.
- `classifications.json`: optional human/agent overrides, map of lead ID to fields.
- `leap_cache.json`: cache of CRM lookups, keyed by 10-digit phone or lowercase email.
- `sheet.json` or `sheet_id` in `config.json`: spreadsheet ID and URL (no secrets).

Optional: a dashboard CSV export (set `LSA_LEADS_CSV` or `leads_csv` in `config.json`) can fill City (`Location`) and finer charge status (`Charge status` = `Credited` or `In review`). This is optional; the API alone is enough to run.

### 9. Point the script at your environment

Copy `config.example.json` to `config.json` (or set environment variables) and fill in at least:

- `customer_id` / `LSA_CUSTOMER_ID` (LSA customer ID, digits only)
- `data_dir` / `LSA_DATA_DIR` (default `./data`)
- `token_file` / `LSA_TOKEN_FILE` (default `~/.config/lsa/token.json`)
- `leap_api_base` / `LEAP_API_BASE` if your Leap host is not `https://api.jobprogress.com/api/v3`
- `won_stages` and `service_names` if your pipeline or trades differ
- `leads_csv` / `LSA_LEADS_CSV` if you use the optional dashboard export
- `sheet_id` so the agent knows which spreadsheet to append to

Do not put `LSA_GOOGLE_CLIENT_SECRET`, refresh tokens, or `LEAP_API_KEY` in `config.json`. Keep those in the environment (or a gitignored `.env` that you load yourself). Add `config.json` to `.gitignore` if it will contain your real account IDs.

Install: `ffmpeg`, plus `pip install requests numpy faster-whisper` in a virtualenv if the OS blocks system pip.

## Daily run procedure

Schedule this on weekdays at a consistent morning time in the Ads account time zone (the reference schedule is 8:00 AM Pacific Time, `America/Los_Angeles`).

1. **Refresh the access token.** If the token endpoint returns `invalid_grant` or the script exits with "Token refresh failed", stop. Tell the operator to redo the one-time sign-in. Do not email the office about an auth failure.

2. **Pull new leads.** From the directory that contains `lsa_review.py` and `config.json`:

   ```bash
   python3 lsa_review.py
   ```

   Default lookback is 3 days. The script skips lead IDs already in `state.json`, downloads new recordings, transcribes them, looks up the CRM, and writes rows to `pending_rows.json`. It prints a JSON summary with `new_lead_ids` and `deferred_recording_not_ready`.

   Useful flags:

   - `--since YYYY-MM-DD` to set the window start
   - `--reprocess` to ignore `state.json` and rebuild every lead in the window
   - `--keep-cache` to reuse `leap_cache.json` instead of refreshing phones for new leads
   - `--out PATH` to write rows somewhere other than `pending_rows.json`

3. **Review transcripts and classify.** Read `transcripts/<conversation_id>.txt` (and the message text in the pending row). Write overrides to `classifications.json`:

   ```json
   {
     "123456789": {
       "classification": "Real lead",
       "summary": "Wants a price range for a new roof on a small shed, leaning toward metal",
       "city": "",
       "service": "Roof installation"
     }
   }
   ```

   Allowed override keys: `classification`, `summary`, `city`, `service`, `dispute`. If `classification` is omitted, the script uses its keyword heuristic and appends ` (auto)` to the Classification cell so someone can come back to it.

   After updating overrides, regenerate the pending rows **before** appending:

   ```bash
   python3 lsa_review.py --since YYYY-MM-DD --reprocess --keep-cache --out /tmp/lsa_rows.json
   ```

   Then replace `pending_rows.json` with that output, or edit `pending_rows.json` directly and skip the re-run.

   **Do not `--reprocess` and append again after those lead IDs are already in the Calls tab.** That would duplicate rows. `--reprocess` with the default `--out` also replaces `pending_rows.json` instead of merging.

4. **Redact before anything is written.** The script already redacts the Transcript cell (see Security). If you edit rows by hand, apply the same rules. Do not paste raw card numbers into classifications, chat, the sheet, or email.

5. **Append once.** Append every pending row to the Calls tab, immediately under the last row. Use raw values (`valueInputOption=RAW` / parse-input false) so phone numbers and lead IDs are not auto-converted. Never edit older Calls rows. The Summary tab updates from formulas.

   Example, Sheets API:

   ```
   POST https://sheets.googleapis.com/v4/spreadsheets/YOUR_SHEET_ID/values/Calls!A:P:append?valueInputOption=RAW
   {"values":[ ["2026-11-13 05:50","123456789", "..."] ]}
   ```

6. **Clear the queue.** After a successful append, write `[]` to `pending_rows.json`.

7. **Email only if needed.** If any **new** row has Classification starting with `Real lead` and Leap match is `No match`, blank, or otherwise not a CRM customer/job, send **one** email for the whole run (see Email template). Do not email for existing customers, wrong-service calls, spam, or missed calls. Do not attach recordings or full transcripts.

8. **Recap the operator.** Short note: how many new leads, how they classified, anything flagged to dispute or review, whether the office email went out, and the sheet link. If there were no new leads, keep this quiet and short.

## Data schema

### Calls tab columns

| Column | Header | Source / rules |
|---|---|---|
| A | Date/time (Pacific) | LSA `creation_date_time`, formatted `YYYY-MM-DD HH:MM`. The API value is already in the Ads account time zone (example: `America/Los_Angeles`). |
| B | Lead ID | `local_services_lead.id` (string of digits). Dedupe key. |
| C | Lead type | `PHONE_CALL` -> `Phone call`; `MESSAGE` -> `Message`. |
| D | Service category | Override `service`, else mapped `service_id` (see default `service_names` in `config.example.json`), else `Roofer (no job type)`. `service_id` is often omitted by the API. Customize the map for your trades. |
| E | Charged? | `Yes` if `lead_charged` is true; `No` if it is omitted or false; `Credited` if `credit_details.credit_state` is `CREDITED` or the optional CSV says Credited; `In review` from the optional CSV. |
| F | Lead status | `lead_status` title-cased. Observed values include `ACTIVE`, `DECLINED`, `BOOKED`. |
| G | Call length | Total phone-call duration as `M:SS`, plus ` (N calls)` if there were multiple calls. Blank for message-only leads. |
| H | Caller name | LSA `contact_details.consumer_name` if present (often missing), else the CRM customer name, else blank. Message leads often have no name. Reviewers may correct spelling in the pending row. |
| I | Caller phone | 10-digit US number formatted `(AAA) PPP-NNNN`, or blank. Message leads often have email only. |
| J | Classification | One of the five classes in the Classification guide, plus ` (auto)` when no override was provided. |
| K | Summary | Short human summary of what they wanted. Leave blank rather than inventing. |
| L | City | Override `city`, else optional CSV `Location`. The GAQL query used here does not request a city field. |
| M | Leap match | See CRM matching below. The header says Leap because that is the reference CRM; keep the header so the Summary formulas still work, or update those formulas if you rename it. |
| N | Leap stage | Newest job's `current_stage` name, or `Customer only`, or blank if no match. |
| O | Should dispute/review? | See dispute rules. |
| P | Transcript | Redacted text, truncated to 1000 characters. Phone calls are prefixed `[Call MM-DD HH:MM M:SS]`. Messages are prefixed `[Message]`. |

### state.json

```json
{
  "processed": ["123456789", "123456790"],
  "last_run": "2026-11-13T08:00:07-08:00"
}
```

Leads listed in `processed` are skipped on later runs unless `--reprocess` is set. Deferred recordings are **not** added to `processed`.

### pending_rows.json

A JSON array of row arrays, each with 16 values in the Calls column order. Empty queue: `[]`.

### classifications.json

A JSON object keyed by lead ID. Not every lead needs an entry. Unknown keys should be ignored by reviewers; only the five override fields above are used.

### Google Ads queries

`POST https://googleads.googleapis.com/v23/customers/{YOUR_LSA_CUSTOMER_ID}/googleAds:search`

Body: `{"query": "..."}` plus `pageToken` when `nextPageToken` is present. GAQL used by the script (snake_case in the query; the JSON response is camelCase):

```
SELECT local_services_lead.id, local_services_lead.lead_type, local_services_lead.lead_status,
       local_services_lead.creation_date_time, local_services_lead.category_id,
       local_services_lead.service_id, local_services_lead.lead_charged,
       local_services_lead.contact_details, local_services_lead.note.description,
       local_services_lead.credit_details.credit_state
FROM local_services_lead
WHERE local_services_lead.creation_date_time >= '{since} 00:00:00'
```

```
SELECT local_services_lead_conversation.id, local_services_lead_conversation.conversation_channel,
       local_services_lead_conversation.participant_type, local_services_lead_conversation.event_date_time,
       local_services_lead_conversation.lead,
       local_services_lead_conversation.phone_call_details.call_duration_millis,
       local_services_lead_conversation.phone_call_details.call_recording_url,
       local_services_lead_conversation.message_details.text
FROM local_services_lead_conversation
WHERE local_services_lead_conversation.event_date_time >= '{since} 00:00:00'
```

The API omits empty or false fields (for example `leadCharged` is often absent when the lead was not charged). Example `category_id` values for a roofing and siding advertiser: `xcat:service_area_business_roofer`, `xcat:service_area_business_siding_pro`. Conversation channels observed: `PHONE_CALL`, `MESSAGE`, `ADS_API`, `EMAIL`. The reference script transcribes `PHONE_CALL` and stores `MESSAGE` text. It does not currently append `ADS_API` or `EMAIL` conversation text to the transcript cell.

Join conversations to leads by the trailing ID of `local_services_lead_conversation.lead` (resource name).

### Recordings and transcription

Recording URLs (on `ads.google.com/localservicesads/attachment/...`) download as `audio/mpeg` with `Authorization: Bearer` and the Ads access token. Save as `recordings/<conversation_id>.mp3` with mode `600`. If the HTTP status is not 200 or the body is shorter than 500 bytes, treat the recording as not ready. If the lead is less than 48 hours old, the script returns no row for that lead and leaves it unprocessed so the next run can retry. Google may return 500 for a while after the call.

The reference script transcribes with **faster-whisper** (`small.en` by default, override with env `WMODEL`), on CPU, `int8`, 8 threads. Audio is decoded with ffmpeg to 16 kHz mono s16le. First pass uses VAD (`beam_size=1`). If the call is longer than 60 seconds and the transcript looks too thin or looped, it retries without VAD (`beam_size=5`, `condition_on_previous_text=False`) and keeps the more varied result. Transcripts are cached as `transcripts/<conversation_id>.txt`.

Generic alternative: any speech-to-text that accepts the MP3 (or the same 16 kHz mono WAV) and writes a UTF-8 text file at that path. Whisper.cpp, OpenAI Whisper, or a hosted transcribe API will work if you keep recordings private and still redact the text before it is stored in the sheet. Do not upload recordings to a third party without an operator decision; this pipeline was built to transcribe locally.

### CRM matching (Leap reference, read-only)

`GET {LEAP_API_BASE}/customers?phone=##########&includes[]=jobs&includes[]=phones` or, if there is no 10-digit phone, `?email=...`. The script does **not** look up by name. Message leads often have an email and no phone.

A customer is treated as already existing if `created_at` is earlier than the lead time minus one day. Match text examples:

- `No match` when the lookup ran and returned no customer
- blank when there was no phone or email, or no API key
- `Customer: {name} (no job)` with stage `Customer only`
- `{name} - job {number} ({job name}) +N more job(s) [customer before call]`
- the same pattern with `[new since call]`, and optionally `; new job {number} after call`

"Converted" in the script means a job in a won stage (example Leap names: `Signed / No Production`, `Ready to Produce`, `Job Completed`, `Q.C. and billed`, `Paid`, `Work`) was created after that same one-day cutoff, and the classification is `Real lead`. That flag is used internally; the sheet shows stage and match text instead.

Never create, update, or otherwise write CRM customers or jobs from this pipeline.

### Using a different CRM

Leap (JobProgress) is the reference implementation, not a requirement. To plug in another CRM:

1. Keep the Calls tab columns, especially **Leap match** and **Leap stage** (or rename them and update the Summary formulas).
2. Replace `leap_get` / `leap_lookup` / `leap_summary` in `lsa_review.py` (or add a thin adapter) so a phone-or-email lookup returns the same three outputs: match text, stage text, and whether the customer existed before the lead.
3. Use the same match-text patterns above (`No match`, `Customer: … (no job)`, `[customer before call]`, `[new since call]`, `; new job … after call`) so the Summary formulas and the "missing from CRM" email rule keep working.
4. Put your won and in-progress stage names in `won_stages` in `config.json`, and update the B24 / B25 formulas to match.
5. Store the CRM API base URL and key in environment variables (for example `LEAP_API_BASE` can point at a compatible host, or you can add your own). Never write to the CRM, and never put the API key in this skill, in git, in the sheet, or in email.

## Classification guide

Use exactly these Classification values (the Summary formulas depend on them):

1. **Real lead** - A new inquiry for work the company actually does (for example: roofing and siding install, repair, inspection, and similar; adjust this list to the trades you advertise in LSA). Labor-only jobs where the caller already has materials can still be real leads. Realtors asking for an estimate are real leads. Follow-up messages that are clearly a duplicate of another LSA lead may still be labeled Real lead but flagged for dispute.
2. **Existing customer** - They were already in the CRM before the call, or the conversation is about a scheduled job, invoice, payment, warranty, or work the company already did. Existing-estimate follow-ups belong here.
3. **Wrong number / wrong service** - Includes job applicants and hiring questions, materials-only requests (the caller wants to buy product, not have the company install it), and callers who reached the wrong company.
4. **Spam / sales pitch / vendor** - Vendors, marketers, financing pitches, SEO, and similar. Use this when the caller is selling to the company, not buying a job.
5. **Missed / no conversation** - Hung up during the greeting, no speech, or too little conversation to classify. The heuristic uses this when a phone call is under 15 seconds or the transcript has fewer than 20 words. Do not use it for a short but clear wrong-service or applicant call.

The script's fallback heuristic (when there is no override): message leads default to Real lead; then it checks short/empty audio, spam keywords, existing-customer keywords or a pre-existing CRM customer, then wrong-service keywords, else Real lead. Always prefer a human (or agent) read of the transcript over the heuristic. Leave `(auto)` rows only when you could not review them yet.

### Dispute rules

Computed by `dispute_flag` unless `classifications.json` supplies `dispute`:

- If Charged? is not `Yes`: `Already credited` or `Already in review with Google` when that applies; otherwise blank.
- If classification is Real lead: blank (unless you override, for example a likely duplicate: `Yes - likely duplicate of message lead {id}`).
- If classification is Existing customer and Charged? is `Yes`: `Review - existing customer (not new business)`. These are not automatically disputable; a person decides.
- Otherwise, if Charged? is `Yes` and it is not a real lead (wrong service, applicant, materials only, spam, missed): `Yes - charged, not a real lead`.

Google's current LSA credit process is mostly automatic: you rate the lead in the LSA Leads inbox (one rating per lead) and Google may credit it within about 30 days. Confirm the current rules on Google's Local Services help pages before treating a flag as a guarantee of a refund.

## Email template

Send **one** email per run, and only when at least one new Real lead is missing from the CRM.

- To: the office reviewer (`office@example.com` or whatever address the operator names)
- From: the company mailbox used for this process
- CC: operator decision. Do not assume a CC unless the operator has confirmed it.
- Subject: `LSA leads missing from Leap - {Weekday}, {Mon} {D}, {YYYY}` (say "your CRM" instead of Leap if you have swapped CRMs)
- Plain language. Do not use em dashes. Do not invent a city, phone, or name. If a name came from the recording or from LSA, say so.
- **Never type a signature block** (name, title, phone) into the body when the mailbox adds one automatically. A typed signature plus an automatic signature appears twice. End the body with "Thanks," and stop.

```
Subject: LSA leads missing from Leap - Friday, Nov 13, 2026

Hi,

Today's Google LSA review found 2 new real leads that are not in Leap yet. Could you take a look and add them if they should be in the system?

The full log is here:
https://docs.google.com/spreadsheets/d/YOUR_SHEET_ID/edit

1. Jane Example (name from the call recording)
Date/time (PT): 2026-11-13 5:50 AM
Phone: (555) 555-0100
Email: jane@example.com
City: Springfield
Service: Roof installation
Summary: Wants a price range for a new metal roof on a 16x10 shed.

2. Name not given
Date/time (PT): 2026-11-13 7:12 AM
Phone: none on the lead
Email: person@example.com
City: not given
Service: Roof repair
Summary: Messaged asking about a leak over the garage.

Names are taken from the call recording or the LSA lead, so spelling may not be perfect.

Thanks,
```

Keep "Thanks," but add no typed name or signature after it; the mailbox signature follows automatically.

## Scheduling

- Weekdays at a consistent morning time in the Ads account time zone (example: 8:00 AM Pacific Time).
- Look back 3 days so a delayed recording can still be picked up.
- No new leads: short quiet recap to the operator only; no office email.
- New leads but none missing from the CRM: log them, recap the operator, no office email.
- OAuth failure: recap the operator with "redo the one-time sign-in"; no office email.

## Security

- Never put `LSA_GOOGLE_CLIENT_SECRET`, refresh tokens, access tokens, authorization codes, or `LEAP_API_KEY` in this skill, in chat, in the spreadsheet, in `config.json`, or in email. Use environment variable names and file paths only.
- Token file and recordings: mode `600`. Recordings stay on the local machine. They may contain personal data (names, addresses, phone numbers, payment details).
- Redact payment data from transcripts before they are written anywhere. The script replaces:
  - 13 to 19 digit runs (spaces or hyphens allowed between digits) with `[card # removed]`
  - spoken CVV phrases ("numbers/code on the back ...") with a short `[removed]` marker
  - spoken expiration dates after "expiration" with `[removed]`
- Long numeric job numbers can look like card numbers. If a CRM job number is wrongly redacted, fix that in the Leap match cell carefully without putting a real card number in any file.
- Never send promotional texts to customers from this process. This pipeline only reviews LSA leads, updates the sheet, and emails internal staff.
- Do not attach recordings, token files, or unredacted transcripts to email.

## Troubleshooting

- **Token refresh failed / `invalid_grant`:** The refresh token was revoked, or it was issued while the OAuth app was in Testing and has expired (7 days). Redo the sign-in with `access_type=offline` and `prompt=consent`. After publishing the app, you must sign in again to get a non-expiring production token.
- **Publish app is disabled:** Branding is missing a homepage URL or a privacy policy URL. Save both, then publish. Until it is In production, new tokens will keep expiring.
- **Unverified-app warning:** Expected until Google verifies the app. Continue, then Allow. This can still appear after the app is In production.
- **No link request on the LSA account:** The manager did not actually send it. From the manager account, use **+** in sub-account settings to send the link.
- **Link sits unapproved:** Look under **View requests** / **Multi-party approvals** on the owner account. A plain Accept control may not complete a multi-party request.
- **Sign-in or passkey problems:** Sign in with the Google account that already has access to the LSA account. A passkey or saved login for a different person or device may not work here.
- **Outdated API Center / developer token form:** Ignore it. Use Cloud Console > Google Ads API Overview for the project's access level. The working calls do not send a developer-token header.
- **Permission error on `googleAds:search`:** Confirm `listAccessibleCustomers` includes the LSA customer. If you authenticated a manager user, set `LSA_LOGIN_CUSTOMER_ID` (digits only). Confirm the manager-to-client link is approved.
- **Recording download fails or returns a tiny body:** Wait and retry on the next run if the lead is under 48 hours old. The script already defers these.
- **Thin or looping transcript ("Hello" repeated):** The script retries without VAD on long calls. You can also re-transcribe that file with a larger Whisper model (`WMODEL`) and replace `transcripts/<id>.txt` (delete the old file first so it is not skipped).
- **Message lead has no name or phone:** Normal. Look up the CRM by email. In the office email, say "Name not given" and "Phone: none on the lead" rather than guessing.
- **Duplicate rows in the sheet:** A `--reprocess` run was appended after those lead IDs were already logged. Stop, do not delete history unless the operator asks, and rely on `state.json` going forward.
- **Heuristic marked a lead `(auto)` incorrectly:** Add a `classifications.json` override and, if the row is not in the sheet yet, regenerate pending rows. If it is already appended, leave the old row as-is unless the operator asks for a correction.

## Your configuration

Fill this in for your company. These are not API defaults. Do not put secrets in this table, in this skill, or in a public repo.

| Item | Where to set it | Notes |
|---|---|---|
| LSA Ads customer ID | `LSA_CUSTOMER_ID` or `customer_id` in `config.json` | `YOUR_LSA_CUSTOMER_ID` (digits only, no dashes) |
| Manager account ID | `LSA_LOGIN_CUSTOMER_ID` or `login_customer_id` | Optional. Use only if `googleAds:search` fails and you signed in with a manager user. |
| Google account to sign in with | operator, during the one-time OAuth sign-in | Must be able to see the LSA customer. Each agent keeps its own refresh token and must not reuse another agent's `token.json`. |
| OAuth desktop client | Google Cloud Console | Create your own. Put the client ID and secret in `LSA_GOOGLE_CLIENT_ID` and `LSA_GOOGLE_CLIENT_SECRET`. The secret is never committed. |
| Token file | `LSA_TOKEN_FILE` or `token_file` | Default `~/.config/lsa/token.json`, mode 600 |
| Data directory | `LSA_DATA_DIR` or `data_dir` | Default `./data` |
| Review spreadsheet | `sheet_id` in `config.json` (or `sheet.json` under the data directory) | `YOUR_SHEET_ID`. Tabs: Calls and Summary. |
| Office reviewer email | your mailer / agent config | Example: `office@example.com` |
| From mailbox | the company mailbox used for this process | If that mailbox appends a signature automatically, never type one in the body. End with "Thanks,". |
| CC on missing-lead emails | operator decision | Do not assume a CC unless the operator has confirmed it. |
| Time zone | Ads account time zone | Example: `America/Los_Angeles` (Pacific Time) |
| Schedule | your scheduler | Example: weekdays, 8:00 AM in the account time zone |
| Leap / CRM API | `LEAP_API_BASE` and `LEAP_API_KEY` | Default base `https://api.jobprogress.com/api/v3`. The key stays in the environment. See [Using a different CRM](#using-a-different-crm) to swap hosts. |
| Optional LSA dashboard CSV | `LSA_LEADS_CSV` or `leads_csv` | Fills City and finer charge status when present. |
| Won / pipeline stage names | `won_stages` (and the Summary tab formulas) | Defaults are example Leap stage names; change them to match your CRM. |
| Service name map | `service_names` | Map LSA `service_id` values to labels for your trades. |
| Standing safety rules | follow in every run | Never put secrets in this skill, in chat, in the sheet, or in email. Never send promotional texts to customers from this process. Never write to the CRM. |

