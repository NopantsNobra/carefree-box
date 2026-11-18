#!/usr/bin/env python3
"""Prototype light-semantics verification for D-04 (2026-11-18).

Verifies in a real browser that the offline prototype implements the confirmed
D-04 semantics: red solid = microphone blocked, red blink (1 s period) = anomaly,
and that no red-breath effect remains. Also re-runs core smoke checks affected
by the light state machine.

Writes evidence/verification-d04.json (run from repository root):

    python scripts/verify_prototype.py

Requires playwright (pip install playwright && playwright install chromium).
Evidence level: local browser observation only (software_simulation); this is
NOT Windows or real-hardware acceptance.
"""
import datetime
import json
import os
import sys

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(ROOT, "prototype", "index.html")
OUT = os.path.join(ROOT, "evidence", "verification-d04.json")

checks = []


def record(name, passed, detail=""):
    checks.append({"name": name, "passed": bool(passed), "detail": str(detail)})


def main():
    page_errors, console_errors, requests = [], [], []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("request", lambda r: requests.append(r.url))

        page.goto(f"file://{PAGE}", wait_until="networkidle")
        page.wait_for_timeout(400)

        # -- Core smoke -----------------------------------------------------
        record("offline_file_load", "智能桌面控制盒" in page.title(), page.title())
        record("fifteen_positions", page.locator(".switch-position").count() == 15,
               page.locator(".switch-position").count())
        mids = page.locator(".switch-position.active[data-position='mid']").count()
        record("five_mid_defaults", mids == 5, f"active mid = {mids}")
        record("light_off_default", page.locator("#lightDiffuser").get_attribute("data-effect") == "off",
               page.locator("#lightDiffuser").get_attribute("data-effect"))

        def pos(n, p_):
            page.click(f'.switch-position[data-switch="{n}"][data-position="{p_}"]')
            page.wait_for_timeout(120)

        def mode(m):
            page.click(f'#modeGroup [data-mode="{m}"]')
            page.wait_for_timeout(120)

        effect = lambda: page.locator("#lightDiffuser").get_attribute("data-effect")
        label = lambda: page.locator("#lightLabel").inner_text()

        # -- D-04 semantics: down position ----------------------------------
        pos(2, "down")
        record("standby_blue_breath", effect() == "blue", effect())
        mode("working")
        record("working_purple_double", effect() == "purple", effect())
        mode("error")
        record("error_red_blink", effect() == "red-blink", effect())
        record("error_label_1s_period", "红色闪烁（1 秒周期）" in label(), label())
        css = page.evaluate(
            "() => {const s = getComputedStyle(document.getElementById('lightDiffuser'));"
            "return {dur: s.animationDuration, timing: s.animationTimingFunction};}")
        record("css_red_blink_duration_1s", css["dur"] == "1s", css["dur"])
        # Chromium serializes step-end as "steps(1)" or "steps(1, end)"
        record("css_red_blink_step_timing",
               css["timing"] in ("step-end", "steps(1)", "steps(1, end)", "steps(1, jump-end)"),
               css["timing"])

        mode("standby")
        pos(1, "down")
        record("mic_block_down_red_solid", effect() == "red-solid", effect())
        record("mic_block_down_label", label() == "麦克风禁用 · 红色常亮", label())
        mode("working")
        record("priority_working_over_mic", effect() == "purple", effect())
        mode("error")
        record("priority_error_over_mic", effect() == "red-blink", effect())
        mode("standby")
        record("mic_block_persist_red_solid", effect() == "red-solid", effect())

        # -- Regression: upper-position mic alert flash ---------------------
        pos(1, "mid")
        pos(2, "up")
        record("up_white", effect() == "white", effect())
        pos(1, "down")
        record("mic_alert_up_red_fast", effect() == "red-fast", effect())
        page.wait_for_timeout(3300)
        record("mic_alert_up_returns_white", effect() == "white", label())

        # -- Unblock returns to standby blue --------------------------------
        pos(2, "down")
        record("mic_blocked_dynamic_still_solid",
               effect() == "red-solid", effect())  # still blocked -> solid red
        pos(1, "mid")
        record("mic_unblock_back_to_blue", effect() == "blue", effect())
        pos(2, "mid")
        record("final_off", effect() == "off", effect())

        # -- Reduced motion: blink degrades to static red -------------------
        ctx2 = browser.new_context(viewport={"width": 1440, "height": 900},
                                   reduced_motion="reduce")
        page2 = ctx2.new_page()
        page2.on("pageerror", lambda e: page_errors.append(str(e)))
        page2.goto(f"file://{PAGE}", wait_until="networkidle")
        page2.wait_for_timeout(300)
        page2.click('.switch-position[data-switch="1"][data-position="down"]')
        page2.wait_for_timeout(150)
        page2.click('.switch-position[data-switch="2"][data-position="down"]')
        page2.wait_for_timeout(700)  # let the background-color transition settle
        rm = page2.evaluate(
            "() => {const s = getComputedStyle(document.getElementById('lightDiffuser'));"
            "return {name: s.animationName, bg: s.backgroundColor};}")
        record("reduced_motion_mic_static_red",
               rm["name"] == "none" and rm["bg"] == "rgb(196, 67, 67)", rm)
        page2.click('#modeGroup [data-mode="error"]')
        page2.wait_for_timeout(700)  # let the background-color transition settle
        rm2 = page2.evaluate(
            "() => {const s = getComputedStyle(document.getElementById('lightDiffuser'));"
            "return {name: s.animationName, bg: s.backgroundColor};}")
        record("reduced_motion_blink_static_red",
               rm2["name"] == "none" and rm2["bg"] == "rgb(166, 60, 60)", rm2)
        ctx2.close()

        # -- No stale red-breath references ---------------------------------
        record("no_red_breath_effect_left", "red-breath" not in page.content(), "page source")
        record("no_javascript_errors", not page_errors, page_errors)
        record("no_console_errors", not console_errors, console_errors)
        # file:// page load is not a network request; only external schemes count
        net = [u for u in requests if not u.startswith("file://")]
        record("no_network_requests", not net, net)

        browser.close()

    passed = sum(1 for c in checks if c["passed"])
    failed = len(checks) - passed
    doc = {
        "schema_version": 1,
        "status": "passed" if failed == 0 else "failed",
        "observed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "browser": "Chromium via Playwright",
        "url_scheme": "file",
        "browser_offline": True,
        "scope": "Local browser observation of the D-04 light-semantics sync; "
                 "not Windows or real hardware acceptance.",
        "prototype_version": "V3.1 software-decoupled + D-04 semantics (2026-11-18)",
        "decision": "D-04: red solid = microphone blocked; red blink 1 s = anomaly",
        "supersedes": "error_red_breath expectation in verification-decoupled.json "
                      "(historical record of the 2026-11-17 state, kept unchanged)",
        "total_checks": len(checks),
        "passed": passed,
        "failed": failed,
        "checks": checks,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    print(f"{passed}/{len(checks)} passed -> {OUT}")
    for c in checks:
        if not c["passed"]:
            print(f"FAILED: {c['name']}: {c['detail']}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
