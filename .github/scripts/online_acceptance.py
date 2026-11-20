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

        # Complete R01 on the deployed Pages site with ordinary desktop keys.
        # Read-only telemetry guides real input; it never moves the player.
        # The Q004 wood crate sits by the initial spawn and intentionally
        # takes interaction priority over the gate. Walk around it on the
        # normal path before approaching the gate; do not teleport the player.
        nav.go(("y", 430), ("x", 210), ("y", 440))
        gate_state = nav.position()
        assert gate_state.get("ready") and gate_state.get("action") == "去竞技岛", gate_state
        page.screenshot(path="/tmp/preview-r01-entrance.png", full_page=True)
        page.keyboard.press("e")

        def wait_comp_screen(expected: int, timeout: float = 20) -> dict:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                state = nav.competition_position()
                if state.get("screen") == expected:
                    return state
                page.wait_for_timeout(50)
            raise AssertionError(f"R01 screen {expected} not reached: {nav.competition_position()}")

        state = wait_comp_screen(0)
        page.screenshot(path="/tmp/preview-r01-hub.png", full_page=True)
        # Walk from the life-island exit to the marked R01 entry inside the hub.
        # This is ordinary keyboard movement, not a player-coordinate edit.
        hub_deadline = time.monotonic() + 20
        page.keyboard.down("w")
        try:
            while time.monotonic() < hub_deadline:
                state = nav.competition_position()
                if state.get("nearest_entry") == "single":
                    break
                page.wait_for_timeout(40)
        finally:
            page.keyboard.up("w")
        assert state.get("nearest_entry") == "single", state
        action = state["action_position"]
        action_size = state["action_size"]
        page.mouse.click(round(action[0] + action_size[0] / 2), round(action[1] + action_size[1] / 2))
        state = wait_comp_screen(1)
        action = state["action_position"]
        action_size = state["action_size"]
        page.mouse.click(round(action[0] + action_size[0] / 2), round(action[1] + action_size[1] / 2))
        state = wait_comp_screen(2)
        assert state.get("clock_running") is True, state
        page.screenshot(path="/tmp/preview-r01-start.png", full_page=True)

        def race_axis(axis: str, target: float, *, jump: bool = False, timeout: float = 30) -> dict:
            deadline = time.monotonic() + timeout
            last_position = None
            last_progress = time.monotonic()
            last_jump = 0.0
            key = ("d" if target > nav.competition_position()["x"] else "a") if axis == "x" else ("s" if target > nav.competition_position()["y"] else "w")
            page.keyboard.down(key)
            try:
                while time.monotonic() < deadline:
                    state = nav.competition_position()
                    if state.get("screen") == 3:
                        return state
                    current = state[axis]
                    if abs(target - current) <= 18:
                        return state
                    position = (state["x"], state["y"])
                    if last_position is None or abs(position[0] - last_position[0]) + abs(position[1] - last_position[1]) > 1:
                        last_position = position
                        last_progress = time.monotonic()
                    elif time.monotonic() - last_progress > 2:
                        raise AssertionError(f"R01 keyboard movement blocked toward {axis}={target}: {state}")
                    if jump and time.monotonic() - last_jump > 0.65:
                        page.keyboard.press("Space")
                        last_jump = time.monotonic()
                    page.wait_for_timeout(35)
            finally:
                page.keyboard.up(key)
            raise AssertionError(f"Timed out walking R01 {axis}={target}: {nav.competition_position()}")

        def race_to(x: float, y: float, *, jump: bool = False) -> dict:
            peak_before = nav.competition_position().get("test_peak_jump_height", 0.0)
            state = race_axis("x", x, jump=jump)
            state = race_axis("y", y, jump=jump)
            if jump:
                landing_deadline = time.monotonic() + 3
                while state.get("jump_active") and time.monotonic() < landing_deadline:
                    page.wait_for_timeout(50)
                    state = nav.competition_position()
                if state.get("test_peak_jump_height", 0.0) - peak_before < 18:
                    raise AssertionError(f"R01 low barrier jump did not clear: {state}")
            return state

        width = state.get("world_width", 1280.0)
        offset_x = (width - 1280.0) * 0.5
        for x, y in ((300, 514), (405, 501), (465, 486)):
            state = race_to(offset_x + x, y)
        state = race_to(offset_x + 525, 452, jump=True)
        for x, y in ((540, 436), (545, 400), (500, 364), (405, 338),
                     (305, 309), (270, 275), (280, 243), (345, 209)):
            state = race_to(offset_x + x, y)
        assert state.get("checkpoint_index", 0) >= 1, f"R01 first ordered checkpoint missing: {state}"
        for x, y in ((445, 182), (560, 168), (650, 177), (715, 204),
                     (735, 234), (755, 258), (770, 289), (790, 334),
                     (910, 344), (1010, 333), (1073, 307), (1090, 274),
                     (1072, 239), (1030, 204), (1004, 172), (1015, 148),
                     (1050, 124), (1068, 111), (1100, 84)):
            state = race_to(offset_x + x, y)
        assert state.get("checkpoint_index") == 2, f"R01 ordered checkpoints incomplete: {state}"
        state = wait_comp_screen(3)
        assert state.get("clock_running") is False and state.get("race_time", 0) > 0, state
        page.screenshot(path="/tmp/preview-r01-finish.png", full_page=True)
        print("PASS live R01 route, jump, ordered checkpoints, finish and timing", flush=True)

        action = state["action_position"]
        action_size = state["action_size"]
        page.mouse.click(round(action[0] + action_size[0] / 2), round(action[1] + action_size[1] / 2))
        state = wait_comp_screen(2)
        assert state.get("checkpoint_index") == 0 and state.get("race_time", 1) <= 0.3, state
        back = state["back_position"]
        back_size = state["back_size"]
        page.mouse.click(round(back[0] + back_size[0] / 2), round(back[1] + back_size[1] / 2))
        wait_comp_screen(0)
        back = nav.competition_position()["back_position"]
        back_size = nav.competition_position()["back_size"]
        page.mouse.click(round(back[0] + back_size[0] / 2), round(back[1] + back_size[1] / 2))
        restored_life = nav.position()
        assert restored_life.get("test_save_data", {}).get("bridge_repaired") is True, restored_life
        print("PASS live R01 replay, return to life island and preserve Q001 state", flush=True)

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

        def open_controlled_pwa(url: str) -> None:
            result = pwa_page.goto(url, wait_until="load", timeout=60000)
            assert result and result.status == 200, f"PWA page failed: {url}"
            pwa_page.wait_for_selector("canvas", timeout=60000)
            pwa_page.evaluate("() => navigator.serviceWorker.ready")
            # A newly installed worker may not control the first navigation.
            # Reload only after activation so the game payload requests pass
            # through the worker and create this site's cache namespace.
            pwa_page.reload(wait_until="load", timeout=60000)
            pwa_page.wait_for_selector("canvas", timeout=60000)
            pwa_page.wait_for_function("navigator.serviceWorker.controller !== null", timeout=30000)
            pwa_page.wait_for_timeout(2500)

        open_controlled_pwa(PREVIEW_URL)
        preview_cache_names = pwa_page.evaluate("() => caches.keys()")
        assert any(key.startswith("wonder-preview-isle-") for key in preview_cache_names), preview_cache_names
        open_controlled_pwa(PRODUCTION_URL)
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
        assert "wonder-preview-isle-" in preview_worker, "preview cache prefix missing"
        assert "wonder-preview-isle-" not in production_worker, "formal worker shares preview cache prefix"
        preview_caches = {key for key in isolation["caches"] if key.startswith("wonder-preview-isle-")}
        production_caches = {key for key in isolation["caches"] if key.startswith("wonder-isle-") and not key.startswith("wonder-preview-isle-")}
        assert preview_caches, isolation["caches"]
        assert production_caches, isolation["caches"]
        assert preview_caches.isdisjoint(production_caches), isolation["caches"]
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
