# Wolf Pillar Accrual (WOLF/ACCRUE/FA/2026.11)

Wolf AI Labs. Human Enabled AI Driven.

Railway worker that builds the Financial Advisers pillar register from the FCA Register,
matches named work emails on each firm's own website, and imports them into Brevo
(list WOLF_FA Register, id 45; segment WOLF_PILLAR FA, id 9).

Railway variables (set in Railway, never in code):
- FCA_EMAIL, FCA_KEY: FCA Register API credentials
- BREVO_API_KEY: Brevo API key
- STATE_DIR: /data (mount a Railway volume here so the sweep resumes after restarts)

Local checks: `python3 wolf_accrue_fa.py --selftest`
