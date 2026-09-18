#!/usr/bin/env python3
"""
migrate_gar_to_ecr.py

Migrates Docker images from a Google Artifact Registry (GAR) Docker
repository to AWS ECR, preserving each service (package) name as the ECR
repository name and preserving every tag exactly as it exists in GAR.

Designed to run from GCP Cloud Shell (or any shell with gcloud, aws, and
skopeo installed). See the accompanying setup notes for prerequisites and
required IAM permissions.

Usage:
    python3 migrate_gar_to_ecr.py --dry-run
    python3 migrate_gar_to_ecr.py --yes
    python3 migrate_gar_to_ecr.py --yes --services api-wrapper-service,other-service
    python3 migrate_gar_to_ecr.py --yes --tag-prefix 2026-09
    python3 migrate_gar_to_ecr.py --dry-run --limit 5
    python3 migrate_gar_to_ecr.py --yes --dedupe-tags --workers 12

--dedupe-tags: when several tags share the same image digest (e.g. a
dev/qa/stage promotion pattern - one build, multiple tags), copy that
digest's blob data from GAR exactly once, then add the remaining tags as
fast registry-side operations on the ECR side (no re-transfer of image
data). Off by default, matching the original per-tag-independent behavior.
"""

import argparse
import hashlib
import json
import logging
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIG - edit these for your environment, or override with CLI flags.
# ---------------------------------------------------------------------------
CONFIG = {
    "gcp_location": "<your_gcp_region>",
    "gcp_project": "<your-gcp-project-id>",
    "gcp_repo": "<your-gcp-repo>",
    "aws_account_id": "<your-aws-account-id>",          # <-- fill in, e.g. "123456789012"
    "aws_region": "<your-aws-region>",              # <-- fill in, e.g. "ap-south-1"
}

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 15

# Refresh auth tokens periodically during a long run, well before they expire.
# I'm reasonably but not fully confident in these exact TTLs (GCP access
# tokens ~1h, ECR auth tokens ~12h) - they're refreshed proactively and well
# before the stated expiry specifically so a slightly-wrong assumption here
# doesn't break a multi-hour migration. If you see 401s mid-run, shorten
# these constants and re-run (already-migrated tags will be skipped).
GCP_TOKEN_TTL_SECONDS = 45 * 60
AWS_TOKEN_TTL_SECONDS = 10 * 60 * 60

LOG_DIR = Path("logs")

# skopeo (and other rootless container tools) can default to a credentials
# path under /run/containers, which requires root and fails with
# "mkdir /run/containers: permission denied" in environments like Cloud
# Shell. Pointing it at an explicit, user-writable authfile sidesteps that
# auto-detection entirely.
SKOPEO_AUTHFILE = Path.home() / ".skopeo_auth_gar_ecr.json"

log = None  # set in main()


# ---------------------------------------------------------------------------
def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOG_DIR / f"migration_{ts}.log"

    logger = logging.getLogger("migrate")
    logger.setLevel(logging.DEBUG)

    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

    logger.addHandler(fh)
    logger.addHandler(ch)
    logger.info(f"Logging to {log_file}")
    return logger


def run(cmd, check=True, capture=False, input_str=None):
    """Run a shell command (as a list), logging it. Returns CompletedProcess."""
    log.debug(f"RUN: {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        check=False,
        capture_output=capture,
        text=True,
        input=input_str,
    )
    if check and result.returncode != 0:
        stderr = result.stderr if capture else "(not captured)"
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(cmd)}\n{stderr}")
    return result


