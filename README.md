# job-radar

Scrapes Greenhouse and Lever job boards, scores postings against a keyword profile, deduplicates via SQLite, and sends Telegram alerts for new matches.

---

## How it works

1. Fetches all open roles from every company in `sources.json`
2. Scores each posting against your `profile.json` (title keywords, description keywords, location bonuses)
3. Skips anything already seen (SQLite dedup in `data/jobs.db`)
4. Sends a Telegram message for every new match above your threshold

---

## Setup

```bash
git clone https://github.com/anadivatsa/job-radar.git
cd job-radar
pip install -r requirements.txt

cp profile.example.json profile.json
cp sources.example.json sources.json
cp notifier.env.example notifier.env
```

Fill in `notifier.env` with your Telegram bot token and chat ID.  
Edit `profile.json` to match your target roles, keywords, and preferred locations.  
Edit `sources.json` with the companies you want to watch.

---

## Configuration

### `profile.json`

| Field | Description |
|---|---|
| `threshold` | Minimum score (0–100) to trigger an alert |
| `title_keywords` | Phrases to match in the job title, each with a score |
| `title_exclude` | If any of these appear in the title, the job is skipped entirely |
| `description_keywords` | Phrases to match in the job description (scores are additive) |
| `location_bonus` | Extra score if the location contains a preferred place |

Only the single highest-scoring `title_keyword` and `location_bonus` match counts — description keyword scores are all summed.

### `sources.json`

Each entry needs a `platform` (`greenhouse` or `lever`), a `token` (the company's board slug), and a `company` display name. Greenhouse EU boards also accept `"region": "eu"`.

**Finding tokens:**
- Greenhouse: `https://boards.greenhouse.io/<token>`
- Lever: `https://jobs.lever.co/<token>`

### `notifier.env`

```
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
```

Create a bot via [@BotFather](https://t.me/BotFather). Get your chat ID by messaging [@userinfobot](https://t.me/userinfobot).

---

## Usage

```bash
# Check all sources are reachable and show job counts
python3 job_match.py --check-sources

# Dry run — fetch, score, dedup, mark seen (no Telegram alert)
python3 job_match.py --run-once

# Full run with Telegram alerts
python3 job_match.py --run-once --notify
```

---

## Scheduling

To run daily at 09:00 via cron:

```bash
crontab -e
```

Add:
```
0 9 * * * cd /path/to/job-radar && python3 job_scan.py >> logs/job_scan.log 2>&1
```

---

## Telegram alert format

```
🎯 Job Match — 61/100

Product Manager, Payments
Stripe  ·  San Francisco, NY, Remote
Why: product manager · payments · cross-functional

👉 Apply now
```

---

## Supported platforms

| Platform | API |
|---|---|
| Greenhouse | `boards-api.greenhouse.io` / `boards-api.eu.greenhouse.io` |
| Lever | `api.lever.co` |
