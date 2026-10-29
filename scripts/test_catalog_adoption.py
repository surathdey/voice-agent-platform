#!/usr/bin/env python3
import os
"""Epic C, story C2 acceptance tests: catalog + adoption + campaign pinning.

Usage:  python3 scripts/test_catalog_adoption.py [base_url]  (default http://localhost:8000)
        Run against a FRESH database (the backend seeds its own agents;
        this suite creates its own catalog agents, so it never collides).

Covers (plan "Epic C Plan", C2 acceptance):
- Publish a new version changes no tenant until adoption.
- An adopting tenant gets the new prompt text; a running campaign keeps
  its old (pinned) version for life, proven end-to-end through a stub
  dispatch whose disposition comes from the PINNED prompt's buckets.
- The adoption log shows exactly which tenant runs which version.
- The adoption dropdown shows suitable_for / model / validated_on for
  every version, and a version unproven on the tenant's stack is
  visibly flagged (flag logic unit-tested for managed/self_hosted).
- Versions are immutable: no update/delete route exists (405).
- A manual prompt edit diverges the agent from its adopted version
  (markers cleared), so a later campaign pins the edited text, not a
  catalog version it no longer equals.
- Cross-box import mode: a version payload from the export endpoint
  adopts cleanly without a same-DB catalog lookup.
"""
import json
import sys
import urllib.request
import urllib.error

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


def call(method, path, body=None, token=None, expect=None):
    data = None
    if body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            status, payload = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, payload = e.code, e.read()
    try:
        payload = json.loads(payload) if payload else None
    except Exception:
        payload = None
    if expect is not None:
        check(f"{method} {path} -> {status} (expected {expect})", status == expect,
              f"body={payload}")
    return status, payload


def call_upload(path, csv_text, token, mapping, expect=None):
    boundary = "----c2test"
    fields = f"--{boundary}\r\nContent-Disposition: form-data; name=\"mapping\"\r\n\r\n{json.dumps(mapping)}\r\n".encode()
    fpart = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"leads.csv\"\r\n"
             "Content-Type: text/csv\r\n\r\n").encode() + csv_text.encode() + b"\r\n"
    body = fields + fpart + f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(BASE + path, method="POST", data=body)
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            status, payload = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, payload = e.code, e.read()
    try:
        payload = json.loads(payload) if payload else None
    except Exception:
        payload = None
    if expect is not None:
        check(f"POST {path} (csv) -> {status} (expected {expect})", status == expect,
              f"body={payload}")
    return status, payload


V1 = ("ROLE\nTest RPI agent v1.\nFLOW\nGreet and verify.\n"
      "DISPOSITIONS\n- PINNED_ALPHA: first pinned bucket\n"
      "- PINNED_BETA: second pinned bucket\nCLOSINGS\nGoodbye.\n")
V2 = ("ROLE\nTest RPI agent v2.\nFLOW\nGreet and verify.\n"
      "DISPOSITIONS\n- PINNED_GAMMA: v2 bucket\nCLOSINGS\nGoodbye.\n")
V3 = ("ROLE\nTest RPI agent v3.\nFLOW\nGreet and verify.\n"
      "DISPOSITIONS\n- PINNED_EPSILON: v3 bucket\nCLOSINGS\nGoodbye.\n")

print(f"C2 catalog + adoption tests against {BASE}")
_, login = call("POST", "/api/auth/login",
                {"email": "demo@qgbs.example", "password": "demo-password"}, expect=200)
TOKEN = login["access_token"]

# C5: catalog writes + agent PUT are Super Admin only.
_, super_login = call("POST", "/api/auth/login",
                     {"email": os.environ.get("SUPER_ADMIN_EMAIL", "super@platform.example"),
                      "password": os.environ.get("SUPER_ADMIN_PASSWORD", "super-password")})
SUPER = super_login["access_token"]

print("── catalog starts empty ────────────────────────")
_, cat = call("GET", "/api/catalog/agents", token=TOKEN, expect=200)
check("catalog empty on fresh DB", cat == [], f"got={cat}")

print("── publish v1 + v2 (immutable) ─────────────────")
_, ca = call("POST", "/api/catalog/agents", {
    "slug": "test-rpi", "name": "Test RPI", "stage": "RPI",
    "purpose": "C2 test catalog agent",
}, **{"token": SUPER}, expect=201)
check("catalog slug test-rpi", ca["slug"] == "test-rpi", f"got={ca}")
_, v1 = call("POST", "/api/catalog/agents/test-rpi/versions", {
    "prompt": V1, "note": "v1", "suitable_for": "managed",
    "model": "gpt-4o", "validated_on": "Vapi managed",
}, **{"token": SUPER}, expect=201)
check("v1 published as version 1", v1["version"] == 1, f"got={v1}")
_, v2 = call("POST", "/api/catalog/agents/test-rpi/versions", {
    "prompt": V2, "note": "v2", "suitable_for": "self_hosted",
    "model": "sarvam-105b", "validated_on": "",
}, **{"token": SUPER}, expect=201)
check("v2 published as version 2", v2["version"] == 2, f"got={v2}")
_, ca = call("GET", "/api/catalog/agents/test-rpi", token=TOKEN, expect=200)
check("v1 text unchanged after v2 publish",
      ca["versions"][0]["prompt"] == V1, "v1 text moved")
