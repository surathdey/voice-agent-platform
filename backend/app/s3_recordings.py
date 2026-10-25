"""S3 archival of managed (Vapi) call recordings.

WHY: the provider's recording URL is a PRIVATE object URL — a browser
that opens it directly gets an Authorization error, and the signed
playback URL the proxy re-signs expires. The operator's durable copy
therefore lives in the tenant's own AWS account: when a managed call
finalizes, the backend downloads the recording server-side (fresh
signed URL from the provider, with the tenant's own API key) and
uploads it to a PRIVATE S3 bucket. Playback and export links are
presigned at request time (7 days) — the bucket is never public-read,
because these are customer calls.

CONFIG (environment, never code):
- S3_RECORDINGS_BUCKET — the bucket name. Empty = archival disabled;
  every function below then returns "" / None and the old Vapi-only
  paths keep working untouched.
- S3_RECORDINGS_REGION / AWS_REGION / AWS_DEFAULT_REGION — the bucket's
  region. Default "ca-central-1": the region of the deployment this
  module shipped against (verified via the instance's own metadata);
  override it when the bucket lives elsewhere.
- Credentials come from boto3's default chain (instance IAM role first
  — the intended setup — then environment / config). This module never
  reads, logs, or accepts an AWS key itself.

SAFETY: boto3 is imported LAZILY (same discipline as the export's
lazy openpyxl): a deployment without boto3 installed boots and runs
exactly as before, with archival silently disabled. Archival itself
never raises into the finalize path — a failed download or upload
leaves the session on its Vapi-only recording and returns None.
Secret discipline: the Vapi key passes through locals only and is
never logged, returned, or stored anywhere here.
"""

import json
import logging
import os
import urllib.request

logger = logging.getLogger(__name__)

_PRESIGN_EXPIRY_S = 7 * 24 * 3600  # 7 days, the export link's lifetime
_DEFAULT_REGION = "ca-central-1"


def bucket_name() -> str:
    """Configured bucket, or "" when archival is disabled."""
    return (os.getenv("S3_RECORDINGS_BUCKET") or "").strip()


def region_name() -> str:
    """Bucket region: explicit env first, else the deployment's region."""
    return (
        os.getenv("S3_RECORDINGS_REGION")
        or os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
        or _DEFAULT_REGION
    ).strip()


def object_key(tenant_id: str, session_id: str) -> str:
    """One object per call, tenant-scoped so a key proves its owner."""
    return f"recordings/{tenant_id}/{session_id}.wav"


def _s3_client():
    """A boto3 S3 client, or None when boto3/bucket is unavailable."""
    bucket = bucket_name()
    if not bucket:
        return None
    try:
        import boto3
    except ImportError:
        logger.warning("s3_recordings: boto3 not installed; archival disabled")
        return None
    try:
        return boto3.client("s3", region_name=region_name())
    except Exception as exc:  # credentials/region misconfiguration
        logger.warning("s3_recordings: cannot build S3 client: %s", type(exc).__name__)
        return None


def fetch_vapi_recording_bytes(api_key: str, provider_call_id: str) -> bytes:
    """Download one call's recording from Vapi (fresh signed URL).

    Mirrors the playback proxy's fetch: the stored provider URL is
    private, so the artifact is re-fetched via the call API and its
    short-lived signed URL downloaded server-side. Raises on any
    failure — callers that must not fail (finalize, backfill) catch it.
    """
    req = urllib.request.Request(
        f"https://api.vapi.ai/call/{provider_call_id}",
        headers={"Authorization": f"Bearer {api_key}",
                 "User-Agent": "VoiceAgentPlatform/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        call = json.load(r)
    art = call.get("artifact") or {}
    url = art.get("presignedMonoUrl") or art.get("presignedStereoUrl") or ""
    if not url:
        raise ValueError("provider returned no playable recording URL")
    req2 = urllib.request.Request(url, headers={"User-Agent": "VoiceAgentPlatform/1.0"})
    with urllib.request.urlopen(req2, timeout=60) as resp:
        return resp.read()


def archive_session_recording(db, session) -> str | None:
    """Archive one finalized session's recording to S3. Best-effort.

    Returns the object key on success, None when archival is disabled,
    not applicable (stub / self-hosted / already archived), or failed.
    Never raises. Sets session.recording_s3_key / recording_s3_bucket
    on success; the caller owns the commit (finalize commits its own
    transaction, the backfill script commits per row).
    """
    try:
        if session.provider_mode != "managed":
            return None
        if not session.provider_call_id or session.provider_call_id.startswith("stub_"):
            return None
        if getattr(session, "recording_s3_key", ""):
            return None  # already archived — never upload twice
        bucket = bucket_name()
        if not bucket:
            return None
        from . import providers as prov

        cfg = prov.get_provider_config(session.tenant_id, "managed", db=db)
        api_key = cfg["providers"]["vapi"]["fields"].get("api_key", "")
        if not api_key:
            return None
        data = fetch_vapi_recording_bytes(api_key, session.provider_call_id)
        client = _s3_client()
        if client is None:
            return None
        key = object_key(session.tenant_id, session.session_id)
        client.put_object(Bucket=bucket, Key=key, Body=data,
                          ContentType="audio/wav")
        session.recording_s3_key = key
        session.recording_s3_bucket = bucket
        return key
    except Exception as exc:
        # Archival must never break finalize: the Vapi-only recording
        # remains, and the backfill script can retry this session later.
        logger.warning("s3_recordings: archival failed for session %s: %s",
                       getattr(session, "session_id", "?"), type(exc).__name__)
        return None


def presigned_playback_url(bucket: str, key: str) -> str:
    """A 7-day presigned GET URL for one archived recording, or "".

    Generated at request time (export) — never stored, so an exported
    link is always fresh. Empty when boto3/credentials are unavailable.
    """
    if not bucket or not key:
        return ""
    client = _s3_client()
    if client is None:
        return ""
    try:
        return client.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key},
            ExpiresIn=_PRESIGN_EXPIRY_S)
    except Exception as exc:
        logger.warning("s3_recordings: presign failed: %s", type(exc).__name__)
        return ""


def stream_s3_recording(bucket: str, key: str):
    """Yield one archived recording's bytes in chunks (proxy playback).

    Raises on failure — the proxy falls back to the Vapi path then.
    """
    client = _s3_client()
    if client is None:
        raise RuntimeError("S3 unavailable")
    body = client.get_object(Bucket=bucket, Key=key)["Body"]
    try:
        while True:
            chunk = body.read(64 * 1024)
            if not chunk:
                break
            yield chunk
    finally:
        body.close()
