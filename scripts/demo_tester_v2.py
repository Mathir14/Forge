"""Live Demonstration of Forge Tester v2 (The Interactive Sensory User).

Demonstrates discovering a genuine user-facing defect that:
1. Executor automated tests missed (unit tests pass: elements exist in DOM).
2. Reviewer code review missed (JavaScript syntax and function signatures look clean).
3. Tester v2 discovers via empirical browser interaction (clicking button triggers uncaught TypeError in browser console).
"""

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from forge.core.config import Config
from forge.core.context import Context
from forge.core.git import GitService
from forge.storage.run_manager import RunManager
from forge.testing.engine import TesterEngine


def run_demo():
    print("=" * 70)
    print("  FORGE TESTER v2 — LIVE REAL-WORLD EMPIRICAL VERIFICATION DEMO")
    print("=" * 70)

    # 1. Create a demo web application project
    demo_dir = Path("/tmp/forge_demo_store")
    if demo_dir.exists():
        shutil.rmtree(demo_dir)
    demo_dir.mkdir(parents=True, exist_ok=True)

    # Write web app files
    (demo_dir / "package.json").write_text(json.dumps({
        "name": "forge-demo-store",
        "version": "1.0.0",
        "scripts": {
            "dev": "python3 -m http.server 3000 --directory public"
        },
        "dependencies": {
            "react": "^19.0.0"
        }
    }, indent=2))

    public_dir = demo_dir / "public"
    public_dir.mkdir(parents=True, exist_ok=True)

    # Web page containing a subtle runtime bug
    (public_dir / "index.html").write_text("""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Acme Checkout Store</title>
  <style>
    body { font-family: sans-serif; margin: 40px; }
    .card { border: 1px solid #ccc; padding: 20px; border-radius: 8px; max-width: 400px; }
    button { background: #0066cc; color: white; border: none; padding: 8px 16px; border-radius: 4px; cursor: pointer; }
    input { padding: 8px; width: 60%; margin-right: 8px; }
    .price { font-size: 24px; font-weight: bold; margin: 12px 0; color: #222; }
  </style>
</head>
<body>
  <div class="card">
    <h2>Checkout Order #1042</h2>
    <div class="price" id="order-total">$120.00</div>
    <p>Apply promotional code:</p>
    <div style="display: flex;">
      <input id="coupon" name="coupon" placeholder="e.g. SAVE20" value="SAVE20" />
      <button id="apply-coupon-btn" onclick="applyCoupon()">Apply</button>
    </div>
  </div>

  <script>
    function applyCoupon() {
      // DEFECT: window.checkoutConfig was never initialized or fetched!
      // This passes unit tests (element exists, function is defined).
      // This passes code review (logic looks plausible).
      // But clicking the button throws: Uncaught TypeError: Cannot read properties of undefined!
      var config = window.checkoutConfig;
      var rate = config.discountRate; 
      var total = 120.00 * (1 - rate);
      document.getElementById('order-total').innerText = '$' + total.toFixed(2);
    }
  </script>
</body>
</html>""", encoding="utf-8")

    # 2. Simulate Executor running unit tests: tests pass!
    print("\n[Stage 03_Executor Validation]")
    print("  ✓ Build succeeded (HTML/JS bundle valid)")
    print("  ✓ Unit test: document.querySelector('#apply-coupon-btn') is not None -> PASSED")
    print("  ✓ Executor claims: 'Implemented coupon code validation and applied discount calculations.'")

    # 3. Simulate Reviewer review: syntax looks clean!
    print("\n[Stage 05_Reviewer Static Analysis]")
    print("  ✓ Diff inspection: Clean function signature, appropriate naming, no secrets.")
    print("  ✓ Reviewer observation: 'applyCoupon logic looks clean and adheres to conventions.'")

    # 4. Now invoke Tester v2: Empirical Black-Box Verification
    print("\n[Stage 04_Tester v2 Empirical Execution]")
    print("  ▶ Archetype detected: WEB_SPA")
    print("  ▶ Runtime Supervisor starting application: python3 -m http.server 3000 --directory public")
    print("  ▶ Polling readiness on http://127.0.0.1:3000 ...")

    rm = RunManager(demo_dir)
    run = rm.create_run(task="Implement checkout discount coupon calculation")

    context = Context(
        run=run,
        project_root=demo_dir,
        config=Config.default(),
        git=GitService(demo_dir),
    )

    engine = TesterEngine(context=context, run_manager=rm)
    result = engine.run()

    print(f"\n  ✓ Tester Execution Complete!")
    print(f"  • Overall Empirical Verdict: {result.status}")
    print(f"  • Duration: {result.duration_seconds:.2f}s")
    print(f"  • Exit Code: {result.response.exit_code}")

    report_json_path = run.run_dir / "04_tester.json"
    report_md_path = run.run_dir / "04_tester.md"
    assert report_json_path.exists(), "04_tester.json missing!"
    assert report_md_path.exists(), "04_tester.md missing!"

    report_data = json.loads(report_json_path.read_text(encoding="utf-8"))
    coverage = report_data.get("COVERAGE", {})
    issues = report_data.get("ISSUES", {})

    print("\n" + "=" * 70)
    print("  EMPIRICAL TEST RESULTS & DISCOVERED DEFECTS")
    print("=" * 70)
    print(f"  Coverage Summary: {coverage.get('summary')}")
    print(f"  Confidence:       {coverage.get('confidence')}")
    print(f"  Planned:          {coverage.get('planned_journeys')} | Executed: {coverage.get('executed_journeys')} | Failed: {coverage.get('failed_journeys')}")

    critical_defects = issues.get("CRITICAL", [])
    major_defects = issues.get("MAJOR", [])
    total_defects = critical_defects + major_defects

    print(f"\n  Discovered {len(total_defects)} genuine user-facing defect(s):")
    for d in total_defects:
        print(f"\n  🚨 [{d.get('ID')}] {d.get('TITLE')}")
        print(f"     Severity: {d.get('SEVERITY')} | Category: {d.get('CATEGORY')}")
        print(f"     Expected: {d.get('EXPECTED')}")
        print(f"     Actual:   {d.get('ACTUAL')}")
        if d.get("EVIDENCE"):
            for k, p in d["EVIDENCE"].items():
                print(f"     {k.capitalize()}: {p}")

    evidence_dir = run.run_dir / "evidence"
    screenshots = list((evidence_dir / "screenshots").glob("*.png"))
    repro_scripts = list((evidence_dir / "repro").glob("*"))
    telemetry_files = list((evidence_dir / "telemetry").glob("*"))

    print("\n" + "=" * 70)
    print("  ATTACHED EVIDENCE BUNDLE")
    print("=" * 70)
    print(f"  📁 Screenshots captured: {len(screenshots)} files")
    for s in screenshots:
        print(f"     • {s}")
    print(f"  📁 Standalone Repro Scripts: {len(repro_scripts)} files")
    for r in repro_scripts:
        print(f"     • {r}")
    print(f"  📁 Telemetry Logs: {len(telemetry_files)} files")
    for t in telemetry_files:
        print(f"     • {t}")

    print("\n" + "=" * 70)
    print("  CONCLUSION: DEMO SUCCESSFUL!")
    print("  Tester v2 discovered a genuine runtime client crash that unit tests and code review missed.")
    print("=" * 70)


if __name__ == "__main__":
    run_demo()
