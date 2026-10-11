from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.video_runtime.plugins import PluginContext, PluginDependencyError, VideoPluginRegistry
from app.video_runtime.capabilities import RuntimeCapabilityRegistry
from app.video_runtime.execution import CapabilityExecutionGateway
from app.video_runtime.security import CapabilityGrant, CapabilityGrantSigner
from app.video_runtime.models import MediaArtifactVersion
from app.video_runtime.runtime import VideoBuildRuntime
from app.video_runtime.builtin_plugins.cuti_provider import CutiAtomicProviderPlugin
from app.video_runtime.builtin_plugins.continuity_validator import ContinuityValidatorPlugin
from app.video_runtime.builtin_plugins.continuity_validator import _inspect_scene_reference, _video_info
from app.orchestration.skills import ResolvedSkillRef, SkillContext
from app.video_runtime.builtin_plugins.media_core import resolve_tts_voice_id
from app.models.image_result import VoiceID


class RecordingPlugin:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def on_load(self, _context): self.events.append("load")
    async def before_plan(self, _context): self.events.append("before_plan")
    async def after_plan(self, _context, plan): return plan
    async def before_execute(self, _context, _envelope): self.events.append("before_execute")
    async def after_execute(self, _context, result): return result
    async def validate_artifact(self, _context, _artifact): return []
    async def on_artifact_committed(self, _context, _artifact): self.events.append("committed")
    async def on_unload(self, _context): self.events.append("unload")
    async def migrate(self, _context, from_version, to_version): self.events.append(f"migrate:{from_version}:{to_version}")


class ExecutableProviderPlugin(RecordingPlugin):
    def capability_handlers(self):
        async def generate(_envelope, payload):
            source = payload["source"]
            return MediaArtifactVersion(
                project_id=source["project_id"],
                type=source["type"],
                uri=f"generated-{source['id']}",
                metadata=dict(source.get("metadata") or {}),
            )
        return {"video.generate": generate}


