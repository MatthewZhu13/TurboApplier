"""macOS notification + optional phone push via ntfy.sh."""
import subprocess

import httpx

from . import config


def notify(title: str, body: str) -> None:
    subprocess.run(["osascript", "-e", f"display notification {body!r} with title {title!r}"
                    .replace("'", '"')], check=False, capture_output=True)
    if config.NTFY_TOPIC:
        try:
            httpx.post(f"https://ntfy.sh/{config.NTFY_TOPIC}", content=body.encode(), headers={"Title": title}, timeout=10)
        except httpx.HTTPError:
            pass