def check_prereqs():
    missing = [t for t in ("gcloud", "aws", "skopeo") if shutil.which(t) is None]
    if missing:
        log.error(
            f"Missing required tools: {', '.join(missing)}. "
            f"Install them first (see setup notes) before running this script."
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
class AuthManager:
    """Logs skopeo into GCP Artifact Registry and AWS ECR, and refreshes
    those logins periodically so a long-running migration doesn't fail on
    an expired token partway through."""

    def __init__(self, gcp_host, aws_host, aws_region):
        self.gcp_host = gcp_host
        self.aws_host = aws_host
        self.aws_region = aws_region
        self._gcp_last_login = 0
        self._aws_last_login = 0
        # Guards refreshes when multiple worker threads call ensure_all()
        # concurrently, so two threads don't both try to re-login at once.
        self._lock = threading.Lock()

    def ensure_gcp_login(self):
        with self._lock:
            if time.time() - self._gcp_last_login < GCP_TOKEN_TTL_SECONDS:
                return
            log.info("Refreshing GCP auth for skopeo...")
            token = run(["gcloud", "auth", "print-access-token"], capture=True).stdout.strip()
            run(
                ["skopeo", "login", "--authfile", str(SKOPEO_AUTHFILE),
                 "-u", "oauth2accesstoken", "--password-stdin", self.gcp_host],
                capture=True,
                input_str=token,
            )
            self._gcp_last_login = time.time()

    def ensure_aws_login(self):
        with self._lock:
            if time.time() - self._aws_last_login < AWS_TOKEN_TTL_SECONDS:
                return
            log.info("Refreshing AWS ECR auth for skopeo...")
            password = run(
                ["aws", "ecr", "get-login-password", "--region", self.aws_region],
                capture=True,
            ).stdout.strip()
            run(
                ["skopeo", "login", "--authfile", str(SKOPEO_AUTHFILE),
                 "-u", "AWS", "--password-stdin", self.aws_host],
                capture=True,
                input_str=password,
            )
            self._aws_last_login = time.time()

    def ensure_all(self):
        self.ensure_gcp_login()
        self.ensure_aws_login()


# ---------------------------------------------------------------------------
# GCP side: discover images/tags
# ---------------------------------------------------------------------------
def list_gcp_images(cfg):
    """Returns a list of dicts: {"service", "tag", "source_ref", "digest"}
    for every (image, tag) pair in the configured GAR repository. This lists
    at the repository level (not a single service), so every service/package
    folder underneath it is discovered automatically. "digest" is included
    even in the default per-tag mode so --dedupe-tags can group by it
    without a second listing call."""
    repo_path = f"{cfg['gcp_location']}-docker.pkg.dev/{cfg['gcp_project']}/{cfg['gcp_repo']}"
    log.info(f"Listing images in {repo_path} ...")
    result = run(
        ["gcloud", "artifacts", "docker", "images", "list", repo_path,
         "--include-tags", "--format=json"],
        capture=True,
    )
    entries = json.loads(result.stdout)

    items = []
    for entry in entries:
        package = entry.get("package", "")
        service = package.rstrip("/").rsplit("/", 1)[-1]
        digest = entry.get("version", "")
        tags = entry.get("tags", [])
        if isinstance(tags, str):
            tags = [t for t in tags.split(",") if t]
        for tag in tags:
            items.append({
                "service": service,
                "tag": tag,
                "source_ref": f"{package}:{tag}",
                "digest": digest,
            })
    log.info(
        f"Found {len(items)} (image, tag) pairs across "
        f"{len({i['service'] for i in items})} services."
    )
    return items


def group_by_digest(items):
    """Collapses a flat items list into one group per (service, digest),
    each carrying every tag that points at that digest. Used by
    --dedupe-tags: the first tag in each group is copied from GAR in full;
    the rest are added as fast ECR-side retags."""
    grouped = {}
    for it in items:
        key = (it["service"], it["digest"])
        g = grouped.setdefault(key, {
            "service": it["service"],
            "package": it["source_ref"].rsplit(":", 1)[0],
            "tags": [],
        })
        g["tags"].append(it["tag"])
    groups = list(grouped.values())
    for g in groups:
        g["tags"].sort()
    return groups


# ---------------------------------------------------------------------------
# AWS side: repo creation
# ---------------------------------------------------------------------------
def ensure_ecr_repo(service, aws_region, created_cache):
    if service in created_cache:
        return
    check = run(
        ["aws", "ecr", "describe-repositories", "--repository-names", service,
         "--region", aws_region],
        check=False,
        capture=True,
    )
    if check.returncode == 0:
        log.debug(f"ECR repo already exists: {service}")
    else:
        log.info(f"Creating ECR repo: {service}")
        run(
            ["aws", "ecr", "create-repository", "--repository-name", service,
             "--region", aws_region],
            capture=True,
        )
    created_cache.add(service)


# ---------------------------------------------------------------------------
# Digest verification / resumability
# ---------------------------------------------------------------------------
def raw_digest(ref):
    """Returns the sha256 of the raw manifest at `ref` (a docker://... URL),
    or None if the reference doesn't exist / can't be inspected. Uses --raw
    plus a local hash rather than skopeo's own --format Digest, so this
    works identically for plain single-arch manifests and multi-arch
    indexes."""
    result = run(
        ["skopeo", "inspect", "--authfile", str(SKOPEO_AUTHFILE), "--raw", ref],
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        return None
    return hashlib.sha256(result.stdout.encode()).hexdigest()


def already_migrated(source_ref, dest_ref):
    dest_digest = raw_digest(dest_ref)
    if dest_digest is None:
        return False
    source_digest = raw_digest(source_ref)
    return source_digest is not None and source_digest == dest_digest


# ---------------------------------------------------------------------------
# Copy
# ---------------------------------------------------------------------------
def copy_with_retry(source_ref, dest_ref, auth: AuthManager):
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            auth.ensure_all()
            run(
                ["skopeo", "copy", "--authfile", str(SKOPEO_AUTHFILE),
                 "--all", source_ref, dest_ref],
                capture=True,
            )
            return True
        except RuntimeError as e:
            last_err = e
            log.warning(f"Attempt {attempt}/{MAX_RETRIES} failed for {dest_ref}: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    log.error(f"Giving up on {dest_ref} after {MAX_RETRIES} attempts: {last_err}")
    return False


# ---------------------------------------------------------------------------
def main():
    global log

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                         help="List what would be migrated, without copying anything.")
    parser.add_argument("--yes", action="store_true",
                         help="Skip the confirmation prompt and run for real.")
    parser.add_argument("--services", default="",
                         help="Comma-separated list of service names to restrict to.")
    parser.add_argument("--tag-prefix", default="",
                         help="Only migrate tags starting with this prefix.")
    parser.add_argument("--limit", type=int, default=0,
                         help="Stop after this many (image,tag) pairs. 0 = no limit.")
    parser.add_argument("--workers", type=int, default=8,
                         help="Number of images/groups to process concurrently. Start "
                              "moderate (8-16) - very high concurrency can trigger "
                              "registry API rate limiting on either side; raise "
                              "gradually and watch the logs before pushing higher.")
    parser.add_argument("--dedupe-tags", action="store_true",
                         help="When several tags share the same digest, copy the image "
                              "data from GAR once and add the remaining tags as fast "
                              "ECR-side retags instead of re-copying from GAR for each.")
    parser.add_argument("--aws-account-id", default=CONFIG["aws_account_id"])
    parser.add_argument("--aws-region", default=CONFIG["aws_region"])
    parser.add_argument("--gcp-location", default=CONFIG["gcp_location"])
    parser.add_argument("--gcp-project", default=CONFIG["gcp_project"])
    parser.add_argument("--gcp-repo", default=CONFIG["gcp_repo"])
    args = parser.parse_args()

    log = setup_logging()
    check_prereqs()

    cfg = {
        "gcp_location": args.gcp_location,
        "gcp_project": args.gcp_project,
        "gcp_repo": args.gcp_repo,
        "aws_account_id": args.aws_account_id,
        "aws_region": args.aws_region,
    }
    if not cfg["aws_account_id"] or not cfg["aws_region"]:
        log.error(
            "aws_account_id and aws_region must be set (edit CONFIG at the top "
            "of this file, or pass --aws-account-id / --aws-region)."
        )
        sys.exit(1)

    gcp_host = f"{cfg['gcp_location']}-docker.pkg.dev"
    aws_host = f"{cfg['aws_account_id']}.dkr.ecr.{cfg['aws_region']}.amazonaws.com"

    items = list_gcp_images(cfg)

    if args.services:
        wanted = {s.strip() for s in args.services.split(",") if s.strip()}
        items = [i for i in items if i["service"] in wanted]
        log.info(f"Restricted to services {wanted}: {len(items)} pairs remain.")

    if args.tag_prefix:
        items = [i for i in items if i["tag"].startswith(args.tag_prefix)]
        log.info(f"Restricted to tag prefix '{args.tag_prefix}': {len(items)} pairs remain.")

    if args.limit:
        items = items[: args.limit]
        log.info(f"Limited to first {args.limit} pairs.")

    services = sorted({i["service"] for i in items})
    log.info(f"Plan: {len(items)} (image,tag) pairs across {len(services)} services.")
    for s in services:
        log.info(f"  - {s}")

    groups = group_by_digest(items) if args.dedupe_tags else None
    if args.dedupe_tags:
        extra_tag_count = len(items) - len(groups)
        log.info(
            f"Dedupe mode: {len(items)} tags collapse into {len(groups)} unique-digest "
            f"groups ({extra_tag_count} tags will be added as fast ECR-side retags "
            f"instead of re-copied from GAR)."
        )

    if args.dry_run:
        log.info("Dry run only - no repos created, nothing copied.")
        if args.dedupe_tags:
            for g in groups:
                primary, *extras = g["tags"]
                log.info(f"  WOULD COPY  {g['package']}:{primary}  ->  "
                          f"{aws_host}/{g['service']}:{primary}")
                for t in extras:
                    log.info(f"  WOULD RETAG (ECR-side)  {aws_host}/{g['service']}:{primary}  "
                              f"->  {aws_host}/{g['service']}:{t}")
        else:
            for i in items:
                dest_ref = f"docker://{aws_host}/{i['service']}:{i['tag']}"
                log.info(f"  WOULD COPY  {i['source_ref']}  ->  {dest_ref}")
        return

    if not args.yes:
        resp = input(
            f"About to migrate {len(items)} (image,tag) pairs across "
            f"{len(services)} services to {aws_host}. Continue? [y/N] "
        )
        if resp.strip().lower() != "y":
            log.info("Aborted by user.")
            return

    auth = AuthManager(gcp_host, aws_host, cfg["aws_region"])
    auth.ensure_all()

    # Repos are all created up front, sequentially, before any parallel
    # copying starts - this avoids two worker threads racing to create the
    # same not-yet-existing repo at once.
    created_cache = set()
    for s in services:
        ensure_ecr_repo(s, cfg["aws_region"], created_cache)

    progress_lock = threading.Lock()
    progress = {"done": 0}

    def log_progress(service, tag, status):
        with progress_lock:
            progress["done"] += 1
            log.info(f"[{progress['done']}] {service}:{tag} -> {status}")

    def process_item(item):
        """Copies one (service, tag) pair straight from GAR. Returns a list
        of (status, {"service", "tag"}) so it shares a result shape with
        process_group below."""
        source_ref = f"docker://{item['source_ref']}"
        dest_ref = f"docker://{aws_host}/{item['service']}:{item['tag']}"

        auth.ensure_all()

        if already_migrated(source_ref, dest_ref):
            status = "skipped"
        else:
            ok = copy_with_retry(source_ref, dest_ref, auth)
            if not ok:
                status = "failed"
            elif already_migrated(source_ref, dest_ref):
                status = "succeeded"
            else:
                log.error(f"  Copied but digest mismatch on verification: "
                          f"{item['service']}:{item['tag']}")
                status = "failed"

        log_progress(item["service"], item["tag"], status)
        return [(status, {"service": item["service"], "tag": item["tag"]})]

    def process_group(group):
        """Copies a group's primary tag straight from GAR, then adds every
        other tag sharing that digest as an ECR-side retag (source and dest
        both point at the ECR repo - only the manifest moves, since ECR
        already has every blob from the primary copy)."""
        service = group["service"]
        package = group["package"]
        primary_tag, *extra_tags = group["tags"]

        primary_item = {"service": service, "tag": primary_tag,
                         "source_ref": f"{package}:{primary_tag}"}
        results = process_item(primary_item)

        if results[0][0] == "failed":
            for t in extra_tags:
                log_progress(service, t, "failed (primary copy failed)")
                results.append(("failed", {"service": service, "tag": t}))
            return results

        primary_ecr_ref = f"docker://{aws_host}/{service}:{primary_tag}"
        for t in extra_tags:
            extra_dest_ref = f"docker://{aws_host}/{service}:{t}"
            auth.ensure_all()

            if already_migrated(primary_ecr_ref, extra_dest_ref):
                status = "skipped"
            else:
                ok = copy_with_retry(primary_ecr_ref, extra_dest_ref, auth)
                if ok and already_migrated(primary_ecr_ref, extra_dest_ref):
                    status = "succeeded"
                else:
                    status = "failed"

            log_progress(service, t, f"{status} (ECR-side retag)")
            results.append((status, {"service": service, "tag": t}))

        return results

    succeeded, skipped, failed = [], [], []
    bucket = {"succeeded": succeeded, "skipped": skipped, "failed": failed}

    work = groups if args.dedupe_tags else items
    worker_fn = process_group if args.dedupe_tags else process_item

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(worker_fn, w) for w in work]
        for future in as_completed(futures):
            for status, label in future.result():
                bucket[status].append(label)

    log.info("=" * 60)
    log.info(
        f"Done. Succeeded: {len(succeeded)}  "
        f"Skipped (already migrated): {len(skipped)}  Failed: {len(failed)}"
    )
    if failed:
        log.error("Failed (image,tag) pairs:")
        for i in failed:
            log.error(f"  {i['service']}:{i['tag']}")
        sys.exit(1)


if __name__ == "__main__":
    main()