class PluginRegistryTest(unittest.IsolatedAsyncioTestCase):
    async def test_final_video_probe_propagates_cancellation(self):
        artifact = MediaArtifactVersion(
            project_id="project-1",
            type="final_video",
            uri="https://cdn.example.test/final.mp4",
            metadata={"generation_parameters": {"final_output": True}},
        )
        with patch(
            "app.video_runtime.builtin_plugins.continuity_validator._video_info",
            new=AsyncMock(side_effect=asyncio.CancelledError),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await ContinuityValidatorPlugin().validate_artifact(
                    PluginContext(project_id="project-1", build_id="build-1"),
                    artifact,
                )

    async def asyncSetUp(self):
        self.plugin = RecordingPlugin()
        module = types.ModuleType("cuti_test_plugin")
        module.plugin = self.plugin
        sys.modules[module.__name__] = module

    async def test_load_migrate_and_unload_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "video-plugin.yaml"
            manifest.write_text("""
id: test.provider
version: 1.0.0
api_version: v1
runtime_entrypoint: cuti_test_plugin:plugin
trusted: true
migration_version: 2
contributions:
  capabilities: [video.generate]
permissions:
  network_domains: [api.example.test]
  max_cost_usd: 3
""", encoding="utf-8")
            registry = VideoPluginRegistry()
            loaded = await registry.load(manifest)
            self.assertEqual(loaded.id, "test.provider")
            await registry.migrate("test.provider", 1)
            await registry.unload("test.provider")
            self.assertEqual(self.plugin.events, ["load", "migrate:1:2", "unload"])

    async def test_untrusted_plugin_is_denied_in_process(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "video-plugin.yaml"
            manifest.write_text("""
id: test.untrusted
version: 1.0.0
runtime_entrypoint: cuti_test_plugin:plugin
trusted: false
""", encoding="utf-8")
            with self.assertRaises(PermissionError):
                await VideoPluginRegistry().load(manifest)

    async def test_upgrade_runs_migration_before_replacing_plugin(self):
        replacement = RecordingPlugin()
        replacement_module = types.ModuleType("cuti_test_plugin_v2")
        replacement_module.plugin = replacement
        sys.modules[replacement_module.__name__] = replacement_module
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.yaml"
            first.write_text("""
id: test.provider
version: 1.0.0
runtime_entrypoint: cuti_test_plugin:plugin
trusted: true
migration_version: 1
""", encoding="utf-8")
            second = Path(directory) / "second.yaml"
            second.write_text("""
id: test.provider
version: 2.0.0
runtime_entrypoint: cuti_test_plugin_v2:plugin
trusted: true
migration_version: 2
""", encoding="utf-8")
            registry = VideoPluginRegistry()
            await registry.load(first)
            upgraded = await registry.upgrade(second)
            self.assertEqual(upgraded.version, "2.0.0")
            self.assertIs(registry.get("test.provider").implementation, replacement)
            self.assertEqual(replacement.events, ["migrate:1:2", "load"])
            self.assertEqual(self.plugin.events, ["load", "unload"])

    async def test_missing_dependency_is_rejected_before_import(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "video-plugin.yaml"
            manifest.write_text("""
id: test.dependent
version: 1.0.0
runtime_entrypoint: cuti_test_plugin:plugin
trusted: true
dependencies: [missing.provider]
""", encoding="utf-8")
            with self.assertRaises(PluginDependencyError):
                await VideoPluginRegistry().load(manifest)

    async def test_registered_capability_uses_signed_execution_gateway(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "video-plugin.yaml"
            manifest.write_text("""
id: test.provider
version: 1.0.0
runtime_entrypoint: cuti_test_plugin:plugin
trusted: true
contributions:
  capabilities: [video.generate]
permissions:
  network_domains: [api.example.test]
  max_cost_usd: 3
""", encoding="utf-8")
            plugins = VideoPluginRegistry()
            await plugins.load(manifest)
            signer = CapabilityGrantSigner(b"a sufficiently long server-owned secret")
            registry = RuntimeCapabilityRegistry(
                CapabilityExecutionGateway(signer, plugins),
            )

            async def operation(envelope, payload):
                envelope.assert_domain("api.example.test")
                return {"artifact": payload["prompt"]}

            dispose = registry.register(
                plugin_id="test.provider",
                capability="video.generate",
                operation=operation,
            )
            token = signer.issue(CapabilityGrant(
                project_id="project-1",
                session_id="session-1",
                user_id="user-1",
                plugin_id="test.provider",
                capability="video.generate",
                allowed_capabilities=["video.generate"],
                allowed_domains=["api.example.test"],
                max_cost_usd=2,
                timeout_seconds=10,
                idempotency_key="request-1",
                audit_id="audit-1",
                nonce="nonce-1",
                expires_at=int(time.time()) + 60,
            ))
            self.assertEqual(
                await registry.execute(
                    grant_token=token,
                    project_id="project-1",
                    session_id="session-1",
                    user_id="user-1",
                    capability="video.generate",
                    payload={"prompt": "a coat"},
                ),
                {"artifact": "a coat"},
            )
            dispose()
            with self.assertRaises(LookupError):
                await registry.execute(
                    grant_token=token,
                    project_id="project-1",
                    session_id="session-1",
                    user_id="user-1",
                    capability="video.generate",
                    payload={},
                )

    async def test_loaded_provider_handler_executes_queued_incremental_build(self):
        executable = ExecutableProviderPlugin()
        module = types.ModuleType("cuti_executable_plugin")
        module.plugin = executable
        sys.modules[module.__name__] = module
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "video-plugin.yaml"
            manifest.write_text("""
id: test.executable
version: 1.0.0
runtime_entrypoint: cuti_executable_plugin:plugin
trusted: true
contributions:
  capabilities: [video.generate]
permissions:
  max_cost_usd: 1
""", encoding="utf-8")
            registry = VideoPluginRegistry()
            await registry.load(manifest)
            runtime = VideoBuildRuntime(plugins=registry)
            runtime.configure_plugin_execution(
                CapabilityGrantSigner(b"a sufficiently long server-owned secret"),
            )
            project, _version = await runtime.create_project(
                user_id="user-1", title="Plugin build",
            )
            artifact = await runtime.add_artifact(MediaArtifactVersion(
                project_id=project.id,
                type="video",
                metadata={"rebuild_capability": "video.generate"},
            ))
            current = await runtime.repo.get_project(project.id)
            plan = await runtime.preview_change(
                project_id=project.id,
                description="rebuild video",
                target_artifact_version_ids=[artifact.id],
            )
            build = await runtime.apply_rebuild(
                project_id=project.id,
                plan_id=plan.id,
                base_project_version_id=current.current_version_id,
                idempotency_key="plugin-build-1",
                session_id="session-1",
                user_id="user-1",
            )
            while (await runtime.repo.get_build(project.id, build.id)).status in {
                "queued", "running",
            }:
                await __import__("asyncio").sleep(0)
            completed = await runtime.repo.get_build(project.id, build.id)
            self.assertEqual(completed.status, "completed")
            selected = await runtime.repo.current_artifacts(project.id)
            self.assertEqual(selected[0].uri, f"generated-{artifact.id}")
            self.assertIn("before_execute", executable.events)
            self.assertIn("committed", executable.events)
            await runtime.close()

    def test_runtime_tts_maps_descriptive_voice_to_supported_provider_id(self):
        self.assertEqual(
            resolve_tts_voice_id(
                "young-adult-neutral-english-male",
                "At the Academy of Bloodlines, every student waited.",
            ),
            VoiceID.ENGLISH_MAGNETIC_MALE_2.value,
        )
        self.assertEqual(
            resolve_tts_voice_id("温和中文男声", "龙族已经灭绝。"),
            VoiceID.CHINESE_MALE_ANNOUNCER.value,
        )

    async def test_cuti_atomic_provider_adapter_reuses_existing_executor_contract(self):
        captured = {}

        async def fake_atomic(**kwargs):
            captured.update(kwargs)
            return "provider-task-1", {
                "title": "Rebuilt shot",
                "summary": "done",
                "uri": "https://cdn.example.test/shot.mp4",
                "metadata": {"provider_used": "mock", "model": "mock-video-v1"},
            }

        plugin = CutiAtomicProviderPlugin(fake_atomic)
        source = MediaArtifactVersion(
            project_id="project-1",
            artifact_id="shot-1",
            type="video",
            title="Shot one",
            metadata={
                "prompt": "A hero enters",
                "rebuild_capability": "atomic.video.generate",
                "generation_parameters": {
                    "prompt": "A hero enters",
                    "reference_from_steps": ["product", "character", "scene", "product-sheet"],
                },
                "skill_context": SkillContext(
                    instructions="Keep the hero identity stable.",
                    applied_skills=[ResolvedSkillRef(
                        skill_id="video-director",
                        version="1.0.0",
                        content_hash="abc123",
                        source="declared",
                    )],
                ).model_dump(mode="json"),
            },
        )
        envelope = CapabilityGrant(
            project_id="project-1",
            session_id="session-1",
            user_id="user-1",
            plugin_id="cuti.atomic-providers",
            capability="atomic.video.generate",
            allowed_capabilities=["atomic.video.generate"],
            max_cost_usd=1,
            timeout_seconds=30,
            idempotency_key="rebuild-shot-1",
            audit_id="audit-1",
            nonce="nonce-1",
            expires_at=int(time.time()) + 60,
        )
        from app.video_runtime.security import CapabilityExecutionEnvelope
        with patch(
            "app.video_runtime.watermark.publish_public_watermark",
            AsyncMock(side_effect=lambda url, generation_id=None: f"{url}?wm=1"),
        ):
            result = await plugin.capability_handlers()["atomic.video.generate"](
                CapabilityExecutionEnvelope(envelope),
                {
                    "build": {"id": "build-1"},
                    "source": source.model_dump(mode="json"),
                    "completed_replacements": {
                        step: MediaArtifactVersion(
                            project_id="project-1",
                            artifact_id=f"ref:{step}",
                            type="image",
                            uri=f"https://cdn.example.test/{step}.png",
                        ).model_dump(mode="json")
                        for step in ("product", "character", "scene", "product-sheet")
                    },
                },
            )
        self.assertEqual(captured["task"].capability_id, "atomic.video.generate")
        self.assertEqual(captured["task"].parameters["prompt"], "A hero enters")
        self.assertNotIn("Keep the hero identity stable", captured["task"].parameters["prompt"])
        self.assertEqual(captured["task"].resolved_skills[0].skill_id, "video-director")
        self.assertEqual(result.artifact_id, "shot-1")
        self.assertEqual(result.provenance["remote_operation_id"], "provider-task-1")
        self.assertEqual(result.provenance["skills"][0]["skill_id"], "video-director")
        self.assertEqual(len(captured["task"].parameters["images"]), 4)
        self.assertIs(captured["task"].parameters["watermark"], False)
        self.assertEqual(
            result.metadata["resolved_generation_parameters"]["images"],
            [
                f"https://cdn.example.test/{step}.png"
                for step in ("product", "character", "scene", "product-sheet")
            ],
        )
        self.assertFalse(result.metadata["skill_prompt_applied"])
        self.assertEqual(result.metadata["watermark_policy"], "public_overlay")
        self.assertEqual(result.metadata["canonical_uri"], "https://cdn.example.test/shot.mp4")
        self.assertEqual(result.uri, "https://cdn.example.test/shot.mp4?wm=1")

    async def test_cuti_image_provider_keeps_leaf_prompt_isolated_from_workflow_skill(self):
        captured = {}

        async def fake_atomic(**kwargs):
            captured.update(kwargs)
            return "provider-image-1", {
                "title": "Empty apartment",
                "summary": "done",
                "uri": "https://cdn.example.test/scene.webp",
                "metadata": {},
            }

        context = SkillContext(
            instructions=(
                "Generate a character sheet, a scene sheet, and a product sheet."
            ),
            applied_skills=[ResolvedSkillRef(
                skill_id="cuti-scenario-product-workflow",
                version="1.0.0",
                content_hash="workflow-hash",
                source="workflow",
            )],
        )
        source = MediaArtifactVersion(
            project_id="project-1",
            artifact_id="scene-setting-reference",
            type="image",
            title="Scene setting reference",
            metadata={
                "generation_parameters": {
                    "prompt": "Create ONLY an empty apartment environment. Zero people or products.",
                },
                "skill_context": context.model_dump(mode="json"),
            },
        )
        envelope = CapabilityGrant(
            project_id="project-1",
            session_id="session-1",
            user_id="user-1",
            plugin_id="cuti.atomic-providers",
            capability="atomic.image.generate",
            allowed_capabilities=["atomic.image.generate"],
            max_cost_usd=1,
            timeout_seconds=30,
            idempotency_key="scene-image-1",
            audit_id="audit-image-1",
            nonce="nonce-image-1",
            expires_at=int(time.time()) + 60,
        )
        from app.video_runtime.security import CapabilityExecutionEnvelope

        result = await CutiAtomicProviderPlugin(fake_atomic).capability_handlers()[
            "atomic.image.generate"
        ](
            CapabilityExecutionEnvelope(envelope),
            {"build": {"id": "build-1"}, "source": source.model_dump(mode="json")},
        )

        self.assertEqual(
            captured["task"].parameters["prompt"],
            "Create ONLY an empty apartment environment. Zero people or products.",
        )
        self.assertNotIn(
            "Generate a character sheet",
            captured["task"].parameters["prompt"],
        )
        self.assertIs(captured["task"].parameters["watermark"], False)
        self.assertEqual(
            captured["task"].resolved_skills[0].skill_id,
            "cuti-scenario-product-workflow",
        )
        self.assertFalse(result.metadata["skill_prompt_applied"])

    async def test_cuti_provider_resolves_seedance_multimodal_step_references(self):
        captured = {}

        async def fake_atomic(**kwargs):
            captured.update(kwargs)
            return "provider-multimodal-1", {
                "title": "Multimodal clip",
                "summary": "done",
                "uri": "https://cdn.example.test/result.mp4",
                "metadata": {},
            }

        source = MediaArtifactVersion(
            project_id="project-1",
            artifact_id="shot-1",
            type="video_clip",
            title="Shot one",
            metadata={
                "generation_parameters": {
                    "prompt": "中文多模态视频提示词",
                    "video_reference_from_steps": ["motion"],
                    "audio_reference_from_steps": ["rhythm"],
                },
            },
        )
        completed = {
            "motion": MediaArtifactVersion(
                project_id="project-1", artifact_id="source:motion",
                type="source_video", uri="https://cdn.example.test/motion.mp4",
            ).model_dump(mode="json"),
            "rhythm": MediaArtifactVersion(
                project_id="project-1", artifact_id="source:rhythm",
                type="source_audio", uri="https://cdn.example.test/rhythm.mp3",
            ).model_dump(mode="json"),
        }
        envelope = CapabilityGrant(
            project_id="project-1", session_id="session-1", user_id="user-1",
            plugin_id="cuti.atomic-providers", capability="atomic.video.generate",
            allowed_capabilities=["atomic.video.generate"], max_cost_usd=1,
            timeout_seconds=30, idempotency_key="multimodal-shot-1",
            audit_id="audit-multimodal-1", nonce="nonce-multimodal-1",
            expires_at=int(time.time()) + 60,
        )
        from app.video_runtime.security import CapabilityExecutionEnvelope

        with patch(
            "app.video_runtime.watermark.publish_public_watermark",
            AsyncMock(side_effect=lambda url, generation_id=None: f"{url}?wm=1"),
        ):
            result = await CutiAtomicProviderPlugin(fake_atomic).capability_handlers()[
                "atomic.video.generate"
            ](
                CapabilityExecutionEnvelope(envelope),
                {
                    "build": {"id": "build-1"},
                    "source": source.model_dump(mode="json"),
                    "completed_replacements": completed,
                },
            )

        parameters = captured["task"].parameters
        self.assertEqual(parameters["videos"], ["https://cdn.example.test/motion.mp4"])
        self.assertEqual(parameters["reference_videos"], parameters["videos"])
        self.assertEqual(parameters["audios"], ["https://cdn.example.test/rhythm.mp3"])
        self.assertEqual(parameters["reference_audios"], parameters["audios"])
        self.assertNotIn("video_reference_from_steps", parameters)
        self.assertNotIn("audio_reference_from_steps", parameters)
        self.assertEqual(
            result.metadata["resolved_generation_parameters"]["video_urls"],
            parameters["videos"],
        )

    async def test_scene_reference_isolation_validator_guards_provider_boundary(self):
        prompt = (
            "Create exactly one unoccupied environment-only setting reference. "
            "Render a single coherent wide architectural view."
        )
        clean = MediaArtifactVersion(
            project_id="project-1",
            artifact_id="scene-setting-reference",
            type="image",
            uri="https://cdn.example.test/scene.webp",
            metadata={
                "generation_parameters": {
                    "prompt": prompt,
                    "artifact_role": "scene_setting_reference",
                },
                "resolved_generation_parameters": {"prompt": prompt},
                "final_prompt": prompt,
                "skill_prompt_applied": False,
            },
        )
        validator = ContinuityValidatorPlugin()
        with patch.dict(
            "os.environ",
            {"VIDEO_SCENE_REFERENCE_VISUAL_VALIDATION_ENABLED": "false"},
        ):
            clean_results = await validator.validate_artifact(
                PluginContext(project_id="project-1", build_id="build-1"), clean,
            )
            self.assertEqual(len(clean_results), 1)
            self.assertTrue(clean_results[0].passed)
            self.assertEqual(
                clean_results[0].validator_id,
                "cuti.continuity.scene-reference-isolation",
            )
            self.assertIn("continuity", clean_results[0].validator_id)

            contaminated = clean.model_copy(deep=True)
            contaminated.metadata["skill_prompt_applied"] = True
            contaminated.metadata["final_prompt"] = (
                prompt + " Create a character sheet and a product sheet."
            )
            contaminated.metadata["resolved_generation_parameters"]["images"] = [
                "https://cdn.example.test/product.png",
            ]
            failed = await validator.validate_artifact(
                PluginContext(project_id="project-1", build_id="build-1"), contaminated,
            )
        self.assertFalse(failed[0].passed)
        self.assertTrue(any("image inputs" in issue for issue in failed[0].issues))
        self.assertTrue(any("Skill instructions" in issue for issue in failed[0].issues))
        self.assertTrue(any("differs" in issue for issue in failed[0].issues))

    async def test_scene_reference_isolation_checks_rendered_pixels(self):
        prompt = (
            "Create exactly one unoccupied environment-only setting reference. "
            "Render a single coherent wide architectural view."
        )
        artifact = MediaArtifactVersion(
            project_id="project-1",
            artifact_id="scene-setting-reference",
            type="image",
            uri="https://cdn.example.test/scene.webp",
            metadata={
                "generation_parameters": {
                    "prompt": prompt,
                    "artifact_role": "scene_setting_reference",
                },
                "resolved_generation_parameters": {"prompt": prompt},
                "final_prompt": prompt,
                "skill_prompt_applied": False,
            },
        )
        visual_result = {
            "people_present": True,
            "product_present": True,
            "contact_sheet_present": False,
            "reason": "A person holds a staged bottle.",
        }
        with (
            patch.dict(
                "os.environ",
                {"VIDEO_SCENE_REFERENCE_VISUAL_VALIDATION_ENABLED": "true"},
            ),
            patch(
                "app.video_runtime.builtin_plugins.continuity_validator._inspect_scene_reference",
                new=AsyncMock(return_value=visual_result),
            ) as inspect,
        ):
            results = await ContinuityValidatorPlugin().validate_artifact(
                PluginContext(project_id="project-1", build_id="build-1"),
                artifact,
            )
        inspect.assert_awaited_once_with(artifact.uri)
        self.assertFalse(results[0].passed)
        self.assertIn(
            "rendered scene setting reference contains people or characters",
            results[0].issues,
        )
        self.assertIn(
            "rendered scene setting reference contains a staged product",
            results[0].issues,
        )
        self.assertEqual(results[0].metadata["visual_result"], visual_result)

    async def test_character_reference_rejects_a_rendered_identity_mismatch(self):
        prompt = (
            "Create ONLY one clean recurring-character identity reference sheet. "
            "Characters from the approved script: Lin, a 25-year-old East Asian woman, "
            "shoulder-length black hair, cream cardigan. Do not include the advertised "
            "product, environment panels, readable text, labels, captions, logos, "
            "watermarks, storyboard borders, or shot frames."
        )
        artifact = MediaArtifactVersion(
            project_id="project-1",
            artifact_id="character-setting-reference",
            type="image",
            uri="https://cdn.example.test/character.webp",
            metadata={
                "generation_parameters": {
                    "prompt": prompt,
                    "artifact_role": "character_setting_reference",
                },
                "resolved_generation_parameters": {"prompt": prompt},
                "final_prompt": prompt,
                "skill_prompt_applied": False,
            },
        )
        visual_result = {
            "character_present": True,
            "matches_expected_description": False,
            "product_present": False,
            "environment_dominant": False,
            "reason": "The image shows a young man rather than the locked woman.",
        }
        with (
            patch.dict(
                "os.environ",
                {"VIDEO_SCENE_REFERENCE_VISUAL_VALIDATION_ENABLED": "true"},
            ),
            patch(
                "app.video_runtime.builtin_plugins.continuity_validator._inspect_character_reference",
                new=AsyncMock(return_value=visual_result),
            ) as inspect,
        ):
            results = await ContinuityValidatorPlugin().validate_artifact(
                PluginContext(project_id="project-1", build_id="build-1"),
                artifact,
            )
        inspect.assert_awaited_once_with(artifact.uri, prompt)
        self.assertFalse(results[0].passed)
        self.assertIn("continuity", results[0].validator_id)
        self.assertTrue(any("locked character definition" in value for value in results[0].issues))
        self.assertEqual(results[0].metadata["visual_result"], visual_result)

    async def test_scene_visual_inspection_inlines_local_storage_url(self):
        captured = {}

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"output": [{"content": [{"text": json.dumps({
                    "people_present": False,
                    "product_present": False,
                    "contact_sheet_present": False,
                    "reason": "empty room",
                })}]}]}

        class Client:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, url, **kwargs):
                captured["url"] = url
                captured["body"] = kwargs["json"]
                return Response()

        data_uri = "data:image/webp;base64,AAAA"
        with (
            patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}),
            patch(
                "app.utils.file_utils.inline_local_image_url_for_llm",
                new=AsyncMock(return_value=data_uri),
            ) as inline,
            patch(
                "app.video_runtime.builtin_plugins.continuity_validator.httpx.AsyncClient",
                Client,
            ),
        ):
            result = await _inspect_scene_reference(
                "http://127.0.0.1:8001/files/images/scene.webp",
            )

        inline.assert_awaited_once()
        self.assertEqual(
            captured["body"]["input"][0]["content"][1]["image_url"],
            data_uri,
        )
        self.assertFalse(result["people_present"])

    async def test_product_video_identity_validation_is_disabled(self):
        artifact = MediaArtifactVersion(
            project_id="project-1",
            artifact_id="shot-1",
            type="video_clip",
            uri="https://cdn.example.test/shot.mp4",
            metadata={
                "generation_parameters": {
                    "prompt": "Product hero orbit",
                    "product_identity_contract": {"verification_required": True},
                },
            },
        )
        results = await ContinuityValidatorPlugin().validate_artifact(
            PluginContext(project_id="project-1", build_id="build-1"),
            artifact,
        )
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].passed)
        self.assertEqual(results[0].issues, [])
        self.assertEqual(results[0].metadata["mode"], "structural")

    async def test_local_final_video_probe_uses_ffprobe_without_media_service(self):
        class Process:
            returncode = 0

            async def communicate(self):
                return (
                    json.dumps({
                        "format": {"duration": "15.0"},
                        "streams": [
                            {"codec_type": "video", "width": 854, "height": 480,
                             "r_frame_rate": "24/1"},
                            {"codec_type": "audio"},
                        ],
                    }).encode(),
                    b"",
                )

        with (
            patch("app.utils.s3_utils._storage_is_local", return_value=True),
            patch("app.utils.s3_utils.is_our_cdn_url", return_value=True),
            patch("app.utils.s3_utils.s3_utils.cdn_url_to_s3_key", return_value="videos/final.mp4"),
            patch(
                "app.utils.s3_utils.s3_utils._local_dir",
                "/tmp/video-runtime",
                create=True,
            ),
            patch("app.utils.media_service_client.os.path.isfile", return_value=True),
            patch(
                "app.utils.media_service_client.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=Process()),
            ) as ffprobe,
            patch("app.utils.media_service_client._post", new=AsyncMock()) as media_service,
        ):
            info = await _video_info(
                "http://127.0.0.1:8001/files/videos/final.mp4",
            )

        self.assertEqual(info["duration"], 15.0)
        self.assertTrue(info["has_video"])
        self.assertTrue(info["has_audio"])
        ffprobe.assert_awaited_once()
        media_service.assert_not_awaited()
