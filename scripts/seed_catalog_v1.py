#!/usr/bin/env python3
"""Seed the platform catalog with v1 (Epic C, C2 step 6).

Reads scripts/catalog_seed_v1.json (the five live agents' current
prompts + structured config, exported from the live DB 2026-10-29)
and publishes each as catalog version 1 on the target box.

Usage:  VAP_EMAIL=<box seed email> VAP_PASSWORD=<box seed password>
        python3 scripts/seed_catalog_v1.py [base_url] [seed_json]
Defaults: base_url http://127.0.0.1:8000 (in-container backend),
seed_json scripts/catalog_seed_v1.json next to this script,
VAP_EMAIL demo@qgbs.example. The credentials come ONLY from the
environment (passed at `docker compose exec` time, never written
into this script or any file).

Idempotent: an agent slug that already exists is left alone, and a
catalog agent that already has a published version is not re-published
— re-running the seed after a box rebuild is safe.
"""
import json
import os
import sys
import urllib.request
import urllib.error

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
SEED = (sys.argv[2] if len(sys.argv) > 2
        else os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "catalog_seed_v1.json"))
EMAIL = os.environ.get("VAP_EMAIL", "demo@qgbs.example")
PASSWORD = os.environ.get("VAP_PASSWORD")
if not PASSWORD:
    sys.exit("VAP_PASSWORD is required (pass it via docker compose exec -e)")


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
created = published = skipped = 0
for entry in seed:
    slug = entry["slug"]
    status, existing = call("GET", f"/api/catalog/agents/{slug}", token=token)
    if status == 200:
        if existing.get("versions"):
            print(f"  skip {slug}: already has {len(existing['versions'])} version(s)")
            skipped += 1
            continue
        print(f"  {slug}: agent exists, publishing v1")
    else:
        body = {k: entry[k] for k in (
            "slug", "name", "stage", "purpose", "llm_model", "stt_provider",
            "stt_language", "tts_provider", "tts_voice_id", "voice_gender",
            "language_rule", "captured_fields", "max_duration_s", "max_turns",
            "silence_prompt_after_s", "silence_max_prompts")}
        status, created_agent = call("POST", "/api/catalog/agents", body, token=token)
        assert status == 201, f"create {slug} failed: {status} {created_agent}"
        created += 1
    status, version = call("POST", f"/api/catalog/agents/{slug}/versions", {
        "prompt": entry["prompt"], "note": entry["note"],
        "suitable_for": entry["suitable_for"], "model": entry["model"],
        "validated_on": entry["validated_on"],
    }, token=token)
    assert status == 201, f"publish {slug} failed: {status} {version}"
    print(f"  publish {slug} v{version['version']} (live v{entry['live_version']})")
    published += 1

print(f"seed done: {created} agents created, {published} versions published, "
      f"{skipped} already seeded")