check("metadata shown for every version",
      ca["versions"][0]["suitable_for"] == "managed"
      and ca["versions"][0]["model"] == "gpt-4o"
      and ca["versions"][0]["validated_on"] == "Vapi managed"
      and ca["versions"][1]["suitable_for"] == "self_hosted", f"got={ca['versions']}")
check("stub box: no unproven flag, no fit label on stub stack",
      all(v["unproven_on_stack"] is False and v["fit_label"] == ""
          for v in ca["versions"]), f"got={ca['versions']}")
# No update/delete route exists for versions: the versioned path matches
# no route at all (404), and the collection path — where POST lives —
# rejects PUT with 405. Either way, a published version cannot change.
call("PUT", "/api/catalog/agents/test-rpi/versions/1", {"prompt": "X"}, token=TOKEN, expect=404)
call("DELETE", "/api/catalog/agents/test-rpi/versions/1", token=TOKEN, expect=404)
call("PUT", "/api/catalog/agents/test-rpi/versions", {"prompt": "X"}, token=TOKEN, expect=405)

print("── flag logic unit test (managed / self_hosted) ─")
sys.path.insert(0, "backend")
from types import SimpleNamespace as NS
from app.routers.catalog import _fit
check("managed stack: self_hosted version flagged",
      _fit(NS(suitable_for="self_hosted", validated_on=""), "managed")[0] is True)
check("managed stack: managed version unflagged, best label",
      _fit(NS(suitable_for="managed", validated_on="Vapi managed"), "managed")
      == (False, "best for managed"))
check("managed stack: both version, unvalidated label intended",
      _fit(NS(suitable_for="both", validated_on=""), "managed")
      == (False, "intended for managed"))
check("self_hosted stack: managed version flagged",
      _fit(NS(suitable_for="managed", validated_on="Vapi managed"), "self_hosted")[0] is True)

print("── adopt v1 ────────────────────────────────────")
_, ad = call("POST", "/api/catalog/adoptions",
             {"catalog_agent_id": "test-rpi", "version": 1}, token=TOKEN, expect=201)
check("adoption recorded v1", ad["catalog_version"] == 1, f"got={ad}")
agent_id = ad["agent_id"]
_, agents = call("GET", "/api/agents", token=TOKEN, expect=200)
mine = [a for a in agents if a["agent_id"] == agent_id]
check("adopted agent prompt == v1", mine and mine[0]["prompt"] == V1,
      f"got={mine[0]['prompt'][:80] if mine else None}")
_, log = call("GET", "/api/catalog/adoption-log", token=TOKEN, expect=200)
check("adoption log shows test-rpi v1",
      any(e["catalog_agent_id"] == "test-rpi" and e["catalog_version"] == 1 for e in log),
      f"got={log}")
_, ads = call("GET", "/api/catalog/adoptions", token=TOKEN, expect=200)
check("adoptions list shows test-rpi v1",
      any(a["catalog_agent_id"] == "test-rpi" and a["catalog_version"] == 1 for a in ads))
_, ca = call("GET", "/api/catalog/agents/test-rpi", token=TOKEN, expect=200)
check("new-version flag: adopted 1, latest 2",
      ca["adopted_version"] == 1 and ca["latest_version"] == 2
      and ca["new_version_available"] is True, f"got={ca}")

print("── campaign pins v1 for life ───────────────────")
_, camp1 = call("POST", "/api/campaigns", {
    "agent_id": agent_id, "name": "C2 pinned v1", "product": "vehicle_loan",
    "provider_mode": "managed",
    "schedule": {"calling_days": "Every day", "window_start": "00:00", "window_end": "23:59"},
}, token=TOKEN, expect=201)
check("campaign pinned to catalog v1",
      camp1["pinned_catalog_agent"] == "test-rpi"
      and camp1["pinned_catalog_version"] == 1, f"got={camp1}")
call_upload(f"/api/campaigns/{camp1['campaign_id']}/leads/upload",
            "phone_number,name,bank_name,state\n+10000000001,Test User,Test Bank,Uttar Pradesh\n",
            TOKEN, {"phone_number": "phone_number", "name": "name",
                    "bank_name": "bank_name", "state": "state"}, expect=200)
call("POST", f"/api/campaigns/{camp1['campaign_id']}/status",
     {"status": "active"}, token=TOKEN, expect=200)

print("── publish + adopt v3, old campaign unaffected ─")
_, v3 = call("POST", "/api/catalog/agents/test-rpi/versions", {
    "prompt": V3, "note": "v3", "suitable_for": "managed",
    "model": "gpt-4o", "validated_on": "Vapi managed",
}, **{"token": SUPER}, expect=201)
check("v3 published as version 3", v3["version"] == 3, f"got={v3}")
_, ad3 = call("POST", "/api/catalog/adoptions",
              {"catalog_agent_id": "test-rpi", "version": 3}, token=TOKEN, expect=201)
