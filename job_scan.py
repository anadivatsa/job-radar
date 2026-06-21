#!/usr/bin/env python3
# SCHEDULE: daily at 09:00
# ENABLED: true
# DESCRIPTION: Scan job boards and Telegram-alert on new matches above threshold
"""Run a full job scan and send Telegram alerts for new matches."""

from job_match import run_daily_scan
import sys

result = run_daily_scan(notify=True)

if result["errors"]:
    print(f"Errors: {result['errors']}", file=sys.stderr)

print(
    f"job_scan: fetched={result['fetched']} new={result['new_matches']} "
    f"sent={result['alerts_sent']} errors={len(result['errors'])}"
)
