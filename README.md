# Google LSA lead review

A command-line tool and setup guide for reviewing Google Local Services Ads (LSA) leads: pull calls and messages, transcribe recordings locally, classify each lead, check whether it already exists in your CRM (Leap / JobProgress is the reference implementation), log a row to a Google Sheet, and email the office when a real lead is missing from the CRM.

Developed by Linkville Roofing & Siding.

## Quick start

1. Copy the example config and fill in your values (no secrets in this file):

   ```bash
   cp config.example.json config.json
   ```

   Set at least `customer_id` (your LSA Ads customer ID, digits only) and, if you want, `data_dir`, `token_file`, `sheet_id`, and `leap_api_base`.

2. Follow the one-time Google Cloud, OAuth, spreadsheet, and CRM setup in [SKILL.md](SKILL.md). Store secrets in the environment only:

   - `LSA_GOOGLE_CLIENT_ID`
   - `LSA_GOOGLE_CLIENT_SECRET`
   - `LEAP_API_KEY`

   The refresh token lives in `~/.config/lsa/token.json` (mode 600) by default.

3. Install dependencies:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install requests numpy faster-whisper
   # also install ffmpeg with your OS package manager
   ```

4. Run a daily pass (default lookback is 3 days):

   ```bash
   python3 lsa_review.py
   ```

   Rows ready to append land in `./data/pending_rows.json`. Review transcripts, then append to the Calls tab of your sheet as described in SKILL.md. Email the office only when a new real lead is missing from the CRM.

Environment variables override `config.json`. See SKILL.md for the full column layout, classification rules, email template, and how to swap in a CRM other than Leap.

## Disclaimer

This project is not affiliated with, endorsed by, or sponsored by Google. Local Services Ads, Google Ads, and related marks are trademarks of Google LLC. Use this software at your own risk.

## License

MIT. See [LICENSE](LICENSE).
