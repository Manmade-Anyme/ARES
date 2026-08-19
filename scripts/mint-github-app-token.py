#!/usr/bin/env python3
"""Mint a short-lived GitHub App installation token (manmade-agents).

Signs a JWT with the App's private key and exchanges it for a 1-hour
installation access token. Tokens are minted per task, scoped to a single
repository when requested, and are never persisted beyond the invoking run.

The private key is referenced by path only — this script never prints or
stores it. The minted token is written to a mode-600 temp file that is
removed on process exit (unless --no-save).

Usage:
  mint-github-app-token.py [--repo OWNER/REPO] [--output token|json|env] [--no-save]

Examples:
  python3 scripts/mint-github-app-token.py
  python3 scripts/mint-github-app-token.py --repo Manmade-Anyme/ARES --output env
  GH_TOKEN=$(python3 scripts/mint-github-app-token.py --repo Manmade-Anyme/ARES --output token)

Environment:
  GITHUB_APP_ID            App id  (default: 4645217)
  GITHUB_INSTALLATION_ID   Installation id (default: 154840801)
  GITHUB_APP_KEY_PATH      Path to the App's PEM private key
                           (default: ~/.multica/secrets/manmade-agents.pem)
"""

import argparse
import atexit
import base64
import json
import os
import sys
import tempfile
import time
import urllib.request

import cryptography
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

APP_ID = int(os.environ.get("GITHUB_APP_ID", "4645217"))
INSTALLATION_ID = int(os.environ.get("GITHUB_INSTALLATION_ID", "154840801"))
KEY_PATH = os.environ.get(
    "GITHUB_APP_KEY_PATH",
    os.path.expanduser("~/.multica/secrets/manmade-agents.pem"),
)
API = "https://api.github.com"
UA = "manmade-agents"


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def load_key():
    if not os.path.exists(KEY_PATH):
        raise SystemExit(f"private key not found: {KEY_PATH}")
    with open(KEY_PATH, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    if not isinstance(key, cryptography.hazmat.primitives.asymmetric.rsa.RSAPrivateKey):
        raise SystemExit("private key is not an RSA key")
    return key


def make_jwt(key):
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {"iat": now - 60, "exp": now + 540, "iss": APP_ID}
    msg = b64url(json.dumps(header).encode()) + "." + b64url(json.dumps(payload).encode())
    sig = key.sign(msg.encode(), padding.PKCS1v15(), hashes.SHA256())
    return msg + "." + b64url(sig)


def api(path, *, jwt=None, token=None, method="GET", body=None):
    req = urllib.request.Request(API + path, method=method)
    if jwt:
        req.add_header("Authorization", f"Bearer {jwt}")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", UA)
    if body is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(body).encode()
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def mint_token(key, repo_ids=None):
    jwt = make_jwt(key)
    body = None
    if repo_ids:
        body = {"repository_ids": repo_ids}
    return api(
        f"/app/installations/{INSTALLATION_ID}/access_tokens",
        jwt=jwt,
        method="POST",
        body=body,
    )


def resolve_repo_id(key, repo):
    owner, _, name = repo.partition("/")
    if not owner or not name:
        raise SystemExit(f"--repo must be OWNER/REPO, got: {repo}")
    first = mint_token(key)
    info = api(f"/repos/{repo}", token=first["token"])
    if info.get("full_name", "").lower() != repo.lower():
        raise SystemExit(f"repo {repo} is not in installation {INSTALLATION_ID}")
    return info["id"]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", help="narrow token to one repo, OWNER/REPO")
    ap.add_argument("--output", choices=["token", "json", "env"], default="json")
    ap.add_argument("--no-save", action="store_true", help="do not write token to temp file")
    args = ap.parse_args()

    key = load_key()
    repo_ids = None
    if args.repo:
        repo_ids = [resolve_repo_id(key, args.repo)]

    data = mint_token(key, repo_ids)
    token = data["token"]
    expires_at = data["expires_at"]
    narrowed = [r["full_name"] for r in data.get("repositories", [])]

    token_path = None
    if not args.no_save:
        fd, token_path = tempfile.mkstemp(prefix="gh-app-token-", suffix=".tmp")
        os.write(fd, token.encode())
        os.close(fd)
        os.chmod(token_path, 0o600)
        atexit.register(lambda p=token_path: os.path.exists(p) and os.remove(p))

    if args.output == "token":
        sys.stdout.write(token)
        sys.stdout.write("\n")
    elif args.output == "env":
        sys.stdout.write(f"export GH_TOKEN={token}\n")
        if token_path:
            sys.stdout.write(f"export GH_TOKEN_FILE={token_path}\n")
    else:
        out = {
            "token": token,
            "expires_at": expires_at,
            "permissions": data.get("permissions"),
            "repositories": narrowed,
            "token_file": token_path,
        }
        sys.stdout.write(json.dumps(out) + "\n")

    print(
        f"minted installation token (expires {expires_at}, "
        f"repos={narrowed if narrowed else 'all installed'}, "
        f"file={token_path or 'not saved'})",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
