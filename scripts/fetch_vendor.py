"""Download the two front-end vendor bundles into app/web/static/vendor.

The app deliberately serves ECharts and HTMX locally: CDN access is unreliable
on the target network, and a blank chart is worse than a slightly larger repo.

Run:
    .venv\\python.exe scripts\\fetch_vendor.py
"""

from __future__ import annotations

from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "app" / "web" / "static" / "vendor"

TARGETS = {
    "echarts.min.js": [
        "https://cdnjs.cloudflare.com/ajax/libs/echarts/5.5.1/echarts.min.js",
        "https://registry.npmmirror.com/echarts/5.5.1/files/dist/echarts.min.js",
        "https://unpkg.com/echarts@5.5.1/dist/echarts.min.js",
        "https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js",
    ],
    "htmx.min.js": [
        "https://cdnjs.cloudflare.com/ajax/libs/htmx/1.9.12/htmx.min.js",
        "https://registry.npmmirror.com/htmx.org/1.9.12/files/dist/htmx.min.js",
        "https://unpkg.com/htmx.org@1.9.12/dist/htmx.min.js",
        "https://cdn.jsdelivr.net/npm/htmx.org@1.9.12/dist/htmx.min.js",
    ],
}

MIN_BYTES = 20_000


def fetch(name: str, urls: list[str]) -> bool:
    target = VENDOR / name
    if target.exists() and target.stat().st_size >= MIN_BYTES:
        print(f"  {name}: already present ({target.stat().st_size:,} bytes)")
        return True

    for url in urls:
        try:
            response = httpx.get(url, timeout=60, follow_redirects=True)
        except Exception as exc:  # noqa: BLE001 - network errors are expected here
            print(f"  {name}: {url} -> {type(exc).__name__}")
            continue
        if response.status_code == 200 and len(response.content) >= MIN_BYTES:
            target.write_bytes(response.content)
            print(f"  {name}: {url} -> {len(response.content):,} bytes")
            return True
        print(f"  {name}: {url} -> HTTP {response.status_code}, {len(response.content)} bytes")
    return False


def main() -> int:
    VENDOR.mkdir(parents=True, exist_ok=True)
    print(f"Vendor directory: {VENDOR}")
    results = {name: fetch(name, urls) for name, urls in TARGETS.items()}
    missing = [name for name, ok in results.items() if not ok]
    if missing:
        print(f"FAILED: {', '.join(missing)}")
        return 1
    print("All vendor bundles ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
