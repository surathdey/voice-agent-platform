"""Backfill S3 recording archival for already-completed calls.

Calls finalized BEFORE S3 archival shipped (migration 012) have a
Vapi-only recording: this script downloads each one server-side (fresh
signed URL, tenant's own Vapi key from the platform DB) and uploads it
to the tenant's PRIVATE bucket, then stamps the session row — the same
app.s3_recordings.archive_session_recording finalize uses, so a row
the script touches behaves exactly like a row finalized after 012.

USAGE (from the repo root, backend venv):
    backend/.venv/bin/python scripts/backfill_s3_recordings.py --dry-run
    backend/.venv/bin/python scripts/backfill_s3_recordings.py [--limit N]

ENV: DATABASE_URL (the platform database) and S3_RECORDINGS_BUCKET
(required for a real run; a dry run lists candidates without it).
AWS credentials come from boto3's default chain (instance IAM role
first). The script NEVER prints keys, URLs, or call content — only
counts and the session ids that failed, so they can be retried.

SAFETY: rows already archived (recording_s3_key set) are skipped, so
re-running is safe; stub and self-hosted sessions are never touched.
This script is NOT run automatically — it is a one-off operator tool.
"""

import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "backend"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="list candidates without downloading or uploading")
    ap.add_argument("--limit", type=int, default=0,
                    help="archive at most N sessions (0 = no limit)")
    args = ap.parse_args()

    from app import s3_recordings
    from app.db import SessionLocal
    from app.models import CallSession

    bucket = s3_recordings.bucket_name()
    if not args.dry_run and not bucket:
        print("ERROR: S3_RECORDINGS_BUCKET is not set; refusing a real run.")
        return 1

    db = SessionLocal()
    try:
        q = (db.query(CallSession)
             .filter(CallSession.provider_mode == "managed",
                     CallSession.provider_call_id != "",
                     ~CallSession.provider_call_id.like("stub_%"),
                     CallSession.recording_s3_key == "")
             .order_by(CallSession.started_at, CallSession.session_id))
        if args.limit:
            q = q.limit(args.limit)
        rows = q.all()
        print(f"candidates: {len(rows)}"
              + (" (dry run — nothing downloaded or uploaded)"
                 if args.dry_run else ""))
        if args.dry_run:
            return 0
        archived, failed = 0, []
        for s in rows:
            key = s3_recordings.archive_session_recording(db, s)
            if key:
                db.commit()
                archived += 1
            else:
                db.rollback()
                failed.append(s.session_id)
        print(f"archived: {archived}")
        print(f"failed: {len(failed)}")
        if failed:
            print("failed sessions (retryable via a later run):")
            for sid in failed:
                print(f"  {sid}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
