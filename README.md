# Internship Automation

Checks `@zero2sudo` Instagram Stories every 20 minutes, summarizes new internship / early-career opportunities with Gemini, and emails a digest only when new Stories appear.

## What it does

1. GitHub Actions runs every 20 minutes.
2. The script fetches active Stories from `@zero2sudo`.
3. Previously processed Story IDs are skipped.
4. Gemini reads the new Story screenshots and extracts company, role/program, timing, location, deadline, application link, summary, and urgency.
5. Gmail sends one HTML digest.
6. Processed Story IDs are committed back to `state.json` so the same Story is not emailed twice.

## Required GitHub Actions secrets

Go to **Settings → Secrets and variables → Actions → New repository secret** and add:

- `GEMINI_API_KEY` — create one in Google AI Studio.
- `EMAIL_FROM` — the Gmail account used to send the alert.
- `EMAIL_TO` — where the alerts should be delivered.
- `GMAIL_APP_PASSWORD` — a Google App Password for `EMAIL_FROM` (not your normal Gmail password).

Optional:

- `IG_SESSIONID` — Instagram session cookie used only if anonymous Story fetching fails. Do not add this unless the workflow tells you it is necessary.

For a same-account setup:

```text
EMAIL_FROM = matsvei.yakubovich@gmail.com
EMAIL_TO   = matsvei.yakubovich@gmail.com
```

## First test

After adding the secrets:

1. Open **Actions**.
2. Select **Internship story alerts**.
3. Choose **Run workflow**.
4. Open the run and inspect the log.

If anonymous Instagram fetching works, no Instagram login secret is needed. If the run reports that anonymous Story fetching failed, add `IG_SESSIONID` and run it again.

## Notes

- The repository can remain public; GitHub Actions secrets are not stored in the repository files.
- Never commit API keys, Gmail App Passwords, or Instagram cookies.
- Instagram is an unofficial integration here and can change/break. The script has an authenticated fallback for that reason.
- Gemini defaults to `gemini-2.5-flash-lite`, which currently has a free API tier.
