from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / ".agents" / "plugins" / "media-scrape-autopilot"


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
