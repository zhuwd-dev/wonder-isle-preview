#!/usr/bin/env python3
"""Online smoke test for the isolated Pages build; all browser data is ephemeral."""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

SOURCE_TOOLS = Path(os.environ["SOURCE_TOOLS"]).resolve()
sys.path.insert(0, str(SOURCE_TOOLS))
from browser_navigation import Navigator  # noqa: E402
from web_save_namespace import MIRROR_KEY, SAVE_KEY  # noqa: E402

PREVIEW_URL = os.environ["PREVIEW_URL"].rstrip("/") + "/"
PRODUCTION_URL = os.environ["PRODUCTION_URL"].rstrip("/") + "/"
EXPECTED_SHA = os.environ["EXPECTED_SHA"].lower()


def main() -> int:
    errors: list[str] = []
    failed_requests: list[str] = []
    bad_responses: list[str] = []
    with sync_playwright() as playwright:
        options: dict[str, object] = {"headless": True}
        chromium = os.environ.get("CHROMIUM_PATH")
        if chromium:
            options["executable_path"] = chromium
        browser = playwright.chromium.launch(**options)
        # New non-persistent context: never reads or deletes the user's browser profile.
        context = browser.new_context(viewport={"width": 1280, "height": 720}, service_workers="block")
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        page.on("requestfailed", lambda request: failed_requests.append(f"{request.url}: {request.failure}"))
        page.on("response", lambda response: bad_responses.append(f"{response.status}: {response.url}") if response.status >= 400 else None)

        response = page.request.get(PREVIEW_URL, timeout=60000)
        assert response.status == 200, f"preview page HTTP {response.status}"
        html = response.text()
        marker = page.request.get(urljoin(PREVIEW_URL, "BUILD_COMMIT.txt?online-check=" + EXPECTED_SHA), timeout=30000)
        assert marker.status == 200 and marker.text().strip() == EXPECTED_SHA, f"BUILD_COMMIT mismatch: {marker.status} {marker.text()!r}"
        assert "Development Preview" in html, "Development Preview label is missing"
        config_match = re.search(r"const GODOT_CONFIG = (\{[^;]+\});", html)
        assert config_match, "Godot configuration is missing"
        config = json.loads(config_match.group(1))
        assert f"--build-revision={EXPECTED_SHA}" in config.get("args", []), config.get("args")
        assert "--save-namespace=preview" in config.get("args", []), config.get("args")
        executable = config["executable"]
        for suffix in (".wasm", ".pck", ".js"):
            asset = page.request.get(urljoin(PREVIEW_URL, f"{executable}{suffix}"), timeout=60000)
            assert asset.status == 200, f"{executable}{suffix} HTTP {asset.status}"
        print("PASS online BUILD_COMMIT, preview label, namespace, WASM/PCK/JS", flush=True)

        nav = Navigator(page, html)
        page.goto(urljoin(PREVIEW_URL, "index.html?test_telemetry=1"), wait_until="load", timeout=60000)
        page.wait_for_selector("canvas", timeout=60000)
        initial = nav.position()
        assert initial.get("build_revision") == EXPECTED_SHA, initial
        canvas = page.locator("canvas").first
        assert canvas.evaluate("element => !!element.getContext('webgl2')"), "WebGL2 did not initialize"
        print("PASS live game initialized with WebGL2 and expected source SHA", flush=True)

        save_key = SAVE_KEY
        def saved() -> dict | None:
            return page.evaluate("""async key => new Promise((resolve, reject) => {
                const opening = indexedDB.open('/userfs');
                opening.onerror = () => reject(opening.error);
                opening.onsuccess = () => {
                    const db = opening.result;
                    const tx = db.transaction('FILE_DATA', 'readonly');
                    const req = tx.objectStore('FILE_DATA').get(key);
                    let value = null;
                    req.onerror = () => reject(req.error);
                    req.onsuccess = () => {
                        if (req.result) value = JSON.parse(new TextDecoder().decode(new Uint8Array(req.result.contents)));
                    };
                    tx.oncomplete = () => { db.close(); resolve(value); };
                    tx.onerror = () => reject(tx.error);
                };
            })""", save_key)

        def verify(predicate, label: str) -> dict:
            deadline = time.monotonic() + 60
            latest = None
            while time.monotonic() < deadline:
                latest = saved()
                memory = nav.position().get("test_save_data")
                if latest and memory and predicate(latest) and predicate(memory):
                    print(f"PASS {label} in memory and IndexedDB", flush=True)
                    return latest
                page.wait_for_timeout(150)
            raise AssertionError(f"{label}: memory={nav.position().get('test_save_data')} saved={latest}")

        def interact() -> None:
            page.mouse.click(1214, 662)

        routes = ((('y', 250), ('x', 350)), (('x', 475), ('y', 500)), (('y', 460), ('x', 625), ('y', 525)))
        for index, route in enumerate(routes):
            nav.go(*route)
            state = nav.position()
            if not state.get("ready"):
                raise AssertionError(f"Q001 board {index + 1} interaction not ready: {state}")
            interact()
            verify(lambda data, i=index: data["collected"][i], f"Q001 collect board {index + 1}")

        for index, source, target in ((2, (1102, 647), (797, 369)), (0, (942, 647), (863, 361)), (1, (1022, 647), (831, 364))):
            page.mouse.move(*source)
            page.mouse.down()
            page.mouse.move(*target, steps=12)
            page.mouse.up()
            verify(lambda data, i=index: data["placed"][i], f"Q001 repair bridge plank {index + 1}/3")
        bridge_save = verify(lambda data: data["bridge_repaired"], "Q001 bridge repaired")

        page.set_viewport_size({"width": 844, "height": 390})
        page.wait_for_timeout(300)
        bounds = canvas.bounding_box()
        assert bounds and round(bounds["width"]) == 844 and round(bounds["height"]) == 390, bounds
        page.set_viewport_size({"width": 932, "height": 430})
        page.wait_for_timeout(300)
        bounds = canvas.bounding_box()
        assert bounds and round(bounds["width"]) == 932 and round(bounds["height"]) == 430, bounds
        page.set_viewport_size({"width": 1280, "height": 720})

        nav.state = None
        page.reload(wait_until="load", timeout=60000)
        restored = nav.position()
        assert restored.get("test_save_data", {}).get("bridge_repaired") is True, restored
        assert saved() and saved().get("bridge_repaired") is True, saved()
        print("PASS online 844x390, 932x430 and refresh restores bridge save", flush=True)

        preview_mirror = page.evaluate("key => localStorage.getItem(key)", MIRROR_KEY)
        assert preview_mirror, "preview localStorage mirror was not written"
        assert "preview" in SAVE_KEY and "preview" in MIRROR_KEY
        # Visit formal Pages in this same ephemeral context only. Its real user profile is untouched.
        production_response = page.goto(PRODUCTION_URL, wait_until="load", timeout=60000)
        assert production_response and production_response.status == 200, "formal site did not load in isolated context"
        page.wait_for_selector("canvas", timeout=60000)
        page.wait_for_timeout(2000)
        same_mirror = page.evaluate("key => localStorage.getItem(key)", MIRROR_KEY)
        assert same_mirror == preview_mirror, "formal site altered the preview mirror"
        keys = page.evaluate("() => Object.keys(localStorage)")
        assert MIRROR_KEY in keys and not any(key == "wonder-isle-save-v1" for key in keys), keys
        print("PASS formal page leaves preview save mirror unchanged in isolated browser context", flush=True)

        pwa = browser.new_context(viewport={"width": 1280, "height": 720}, service_workers="allow")
        pwa_page = pwa.new_page()
        for url in (PREVIEW_URL, PRODUCTION_URL):
            result = pwa_page.goto(url, wait_until="load", timeout=60000)
            assert result and result.status == 200, f"PWA page failed: {url}"
            pwa_page.wait_for_selector("canvas", timeout=60000)
            pwa_page.wait_for_timeout(2500)
        isolation = pwa_page.evaluate("""async () => {
            const regs = await navigator.serviceWorker.getRegistrations();
            const keys = await caches.keys();
            const workers = {};
            for (const path of ['/wonder-isle-preview/index.service.worker.js', '/wonder-isle/index.service.worker.js']) {
                const response = await fetch(path, {cache: 'no-store'});
                workers[path] = response.ok ? await response.text() : '';
            }
            return {scopes: regs.map(reg => new URL(reg.scope).pathname), caches: keys, workers};
        }""")
        assert "/wonder-isle-preview/" in isolation["scopes"], isolation
        assert "/wonder-isle/" in isolation["scopes"], isolation
        preview_worker = isolation["workers"]["/wonder-isle-preview/index.service.worker.js"]
        production_worker = isolation["workers"]["/wonder-isle/index.service.worker.js"]
        assert "wonder-isle-preview-" in preview_worker, "preview cache prefix missing"
        assert "wonder-isle-preview-" not in production_worker, "formal worker shares preview cache prefix"
        assert any(key.startswith("wonder-isle-preview-") for key in isolation["caches"]), isolation["caches"]
        print("PASS preview and formal Service Worker scopes and Cache Storage namespaces are separate", flush=True)

        if errors or failed_requests or bad_responses:
            raise AssertionError(json.dumps({"console_errors": errors, "failed_requests": failed_requests, "bad_responses": bad_responses}, ensure_ascii=False))
        print(json.dumps({"result": "passed", "source_sha": EXPECTED_SHA, "q001": "boards collected and bridge repaired", "save_after_refresh": "passed", "viewport_checks": ["844x390", "932x430"], "formal_preview_storage_isolation": "passed", "service_worker_cache_isolation": "passed", "console_errors": errors, "failed_requests": failed_requests, "bad_responses": bad_responses}, ensure_ascii=False), flush=True)
        pwa.close()
        context.close()
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
