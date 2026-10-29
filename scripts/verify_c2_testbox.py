#!/usr/bin/env python3
"""Verify C2 on a deployed tenant box: catalog seeded + real adoption.

Usage:  VAP_EMAIL=<box seed email> VAP_PASSWORD=<box seed password>
        python3 scripts/verify_c2_testbox.py [base_url] [seed_json]
Defaults: base_url http://127.0.0.1:8000 (in-container backend),
seed_json /tmp/catalog_seed_v1.json.
Checks: the box's catalog holds all five seed agents; adopting
rpi-verification v1 creates an agent whose prompt is EXACTLY the
seed's live-RPI-v10 text, with the adopted markers set; the
adoption log records it. Credentials come only from the environment.
"""
import json
import os
import sys
import urllib.request
import urllib.error

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
SEED = sys.argv[2] if len(sys.argv) > 2 else "/tmp/catalog_seed_v1.json"
EMAIL = os.environ.get("VAP_EMAIL", "demo@qgbs.example")
PASSWORD = os.environ.get("VAP_PASSWORD")
if not PASSWORD:
    sys.exit("VAP_PASSWORD is required (pass it via docker compose exec -e)")

passed = failed = 0


def check(label, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


def call(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


status, login = call("POST", "/api/auth/login",
                     {"email": EMAIL, "password": PASSWORD})
assert status == 200, f"login failed: {status} {login}"
token = login["access_token"]

seed = json.load(open(SEED))
seed_by_slug = {e["slug"]: e for e in seed}
status, catalog = call("GET", "/api/catalog/agents", token=token)
check("catalog holds all 5 seed agents",
      status == 200 and {a["slug"] for a in catalog} >= set(seed_by_slug),
      f"status={status} slugs={[a.get('slug') for a in catalog] if isinstance(catalog, list) else catalog}")
for entry in seed:
    status, one = call("GET", f"/api/catalog/agents/{entry['slug']}", token=token)
    check(f"{entry['slug']}: v1 metadata managed/gpt-4o/Vapi managed",
          status == 200 and one["versions"] and one["versions"][0]["suitable_for"] == "managed"
          and one["versions"][0]["model"] == "gpt-4o"
          and one["versions"][0]["validated_on"] == "Vapi managed",
          f"status={status}")

status, ad = call("POST", "/api/catalog/adoptions",
                  {"catalog_agent_id": "rpi-verification", "version": 1}, token=token)
check("adopt rpi-verification v1", status == 201, f"status={status} body={ad}")
if status == 201:
    status, agent = call("GET", f"/api/agents/{ad['agent_id']}", token=token)
    expected = seed_by_slug["rpi-verification"]["prompt"]
    check("adopted RPI prompt == live v10 seed text",
          status == 200 and agent["prompt"] == expected,
          f"status={status} len={len(agent.get('prompt', '')) if isinstance(agent, dict) else '?'} expected={len(expected)}")
    check("adopted markers set",
          status == 200 and agent["adopted_catalog_agent"] == "rpi-verification"
          and agent["adopted_catalog_version"] == 1, f"got={agent}")
    status, log = call("GET", "/api/catalog/adoption-log", token=token)
    check("adoption log records rpi-verification v1",
          status == 200 and any(e["catalog_agent_id"] == "rpi-verification"
                                and e["catalog_version"] == 1 for e in log),
          f"status={status}")

print(f"test-box C2 verification: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
