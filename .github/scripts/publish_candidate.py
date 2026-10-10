#!/usr/bin/env python3
"""GitHub-only candidate publication, consumed by release.yml and source-side tests."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


class ReleaseError(RuntimeError):
    pass


def api(method: str, path: str, payload: dict | None = None, *, missing_ok: bool = False):
    request = urllib.request.Request(
        f"https://api.github.com/repos/{os.environ['GH_REPO']}/{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {os.environ['GH_TOKEN']}",
                 "Accept": "application/vnd.github+json", "User-Agent": "longtask-candidate-release",
                 "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            record = json.load(response)
            if record is None:
                raise ReleaseError(f"GitHub {method} {path} returned an invalid null record")
            return record
    except urllib.error.HTTPError as error:
        try:
            if error.code == 404 and missing_ok:
                return None
            raise ReleaseError(f"GitHub {method} {path} failed with HTTP {error.code}") from error
        finally:
            error.close()
    except (urllib.error.URLError, ValueError) as error:
        raise ReleaseError(f"GitHub {method} {path} did not return usable data") from error


def identity(root: Path) -> tuple[str, str, str]:
    version = json.loads((root / "release-manifest.json").read_text())["version"]
    if not isinstance(version, str) or not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
        raise ReleaseError("release-manifest.json needs a valid X.Y.Z version")
    commit = os.environ["GITHUB_SHA"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ReleaseError("GITHUB_SHA must bind the checked source commit")
    ref = os.environ["GITHUB_REF"]
    tag = f"v{version}"
    if ref not in {"refs/heads/main", f"refs/tags/{tag}"}:
        raise ReleaseError("Publication must run on main or the matching version tag")
    return version, tag, commit


def already_published(tag: str) -> bool:
    release = api("GET", f"releases/tags/{tag}", missing_ok=True)
    if release is None:
        return False
    if not isinstance(release, dict) or release.get("tag_name") != tag or type(release.get("draft")) is not bool:
        raise ReleaseError("GitHub returned an invalid release record")
    if release["draft"]:
        raise ReleaseError("An incomplete draft exists; inspect and remove it before retrying")
    return True


def listed_release_exists(tag: str) -> bool:
    """Called by the contents:write publication job, which can see drafts."""
    page = 1
    while True:
        records = api("GET", f"releases?per_page=100&page={page}")
        if not isinstance(records, list) or len(records) > 100:
            raise ReleaseError("GitHub returned an invalid release listing")
        for record in records:
            if (not isinstance(record, dict) or not isinstance(record.get("tag_name"), str)
                    or type(record.get("draft")) is not bool):
                raise ReleaseError("GitHub returned an invalid release record")
            if record["tag_name"] == tag:
                if record["draft"]:
                    raise ReleaseError("An incomplete draft exists; inspect and remove it before retrying")
                return True
        if len(records) < 100:
            return False
        page += 1


def tag_commit(tag: str) -> str | None:
    # Commit lookup returns 422 for a missing ref and can resolve branch names.
    # Only an exact tag reference returning 404 establishes that the tag is absent.
    record = api("GET", f"git/ref/tags/{tag}", missing_ok=True)
    if record is None:
        return None
    if not isinstance(record, dict) or record.get("ref") != f"refs/tags/{tag}":
        raise ReleaseError("GitHub returned an invalid tag reference")
    seen = set()
    while True:
        target = record.get("object")
        sha = target.get("sha") if isinstance(target, dict) else None
        kind = target.get("type") if isinstance(target, dict) else None
        if (not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha)
                or not isinstance(kind, str) or kind not in {"commit", "tag"}):
            raise ReleaseError("GitHub returned an invalid tag target")
        if kind == "commit":
            return sha
        if sha in seen or len(seen) >= 8:
            raise ReleaseError("GitHub returned a cyclic or excessive annotated tag chain")
        seen.add(sha)
        # A missing referenced object is corruption, not an absent version tag.
        record = api("GET", f"git/tags/{sha}")
        if not isinstance(record, dict) or record.get("sha") != sha:
            raise ReleaseError("GitHub returned an invalid annotated tag object")


def require_matching_tag(tag: str, commit: str) -> bool:
    observed = tag_commit(tag)
    if observed is not None and observed != commit:
        raise ReleaseError("Existing version tag targets a different commit; it will not be moved")
    return observed is not None


def plan(root: Path) -> None:
    version, tag, commit = identity(root)
    publish = not already_published(tag)
    if publish:
        require_matching_tag(tag, commit)
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"publish={str(publish).lower()}\nversion={version}\ntag={tag}\n")
    print(f"{tag}: {'candidate required' if publish else 'already published; skip'}")


def publish(root: Path) -> None:
    version, tag, commit = identity(root)
    if already_published(tag) or listed_release_exists(tag):
        print(f"{tag}: already published; skip")
        return
    archive = root / "dist" / f"longtask-{version}.zip"
    checksum = root / "dist" / f"longtask-{version}.sha256"
    notes = root / "dist" / "release-notes.txt"
    for path in (archive, checksum, notes):
        if not path.is_file() or not path.stat().st_size:
            raise ReleaseError(f"Missing verified publication input: {path.name}")
    if not require_matching_tag(tag, commit):
        api("POST", "git/refs", {"ref": f"refs/tags/{tag}", "sha": commit})
    if not require_matching_tag(tag, commit):
        raise ReleaseError("Version tag is unavailable after creation")
    subprocess.run([
        "gh", "release", "create", tag, str(archive), str(checksum),
        "--verify-tag", "--prerelease", "--latest=false",
        "--title", f"longtask {version} (candidate)", "--notes-file", str(notes),
    ], check=True, timeout=120)


def main() -> int:
    try:
        if len(sys.argv) != 2 or sys.argv[1] not in {"plan", "publish"}:
            raise ReleaseError("Usage: publish_candidate.py plan|publish")
        {"plan": plan, "publish": publish}[sys.argv[1]](Path.cwd())
    except (ReleaseError, KeyError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Candidate release failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
