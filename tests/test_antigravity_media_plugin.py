from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / ".agents" / "plugins" / "media-scrape-autopilot"
AUTO_EXECUTE_PLUGIN = ROOT / ".agents" / "plugins" / "auto-execute"


def test_antigravity_media_plugin_manifest_and_hook_are_valid():
    plugins_config = json.loads((ROOT / ".agents" / "plugins.json").read_text(encoding="utf-8"))
    assert plugins_config["entries"] == [{"path": ".agents/plugins"}]

    skills_config = json.loads((ROOT / ".agents" / "skills.json").read_text(encoding="utf-8"))
    assert skills_config["entries"] == [{"path": ".agents/skills"}]

    manifest = json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "media-scrape-autopilot"
    assert manifest["$schema"] == "https://antigravity.google/schemas/v1/plugin.json"

    hooks = json.loads((PLUGIN / "hooks.json").read_text(encoding="utf-8"))
    hook = hooks["media-scrape-autopilot-auto-allow"]["PreToolUse"][0]["hooks"][0]
    assert hook["type"] == "command"
    assert hook["command"] == "python allow.py"
    assert (PLUGIN / "allow.py").exists()
    assert (PLUGIN / "scripts" / "install_antigravity_auto_approvals.ps1").exists()


def test_antigravity_media_plugin_skill_and_rule_cover_output_contract():
    skill = (PLUGIN / "skills" / "media-scrape-bundle" / "SKILL.md").read_text(encoding="utf-8")
    rules = (PLUGIN / "rules" / "media-scrape-autopilot.md").read_text(encoding="utf-8")
    agents_rules = (PLUGIN / "rules" / "AGENTS.md").read_text(encoding="utf-8")
    workspace_rules = (ROOT / ".agents" / "rules" / "AGENTS.md").read_text(encoding="utf-8")
    code_builder = (ROOT / ".agents" / "agents" / "code_builder" / "agent.md").read_text(encoding="utf-8")
    combined = f"{skill}\n{rules}\n{agents_rules}\n{workspace_rules}\n{code_builder}"

    assert "RequestFeedback: false" in combined
    assert "crawl_and_export_bundle" in combined
    assert "download_media_and_zip" in combined
    assert "outputs/csv/" in combined
    assert "outputs/media/<job_name>/" in combined
    assert "outputs/zips/<job_name>_media.zip" in combined
    assert "Submit plan" in combined
    assert "command(*)" in combined
    assert "unsandboxed(*)" in combined
    assert "Never stop at" in combined
    assert "AGENTS.md" in combined


def test_antigravity_customizations_check_script_exists_and_covers_checks():
    import os
    import shutil
    import subprocess

    script_path = ROOT / ".agents" / "scripts" / "check_antigravity_customizations.ps1"
    assert script_path.exists(), "check_antigravity_customizations.ps1 must exist"

    script_text = script_path.read_text(encoding="utf-8")

    # Key checks verified by the script
    assert "AGENTS.md" in script_text
    assert "plugins.json" in script_text
    assert "skills.json" in script_text
    assert "auto-execute" in script_text
    assert "media-scrape-autopilot" in script_text
    assert ".gemini\\bin\\agy.exe" in script_text
    assert "plugin validate" in script_text
    assert "plugin list" in script_text
    assert "--add-dir" in script_text
    assert "code_builder" in script_text
    assert "PASS" in script_text
    assert "FAIL" in script_text
    assert "exit 1" in script_text
    assert "exit 0" in script_text

    # Execute verification script if environment supports it
    powershell_cmd = shutil.which("powershell") or shutil.which("pwsh")
    agy_path = Path(os.environ.get("USERPROFILE", "")) / ".gemini" / "bin" / "agy.exe"
    if powershell_cmd and agy_path.exists():
        proc = subprocess.run(
            [powershell_cmd, "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"Script failed with output:\n{proc.stdout}\n{proc.stderr}"
        assert "Result: PASS" in proc.stdout


def test_auto_execute_plugin_requires_strict_review_after_plan():
    manifest = json.loads((AUTO_EXECUTE_PLUGIN / "plugin.json").read_text(encoding="utf-8"))
    rules = (AUTO_EXECUTE_PLUGIN / "rules" / "AGENTS.md").read_text(encoding="utf-8")
    review_skill = (AUTO_EXECUTE_PLUGIN / "skills" / "review" / "SKILL.md").read_text(encoding="utf-8")
    readme = (AUTO_EXECUTE_PLUGIN / "README.md").read_text(encoding="utf-8")
    combined = f"{rules}\n{review_skill}\n{readme}"

    assert manifest["name"] == "auto-execute"
    assert manifest["$schema"] == "https://antigravity.google/schemas/v1/plugin.json"
    assert "name: review" in review_skill
    assert "automatically after implementing a /plan" in review_skill
    assert "adversarial mindset" in combined
    assert "P0, P1, and P2" in combined
    assert "maximum of three full review passes" in review_skill
    assert "before `git commit`" in rules
    assert "green tests as evidence, never as proof" in review_skill
    assert "explicit standalone `/review` is read-only" in review_skill
