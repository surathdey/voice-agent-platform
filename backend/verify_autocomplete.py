"""Local before/after check for the finalize_attempt auto-complete fix.

Proves, without a phone call, that a campaign completes itself when its
last lead lands — and that it does NOT complete while a lead is still
pending or retry-waiting. Uses the app's own SessionLocal (autoflush=False,
same as production) against a throwaway sqlite DB.

Usage (from backend dir):
    DATABASE_URL=sqlite:////tmp/autocomp-test.db python verify_autocomplete.py
Run it once before the fix (bug: single-lead campaign stays active) and
once after (campaign completes).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import CallSession, Campaign, Lead  # noqa: E402
from app import orchestrator  # noqa: E402

Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)
db = SessionLocal()


def make_campaign(cid, n_leads, done_first=0):
    camp = Campaign(
        campaign_id=cid, tenant_id="ten_test", agent_id="agt_test",
        name=cid, status="active",
        retry_rules={"max_attempts": 1, "retry_gap_days": 1},
    )
    db.add(camp)
    leads = []
    for i in range(n_leads):
        lead = Lead(
            lead_id=f"{cid}-led{i}", campaign_id=cid, tenant_id="ten_test",
            name="Test", phone_number="+1000000000",
            status="done" if i < done_first else "pending",
        )
        db.add(lead)
        leads.append(lead)
    db.commit()
    return camp, leads


def finalize(lead, camp, state="COMPLETED"):
    ses = CallSession(
        session_id=f"ses-{lead.lead_id}", tenant_id="ten_test",
        campaign_id=camp.campaign_id, lead_id=lead.lead_id,
        attempt_id=f"att-{lead.lead_id}", provider_mode="managed",
        channel="", state=state,
    )
    db.add(ses)
    db.commit()
    orchestrator.finalize_attempt(db, ses, {
        "final_state": state, "end_reason": "test",
        "disposition": "RPC_CONFIRMED" if state == "COMPLETED" else None,
        "confidence": None, "review_required": False,
        "summary": "test", "captured_fields": {},
        "duration_s": 40, "recording_url": None, "transcript": [],
        "cost_breakdown": {"cost_source": "provider", "total": 0.08},
    })
    db.refresh(camp)
    db.refresh(lead)
    return camp.status, lead.status


results = []
# A: single lead lands -> campaign completes
c, (l,) = make_campaign("cmp_A", 1)
results.append(("A single-lead completes", finalize(l, c) == ("completed", "done")))
# B: two leads, one already done, second lands -> completes
c, (_, l2) = make_campaign("cmp_B", 2, done_first=1)
results.append(("B last-lead completes", finalize(l2, c) == ("completed", "done")))
# C: two leads, one lands, one still pending -> stays active
c, (l1, _l2) = make_campaign("cmp_C", 2)
results.append(("C pending-lead stays active", finalize(l1, c) == ("active", "done")))
# D: retryable outcome, attempts left -> lead re-queued, campaign active
c, (l,) = make_campaign("cmp_D", 1)
c.retry_rules = {"max_attempts": 3, "retry_gap_days": 1}
db.commit()
results.append(("D retryable re-queues", finalize(l, c, state="NO_ANSWER") == ("active", "pending")))

ok = True
for name, passed in results:
    print(("PASS " if passed else "FAIL ") + name)
    ok = ok and passed
print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