check("adopted v3", ad3["catalog_version"] == 3, f"got={ad3}")
_, camp1_after = call("GET", f"/api/campaigns/{camp1['campaign_id']}", token=TOKEN, expect=200)
check("old campaign still pinned v1 after v3 adoption",
      camp1_after["pinned_catalog_version"] == 1, f"got={camp1_after}")
_, agents = call("GET", "/api/agents", token=TOKEN, expect=200)
mine = [a for a in agents if a["agent_id"] == agent_id]
check("agent live prompt now v3", mine and mine[0]["prompt"] == V3)

print("── stub dispatch runs the PINNED prompt ───────")
_, disp = call("POST", f"/api/campaigns/{camp1['campaign_id']}/dispatch",
               token=TOKEN, expect=200)
_, calls = call("GET", f"/api/campaigns/{camp1['campaign_id']}/calls",
                token=TOKEN, expect=200)
disps = [c["disposition"] for c in calls["calls"]]
check("dispatched call disposition from PINNED v1 buckets",
      disps and disps[0] == "PINNED_ALPHA", f"got={disps}")
_, camp2 = call("POST", "/api/campaigns", {
    "agent_id": agent_id, "name": "C2 pinned v3", "product": "vehicle_loan",
    "provider_mode": "managed",
    "schedule": {"calling_days": "Every day", "window_start": "00:00", "window_end": "23:59"},
}, token=TOKEN, expect=201)
check("new campaign pins v3",
      camp2["pinned_catalog_version"] == 3, f"got={camp2}")
call_upload(f"/api/campaigns/{camp2['campaign_id']}/leads/upload",
            "phone_number,name,bank_name,state\n+10000000002,Test Two,Test Bank,Uttar Pradesh\n",
            TOKEN, {"phone_number": "phone_number", "name": "name",
                    "bank_name": "bank_name", "state": "state"}, expect=200)
call("POST", f"/api/campaigns/{camp2['campaign_id']}/status",
     {"status": "active"}, token=TOKEN, expect=200)
call("POST", f"/api/campaigns/{camp2['campaign_id']}/dispatch",
     token=TOKEN, expect=200)
_, calls2 = call("GET", f"/api/campaigns/{camp2['campaign_id']}/calls",
                 token=TOKEN, expect=200)
disps2 = [c["disposition"] for c in calls2["calls"]]
check("new campaign dispatch from v3 buckets",
      disps2 and disps2[0] == "PINNED_EPSILON", f"got={disps2}")

print("── manual edit diverges from catalog ───────────")
_, agent_now = call("GET", f"/api/agents/{agent_id}", token=TOKEN, expect=200)
edited = dict(agent_now)
edited["prompt"] = V3.replace("PINNED_EPSILON", "PINNED_EPSILON") + "\nEDITED LINE\n"
_, upd = call("PUT", f"/api/agents/{agent_id}", edited, **{"token": SUPER}, expect=200)
_, camp3 = call("POST", "/api/campaigns", {
    "agent_id": agent_id, "name": "C2 diverged", "product": "vehicle_loan",
    "provider_mode": "managed",
    "schedule": {"calling_days": "Every day", "window_start": "00:00", "window_end": "23:59"},
}, token=TOKEN, expect=201)
check("diverged agent: campaign pins no catalog version",
      camp3["pinned_catalog_agent"] is None
      and camp3["pinned_catalog_version"] is None, f"got={camp3}")

print("── cross-box import mode ───────────────────────")
_, ca_imp = call("POST", "/api/catalog/agents", {
    "slug": "test-imp", "name": "Test Imp", "stage": "RPI",
}, **{"token": SUPER}, expect=201)
_, v_imp = call("POST", "/api/catalog/agents/test-imp/versions", {
    "prompt": V2, "note": "imp v1", "suitable_for": "both",
    "model": "gpt-4o", "validated_on": "Vapi managed",
}, **{"token": SUPER}, expect=201)
_, payload = call("GET", "/api/catalog/agents/test-imp/versions/1/export",
                  token=TOKEN, expect=200)
check("export payload carries prompt + version",
      payload["prompt"] == V2 and payload["version"] == 1
      and payload["catalog_agent_id"] == "test-imp", f"got={payload}")
_, ad_imp = call("POST", "/api/catalog/adoptions",
                 {"catalog_agent_id": "test-imp", "payload": payload},
                 token=TOKEN, expect=201)
check("payload adoption lands v1",
      ad_imp["catalog_version"] == 1, f"got={ad_imp}")
_, agents = call("GET", "/api/agents", token=TOKEN, expect=200)
imp = [a for a in agents if a["agent_id"] == ad_imp["agent_id"]]
check("imported agent prompt == payload prompt",
      imp and imp[0]["prompt"] == V2)

print(f"\nC2 RESULT: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
