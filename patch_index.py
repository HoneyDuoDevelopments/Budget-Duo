#!/usr/bin/env python3
"""
Patches index.html to add the ExternalSyncPage component.
Run from the Budget-Duo repo root:
  python3 patch_index.py
"""
import os, sys

HTML_PATH = "backend/static/index.html"

if not os.path.exists(HTML_PATH):
    print(f"ERROR: {HTML_PATH} not found. Run from the Budget-Duo repo root.")
    sys.exit(1)

with open(HTML_PATH, "r") as f:
    content = f.read()

# Check if already patched
if "ExternalSyncPage" in content:
    print("Already patched — ExternalSyncPage found in index.html")
    sys.exit(0)

errors = []

# === PATCH 1: Add ExternalSyncPage component before "// APP ROOT" ===
COMPONENT = open(os.path.join(os.path.dirname(__file__) or ".", "external_sync_page.js")).read()

marker = '// ══════════════════════════════════════════════\n// APP ROOT'
if marker in content:
    content = content.replace(marker, COMPONENT + "\n" + marker)
    print("✅ Patch 1: ExternalSyncPage component inserted")
else:
    errors.append("Could not find APP ROOT marker")

# === PATCH 2: Add nav item after rules ===
old_nav = """    {id:'rules',       label:'Rules',        icon:html`<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"><circle cx="4" cy="4" r="2"/><circle cx="12" cy="12" r="2"/><path d="M4 6v2a2 2 0 0 0 2 2h2a2 2 0 0 1 2 2"/></svg>`},
  ];"""

new_nav = """    {id:'rules',       label:'Rules',        icon:html`<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"><circle cx="4" cy="4" r="2"/><circle cx="12" cy="12" r="2"/><path d="M4 6v2a2 2 0 0 0 2 2h2a2 2 0 0 1 2 2"/></svg>`},
    {id:'sync',        label:'External Sync', icon:html`<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M2 8a6 6 0 0 1 10.3-4.2M14 8a6 6 0 0 1-10.3 4.2"/><path d="M14 2v4h-4M2 14v-4h4"/></svg>`},
  ];"""

if old_nav in content:
    content = content.replace(old_nav, new_nav)
    print("✅ Patch 2: Nav item added")
else:
    errors.append("Could not find NAV rules entry")

# === PATCH 3: Add page title ===
if "rules:'Merchant Rules'," in content and "sync:'External Account Sync'" not in content:
    content = content.replace(
        "rules:'Merchant Rules',",
        "rules:'Merchant Rules',\n    sync:'External Account Sync',"
    )
    print("✅ Patch 3: Page title added")
else:
    errors.append("Could not find PAGE_TITLES rules entry")

# === PATCH 4: Add page render ===
old_render = "${tab==='rules'&&html`<${RulesPage} categories=${categories||[]}/>`}"
new_render = old_render + "\n          ${tab==='sync'&&html`<${ExternalSyncPage}/>`}"

if old_render in content and "ExternalSyncPage" not in content.split(old_render)[1][:200]:
    content = content.replace(old_render, new_render, 1)
    print("✅ Patch 4: Page render added")
elif "ExternalSyncPage" in content:
    print("✅ Patch 4: Already present")
else:
    errors.append("Could not find rules page render")

if errors:
    print(f"\n⚠️  Errors: {errors}")
    sys.exit(1)

# Write the patched file
with open(HTML_PATH, "w") as f:
    f.write(content)

print(f"\n✅ index.html patched successfully ({len(content)} bytes)")
