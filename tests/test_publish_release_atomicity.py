import json
from pathlib import Path
import unittest


class PublishReleaseAtomicityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.script = (
            cls.repo_root / "scripts" / "publish_release.ps1"
        ).read_text(encoding="utf-8")
        cls.orchestrator = (
            cls.repo_root / "scripts" / "build_and_publish.ps1"
        ).read_text(encoding="utf-8")
        cls.workflow = (
            cls.repo_root / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")
        cls.release_doc = (
            cls.repo_root / "RELEASE.md"
        ).read_text(encoding="utf-8")
        cls.config = json.loads(
            (cls.repo_root / "ril_config.json").read_text(
                encoding="utf-8-sig",
            )
        )

    def test_publish_is_split_into_stage_and_activate_phases(self):
        self.assertIn(
            '[ValidateSet("Stage", "Activate")]',
            self.script,
        )
        self.assertIn(
            '[string]$Phase = "Stage"',
            self.script,
        )
        self.assertNotIn("$ServersDeployed", self.script)
        self.assertIn(
            "Activated unified RIL",
            self.script,
        )

    def test_stage_uploads_all_metadata_without_activating_markers(self):
        versioned_assets = self.script.index("$versionedAssets = @(")
        stage_branch = self.script.index('if ($Phase -eq "Stage")')
        stage_complete = self.script.index(
            'Write-Output "Run after review:',
            stage_branch,
        )
        stage_return = self.script.index("    return", stage_complete)
        activate_manifest = self.script.rindex(
            "-Path ([string]$artifacts.manifest_filename)"
        )

        self.assertIn("$manifestFile", self.script[versioned_assets:stage_branch])
        self.assertIn(
            "$legacyVersionFile",
            self.script[versioned_assets:stage_branch],
        )
        self.assertLess(stage_return, activate_manifest)

    def test_only_stage_requires_local_binary_provenance(self):
        self.assertIn(
            'Assert-LocalRelease -RequireBuildInfo:($Phase -eq "Stage")',
            self.script,
        )

    def test_activation_prepares_all_assets_before_markers(self):
        activate_manifest = self.script.rindex(
            "-Path ([string]$artifacts.manifest_filename)"
        )
        legacy_asset = self.script.rindex("Publish-LegacyClientAsset")
        legacy_marker = self.script.rindex(
            "-Path ([string]$artifacts.legacy_version_filename)"
        )

        self.assertLess(legacy_asset, activate_manifest)
        self.assertLess(activate_manifest, legacy_marker)

    def test_existing_version_release_requires_explicit_clobber(self):
        self.assertIn("$AllowVersionClobber", self.script)
        self.assertIn("$ResumeStage", self.script)
        self.assertIn("Resume-VersionedReleaseAssets", self.script)
        self.assertIn("every existing asset", self.script)
        self.assertIn("date.hotfix", self.script)

    def test_activation_rejects_drafts_and_marker_updates_are_idempotent(self):
        self.assertIn("Assert-VersionedReleasePublished", self.script)
        self.assertIn('$isDraft -ne "false"', self.script)
        self.assertIn(
            "Repository activation file is already current",
            self.script,
        )

    def test_github_write_permission_is_checked_before_release_changes(self):
        preflight_call = self.script.rindex("Assert-GitHubWriteAccess")
        stage_branch = self.script.rindex('if ($Phase -eq "Stage")')

        self.assertIn(".permissions.push", self.script)
        self.assertIn("$env:GH_TOKEN", self.script)
        self.assertLess(preflight_call, stage_branch)

    def test_repository_override_cannot_diverge_from_manifest_repository(self):
        self.assertIn(
            "$Repository -ne $configuredRepository",
            self.script,
        )
        self.assertIn(
            "manifest URL과 업로드 대상이 달라지는",
            self.script,
        )

    def test_documentation_records_current_publish_blockers(self):
        self.assertIn("DISTRIBUTION_TOKEN", self.release_doc)
        self.assertIn(
            self.config["release"]["version"],
            self.release_doc,
        )
        self.assertNotIn("-ServersDeployed", self.release_doc)
        self.assertIn("서버도", self.release_doc)

    def test_one_click_release_builds_then_stages_then_activates(self):
        self.assertIn(
            '[ValidateSet("Release", "StageOnly", "ActivateOnly")]',
            self.orchestrator,
        )
        build_call = self.orchestrator.rindex("-Path $buildScript")
        stage_call = self.orchestrator.index(
            'Phase = "Stage"',
            build_call,
        )
        activate_call = self.orchestrator.index(
            'Phase = "Activate"',
            stage_call,
        )
        self.assertLess(build_call, stage_call)
        self.assertLess(stage_call, activate_call)
        self.assertNotIn("exit 0", self.orchestrator)
        self.assertNotIn("exit 0", self.script)

    def test_one_click_release_serializes_and_restores_authentication(self):
        self.assertIn("[Threading.Mutex]::new", self.orchestrator)
        self.assertIn("gh auth token --user $repositoryOwner", self.orchestrator)
        self.assertIn("$hadOriginalToken", self.orchestrator)
        self.assertIn("Remove-Item Env:GH_TOKEN", self.orchestrator)
        self.assertIn("finally {", self.orchestrator)

    def test_publish_blocks_downgrade_and_same_version_replacement(self):
        self.assertIn(
            "Assert-RemoteMarkersDoNotBlockRelease",
            self.script,
        )
        self.assertIn("Downgrade publishing is blocked", self.script)
        self.assertIn(
            "with different contents. Increase the version",
            self.script,
        )
        self.assertIn('"status"\\s*:\\s*"404"', self.script)
        self.assertIn(
            "Could not read repository file",
            self.script,
        )

    def test_stage_and_activate_have_configured_retries(self):
        self.assertIn("$releaseConfig.stage_attempts", self.orchestrator)
        self.assertIn(
            "$releaseConfig.stage_retry_delay_seconds",
            self.orchestrator,
        )
        self.assertIn(
            "$releaseConfig.activation_attempts",
            self.orchestrator,
        )
        self.assertIn("ResumeStage = $true", self.orchestrator)

    def test_workflow_serializes_and_activates_only_after_stage(self):
        self.assertIn("group: ril-production-release", self.workflow)
        self.assertIn("cancel-in-progress: false", self.workflow)
        stage = self.workflow.index(
            "publish_release.ps1 -Phase Stage -ResumeStage"
        )
        activate = self.workflow.index(
            "publish_release.ps1 -Phase Activate",
            stage,
        )
        self.assertLess(stage, activate)


if __name__ == "__main__":
    unittest.main()
