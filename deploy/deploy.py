"""Deploy to an Akamai (Linode) Ubuntu 24.04 host. Works from PowerShell, cmd, or bash:

    python deploy/deploy.py root@HOST

Ships `git archive HEAD`, so uncommitted work never goes out. .env is copied
separately with owner-only permissions. Needs git and the OpenSSH client
(built into Windows 10/11).
"""

import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REMOTE = "/opt/advisor-audit"


def run(*argv: str) -> None:
    print("+", " ".join(argv[:2]), "...")
    subprocess.run(argv, check=True)


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: python deploy/deploy.py user@host")
    host = sys.argv[1]

    env = ROOT / ".env"
    if not env.is_file():
        sys.exit("Missing .env (see .env.example)")
    domain = next((l.split("=", 1)[1].strip() for l in env.read_text(encoding="utf-8-sig").splitlines()
                   if l.startswith("DOMAIN=")), "")
    if not domain:
        sys.exit("Set DOMAIN in .env")
    if subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout.strip():
        print("Note: you have uncommitted changes. Only committed code is deployed.")

    with tempfile.TemporaryDirectory() as tmp:
        tar = str(Path(tmp) / "advisor-audit.tar")
        run("git", "-C", str(ROOT), "archive", "--format=tar", "-o", tar, "HEAD")
        run("ssh", host, "command -v docker >/dev/null || (apt-get update && apt-get install -y docker.io docker-compose-v2)")
        run("ssh", host, f"mkdir -p {REMOTE}")
        run("scp", "-q", tar, f"{host}:/tmp/advisor-audit.tar")
    run("scp", "-q", str(env), f"{host}:{REMOTE}/.env")
    run("ssh", host,
        f"tar -x -C {REMOTE} -f /tmp/advisor-audit.tar && rm /tmp/advisor-audit.tar"
        f" && sed -i 's/\\r$//' {REMOTE}/.env && chmod 600 {REMOTE}/.env"
        f" && cd {REMOTE}/deploy && docker compose --env-file ../.env up -d --build")

    url = f"https://{domain}/healthz"
    print(f"Waiting for {url} ...")
    for _ in range(40):
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                if r.status == 200:
                    print(f"Up: https://{domain}")
                    return
        except Exception:
            time.sleep(4)
    sys.exit(f"Health check failed. Logs: ssh {host} \"cd {REMOTE}/deploy && docker compose logs\"")


if __name__ == "__main__":
    main()
