#!/usr/bin/env python3
"""Record visible proof of an installed Superboard on a clean machine.

The product still runs without Playwright. CI installs it only for this evidence
run, starts the already-installed wheel in a new workspace, drives the real UI,
and keeps screenshots, a WebM recording, and a small machine-readable report.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from playwright.sync_api import Page, sync_playwright


PROMPT = "Draft a three-step launch checklist for my side project"


def screenshot(page: Page, output: Path, name: str) -> None:
    page.screenshot(path=output / name, full_page=True)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_server(url: str, server: subprocess.Popen[str]) -> None:
    """Keep server startup out of the recording instead of filming a blank tab."""
    for _ in range(30):
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except URLError:
            if server.poll() is not None:
                raise RuntimeError(f"Superboard exited early with {server.returncode}")
            time.sleep(1)
    raise RuntimeError("Superboard did not become reachable within 30 seconds")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    port = args.port or free_port()

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="superboard-visible-e2e-"))
    log_path = output / "superboard.log"
    clean_path = os.pathsep.join(
        [str(Path(sys.executable).parent), "/usr/bin", "/bin"]
    )
    env = {**os.environ, "PATH": clean_path}
    if shutil.which("claude", path=clean_path):
        raise RuntimeError("Visible E2E requires a clean PATH without the Claude CLI")

    with log_path.open("w", encoding="utf-8") as log:
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "superboard",
                "--port",
                str(port),
                str(workspace),
            ],
            cwd=workspace,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )

    url = f"http://127.0.0.1:{port}"
    errors: list[str] = []
    video_path: Path | None = None
    try:
        wait_for_server(url, server)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1440, "height": 1000},
                record_video_dir=output / "video",
                record_video_size={"width": 1440, "height": 1000},
            )
            page = context.new_page()
            page.on(
                "console",
                lambda msg: errors.append(f"console: {msg.text}")
                if msg.type == "error"
                else None,
            )
            page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))

            response = page.goto(url, wait_until="domcontentloaded")
            if response is None or response.status != 200:
                raise RuntimeError("Superboard root did not return HTTP 200")

            capture = page.locator("#captureinput")
            capture.wait_for(state="attached", timeout=10_000)
            screenshot(page, output, "01-first-start.png")
            page.wait_for_timeout(1_500)

            page.locator('[data-view="todos"]').click()
            capture.wait_for(state="visible")
            page.wait_for_timeout(700)
            capture.press_sequentially(PROMPT, delay=25)
            screenshot(page, output, "02-task-ready.png")
            page.wait_for_timeout(1_500)
            with page.expect_response(
                lambda response: response.url.endswith("/api/quick-capture")
            ) as response_info:
                page.locator("#capturebtn").click()
            response = response_info.value
            if response.status != 202:
                raise RuntimeError(
                    f"Quick capture returned HTTP {response.status}: {response.text()}"
                )
            task_id = response.json()["id"]

            page.locator('[data-view="todos"]').click()
            task = page.locator(f'.item[data-id="{task_id}"]')
            task.wait_for()
            screenshot(page, output, "03-task-created.png")
            page.wait_for_timeout(2_000)

            # The clean runner intentionally has no Claude CLI. The product must turn
            # that into a readable thread reply rather than a traceback or silent hang.
            for _ in range(30):
                page.reload(wait_until="domcontentloaded")
                page.locator('[data-view="todos"]').click()
                task = page.locator(f'.item[data-id="{task_id}"]')
                task.wait_for()
                task.locator('.pill[role="button"]').click()
                if "Claude binary not found (claude)" in page.locator(".gc-overlay").inner_text():
                    break
                page.keyboard.press("Escape")
                time.sleep(1)
            else:
                raise RuntimeError("Missing-Claude explanation did not appear in the task thread")
            screenshot(page, output, "04-missing-cli-explained.png")
            page.wait_for_timeout(4_000)

            board_file = workspace / "inbox" / "board.md"
            if not board_file.is_file():
                raise RuntimeError("First start did not create inbox/board.md")
            if errors:
                raise RuntimeError("Browser errors: " + " | ".join(errors))

            video = page.video
            context.close()
            browser.close()
            video_path = Path(video.path()) if video else None

        report = {
            "result": "passed",
            "scope": [
                "installed wheel starts in an empty workspace",
                "first-start UI renders without browser errors",
                "quick capture creates a real task and starts the runner path",
                "missing Claude CLI becomes a readable thread reply",
            ],
            "not_proven": "A successful Claude agent response; CI receives no personal Claude credentials.",
            "machine": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "architecture": platform.machine(),
            },
            "task_id": task_id,
            "prompt": PROMPT,
            "board_sha256": hashlib.sha256(board_file.read_bytes()).hexdigest(),
            "video": str(video_path.relative_to(output)) if video_path else None,
        }
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2))
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
