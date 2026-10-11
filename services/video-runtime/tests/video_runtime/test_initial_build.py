from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.video_runtime.plan_utils import BuildPlanValidationError, topological_steps
from app.video_runtime.builtin_plugins.continuity_validator import ContinuityValidatorPlugin
from app.video_runtime.models import (
    MediaEditOperation,
    MediaArtifactVersion,
    RebuildPlan,
    RebuildPlanItem,
    ValidationResult,
    VideoLanguageContract,
    VideoSpec,
)
from app.video_runtime.runtime import VideoBuildRuntime
from app.video_runtime.plugins import PluginContext
from app.video_runtime.skill_workflows import load_workflow_skills


async def video_runtime(
    *, max_parallel_generation_tasks: int | None = None,
) -> VideoBuildRuntime:
    runtime = VideoBuildRuntime(
        max_parallel_generation_tasks=max_parallel_generation_tasks,
    )
    await runtime.plugins.load_directories([
        Path(__file__).resolve().parents[2] / "plugins",
    ])
    await load_workflow_skills(runtime.plugins, runtime.skills)
    return runtime


def video_spec() -> VideoSpec:
    return VideoSpec.model_validate({
        "title": "A fifteen second story",
        "language": "zh-CN",
        "target_duration_seconds": 15,
        "aspect_ratio": "16:9",
        "resolution": "1080p",
        "workflow_id": "seedance2",
        "style_id": "cuti.cinematic",
        "workflow_parameters": {
            "scenes": [{
                "id": "station",
                "name": "Railway station",
                "description": "A quiet night railway station with a clock and blue platform lights",
            }],
        },
        "characters": [{
            "id": "hero", "name": "Hero", "appearance": "short black hair",
            "clothing": "blue coat",
        }],
        "shots": [
            {
                "id": "one", "order": 1, "duration_seconds": 5,
                "beat": "arrives", "visual_prompt": "0-5秒，主角抵达车站，镜头平稳推进。",
                "narration": "他抵达车站。", "character_ids": ["hero"],
            },
            {
                "id": "two", "order": 2, "duration_seconds": 5,
                "beat": "waits", "visual_prompt": "0-5秒，主角在时钟下等待，灯光缓慢变化。",
                "narration": "时间慢慢过去。", "character_ids": ["hero"],
            },
            {
                "id": "three", "order": 3, "duration_seconds": 5,
                "beat": "leaves", "visual_prompt": "0-5秒，主角登上列车，镜头随动作收束。",
                "narration": "列车终于到来。", "character_ids": ["hero"],
            },
        ],
        "audio": {
            "narration_voice": "Wise_Woman", "bgm_prompt": "quiet cinematic piano",
            "subtitles": True,
        },
        "providers": {"video": "seedance-2.0", "image": "gpt-image-2", "music": "suno"},
        "automation": {"mode": "automatic", "max_artifact_retries": 1},
    })


class FakePlanExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.remote_resumes: dict[str, str] = {}

    async def execute_plan_step(
        self, *, build, step, completed_artifacts, idempotency_key,
        report_remote_operation=None,
    ) -> MediaArtifactVersion:
        self.calls.append(step.step_id)
        if step.parameters.get("remote_operation_id"):
            self.remote_resumes[step.step_id] = step.parameters["remote_operation_id"]
        if step.output_artifact_type == "video_clip" and report_remote_operation:
            await report_remote_operation(f"remote-{step.step_id}", "fake-provider")
        metadata = {}
        metadata.update({
            "generation_parameters": dict(step.parameters),
            "rebuild_capability": step.capability,
            "plan_step_id": step.step_id,
        })
        if step.output_artifact_type == "video_spec":
            metadata["content"] = step.parameters["content"]
        if step.output_artifact_type == "timeline":
            cursor = 0.0
            items = []
            for shot, dependency in zip(step.parameters["shots"], step.parameters["video_steps"], strict=True):
                artifact = completed_artifacts[dependency]
                items.append({
                    "artifactVersionId": artifact.id,
                    "startSeconds": cursor,
                    "durationSeconds": shot["duration_seconds"],
                })
                cursor += shot["duration_seconds"]
            metadata["timeline"] = {"items": items, "durationSeconds": cursor}
        return MediaArtifactVersion(
            project_id=build.project_id,
            type=step.output_artifact_type,
            uri=None if step.output_artifact_type in {"video_spec", "script", "characters", "storyboard", "timeline"}
            else f"https://media.test/{step.step_id}",
            metadata=metadata,
            provenance={"idempotency_key": idempotency_key},
        )

    async def rebuild_artifact(
        self, *, build, source, completed_replacements, idempotency_key,
    ) -> MediaArtifactVersion:
        parameters = dict(source.metadata.get("rebuild_parameters") or {})
        metadata = {
            **source.metadata,
            "generation_parameters": parameters,
            "rebuild_capability": source.metadata.get("rebuild_capability"),
            "plan_step_id": source.metadata.get("plan_step_id"),
        }
        if source.type == "video_spec":
            metadata["content"] = parameters["content"]
        return MediaArtifactVersion(
            project_id=build.project_id,
            artifact_id=source.artifact_id,
            type=source.type,
            uri=source.uri,
            metadata=metadata,
            provenance={"idempotency_key": idempotency_key},
        )


class CancellationSwallowingPlanExecutor(FakePlanExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()

    async def execute_plan_step(self, **kwargs) -> MediaArtifactVersion:
        if not self.calls:
            self.entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass
        return await super().execute_plan_step(**kwargs)

class ConcurrencyProbeExecutor(FakePlanExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.active_group_steps = 0
        self.max_active_group_steps = 0

    async def execute_plan_step(
        self, *, build, step, completed_artifacts, idempotency_key,
        report_remote_operation=None,
    ) -> MediaArtifactVersion:
        if not step.execution_group:
            return await super().execute_plan_step(
                build=build,
                step=step,
                completed_artifacts=completed_artifacts,
                idempotency_key=idempotency_key,
                report_remote_operation=report_remote_operation,
            )
        self.active_group_steps += 1
        self.max_active_group_steps = max(
            self.max_active_group_steps,
            self.active_group_steps,
        )
        try:
            # Give sibling tasks a deterministic chance to enter the provider
            # call. A serial executor can never observe more than one here.
            await asyncio.sleep(0.02)
            return await super().execute_plan_step(
                build=build,
                step=step,
                completed_artifacts=completed_artifacts,
                idempotency_key=idempotency_key,
                report_remote_operation=report_remote_operation,
            )
        finally:
            self.active_group_steps -= 1


class FailOneParallelExecutor(FakePlanExecutor):
    def __init__(self, failed_step_id: str) -> None:
        super().__init__()
        self.failed_step_id = failed_step_id

    async def execute_plan_step(
        self, *, build, step, completed_artifacts, idempotency_key,
        report_remote_operation=None,
    ) -> MediaArtifactVersion:
        if step.execution_group:
            await asyncio.sleep(0.02)
        if step.step_id == self.failed_step_id:
            self.calls.append(step.step_id)
            raise RuntimeError("deterministic parallel test failure")
        return await super().execute_plan_step(
            build=build,
            step=step,
            completed_artifacts=completed_artifacts,
            idempotency_key=idempotency_key,
            report_remote_operation=report_remote_operation,
        )


class ContaminatedSceneExecutor(FakePlanExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.video_started = False

    async def execute_plan_step(
        self, *, build, step, completed_artifacts, idempotency_key,
        report_remote_operation=None,
    ) -> MediaArtifactVersion:
        if step.output_artifact_type == "video_clip":
            self.video_started = True
        artifact = await super().execute_plan_step(
            build=build,
            step=step,
            completed_artifacts=completed_artifacts,
            idempotency_key=idempotency_key,
            report_remote_operation=report_remote_operation,
        )
        if step.step_id == "scene-setting-reference":
            artifact.metadata.update({
                "final_prompt": step.parameters["prompt"] + " Add the product and cast.",
                "resolved_generation_parameters": {
                    **step.parameters,
                    "images": ["https://media.test/product.png"],
                },
                "skill_prompt_applied": True,
            })
        return artifact

class FakeRebuildExecutor:
    async def rebuild_artifact(
        self, *, build, source, completed_replacements, idempotency_key,
    ) -> MediaArtifactVersion:
        parameters = dict(source.metadata.get("rebuild_parameters") or {})
        return MediaArtifactVersion(
            project_id=build.project_id,
            artifact_id=source.artifact_id,
            type=source.type,
            uri=source.uri,
            metadata={
                **source.metadata,
                "content": parameters.get("content", source.metadata.get("content")),
                "generation_parameters": parameters,
                "rebuild_capability": source.metadata.get("rebuild_capability"),
                "plan_step_id": source.metadata.get("plan_step_id"),
            },
            provenance={"idempotency_key": idempotency_key},
        )


class InitialBuildTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_initial_build_stops_after_active_step_and_can_resume(self):
        runtime = await video_runtime()
        project, version = await runtime.create_project(
            user_id="user", title="Stopped initial build",
        )
        plan = await runtime.plan_project(
            project_id=project.id,
            base_project_version_id=version.id,
            video_spec=video_spec(),
            idempotency_key="stopped-initial-plan",
        )
        build = await runtime.start_build(
            project_id=project.id,
            plan_id=plan.id,
            base_project_version_id=version.id,
            idempotency_key="stopped-initial-build",
        )

        async def validate(*, build_id, artifact):
            return [ValidationResult(
                project_id=project.id,
                build_id=build_id,
                artifact_version_id=artifact.id,
                validator_id="test-continuity",
                passed=True,
            )]

        runtime.validate_artifact = validate
        executor = CancellationSwallowingPlanExecutor()
        task = asyncio.create_task(runtime.execute_build(
            project_id=project.id,
            build_id=build.id,
            executor=executor,
        ))
        runtime._build_tasks[build.id] = task
        await executor.entered.wait()

        stopped = await runtime.cancel_build(project_id=project.id, build_id=build.id)

        self.assertEqual(stopped.status, "cancelled")
        self.assertEqual((await runtime.repo.get_build(project.id, build.id)).status, "cancelled")
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(
            (await runtime.repo.get_project(project.id)).current_version_id,
            version.id,
        )

        await runtime.resume_cancelled_build(project_id=project.id, build_id=build.id)
        resumed_executor = FakePlanExecutor()
        completed, committed = await runtime.execute_build(
            project_id=project.id,
            build_id=build.id,
            executor=resumed_executor,
        )
        self.assertEqual(completed.status, "completed")
        self.assertIsNotNone(committed)
        self.assertNotIn(executor.calls[0], resumed_executor.calls)

    async def test_cancelled_initial_build_cannot_publish_project_version(self):
        runtime = await video_runtime()
        project, version = await runtime.create_project(
            user_id="user", title="Cancelled initial build",
        )
        plan = await runtime.plan_project(
            project_id=project.id,
            base_project_version_id=version.id,
            video_spec=video_spec(),
            idempotency_key="cancelled-initial-plan",
        )
        build = await runtime.start_build(
            project_id=project.id,
            plan_id=plan.id,
            base_project_version_id=version.id,
            idempotency_key="cancelled-initial-build",
        )
        await runtime.repo.cancel_build(project.id, build.id)

        with self.assertRaisesRegex(ValueError, "cannot commit cancelled build"):
            await runtime.repo.commit_initial_build(
                build_id=build.id,
                artifacts=[],
                dependencies=[],
                validation_results=[],
            )

        self.assertEqual(
            (await runtime.repo.get_project(project.id)).current_version_id,
            version.id,
        )

    def test_video_spec_requires_explicit_workflow_selection(self) -> None:
        raw = video_spec().model_dump(mode="json")
        raw.pop("workflow_id")
        with self.assertRaisesRegex(ValueError, "workflow_id"):
            VideoSpec.model_validate(raw)

    async def test_user_artifact_records_the_project_language_contract(self):
        runtime = await video_runtime()
        project, version = await runtime.create_project(
            user_id="user", title="Language metadata",
        )
        spec = video_spec().model_copy(deep=True, update={
            "language": "en-US",
            "language_contract": VideoLanguageContract(
                ui_locale="zh-CN",
                content_language="en-US",
                spoken_language="zh-CN",
                subtitle_language="en-US",
            ),
        })
        plan = RebuildPlan(
            project_id=project.id,
            kind="initial",
            base_project_version_id=version.id,
            workflow_id=spec.workflow_id,
            video_spec=spec,
            items=[RebuildPlanItem(
                step_id="script",
                action="create",
                capability="atomic.text.generate",
                output_artifact_id="script:main",
                output_artifact_type="script",
            )],
        )
        plan = await runtime.repo.save_plan(plan, "language-metadata-plan")
        build = await runtime.start_build(
            project_id=project.id,
            plan_id=plan.id,
            base_project_version_id=version.id,
            idempotency_key="language-metadata-build",
        )
        await runtime.execute_build(
            project_id=project.id,
            build_id=build.id,
            executor=FakePlanExecutor(),
        )
        step = (await runtime.repo.list_build_steps(project.id, build.id))[0]
        artifact = await runtime.repo.get_artifact(
            project.id, step.result_artifact_version_id,
        )
        self.assertEqual(artifact.metadata["language"], "en-US")
        self.assertEqual(artifact.metadata["language_contract"]["ui_locale"], "zh-CN")
        self.assertTrue(artifact.metadata["language_validation"]["passed"])

    async def test_scene_reference_isolation_fails_before_video_generation(self):
        runtime = await video_runtime()
        project, version = await runtime.create_project(
            user_id="user", title="Scene isolation",
        )
        prompt = (
            "Create exactly one unoccupied environment-only setting reference. "
            "Render a single coherent wide architectural view."
        )
        plan = RebuildPlan(
            project_id=project.id,
            kind="initial",
            base_project_version_id=version.id,
            workflow_id="cuti-scenario-product-workflow",
            items=[
                RebuildPlanItem(
                    step_id="scene-setting-reference",
                    action="create",
                    capability="atomic.image.generate",
                    output_artifact_id="scene:main:reference",
                    output_artifact_type="image",
                    parameters={
                        "prompt": prompt,
                        "artifact_role": "scene_setting_reference",
                    },
                ),
                RebuildPlanItem(
                    step_id="validate-setting-references",
                    action="validate",
                    capability="cuti.continuity.validate",
                    output_artifact_type="setting_reference_validation",
                    depends_on=["scene-setting-reference"],
                ),
                RebuildPlanItem(
                    step_id="shot-one-video",
                    action="create",
                    capability="atomic.video.generate",
                    output_artifact_id="shot:one:clip",
                    output_artifact_type="video_clip",
                    depends_on=[
                        "scene-setting-reference",
                        "validate-setting-references",
                    ],
                    execution_group="scenario-product-segments",
                    max_parallelism=2,
                ),
            ],
        )
        plan = await runtime.repo.save_plan(plan, "scene-isolation-plan")
        build = await runtime.start_build(
            project_id=project.id,
            plan_id=plan.id,
            base_project_version_id=version.id,
            idempotency_key="scene-isolation-build",
        )
        validator = ContinuityValidatorPlugin()

        async def validate(*, build_id, artifact):
            return await validator.validate_artifact(
                PluginContext(project_id=project.id, build_id=build_id), artifact,
            )

        runtime.validate_artifact = validate
        executor = ContaminatedSceneExecutor()
        with self.assertRaisesRegex(ValueError, "initial build validation failed"):
            await runtime.execute_build(
                project_id=project.id,
                build_id=build.id,
                executor=executor,
            )
        self.assertFalse(executor.video_started)
        states = {
            item.plan_step_id: item
            for item in await runtime.repo.list_build_steps(project.id, build.id)
        }
        self.assertEqual(states["validate-setting-references"].status, "failed")
        self.assertEqual(states["shot-one-video"].status, "pending")

    async def test_declared_short_drama_segments_execute_in_parallel(self):
        runtime = await video_runtime(max_parallel_generation_tasks=2)
        project, version = await runtime.create_project(user_id="user", title="Parallel drama")
        base = video_spec()
        spec = base.model_copy(deep=True, update={
            "workflow_id": "short-drama-workflow",
            "target_duration_seconds": 45,
            "shots": [
                shot.model_copy(update={"duration_seconds": 15})
                for shot in base.shots
            ],
            "audio": base.audio.model_copy(update={"subtitles": False}),
            "providers": base.providers.model_copy(update={"video": "seedance-2.5"}),
        })
        plan = await runtime.plan_project(
            project_id=project.id,
            base_project_version_id=version.id,
            video_spec=spec,
            idempotency_key="parallel-drama-plan",
        )
        clips = [item for item in plan.items if item.output_artifact_type == "video_clip"]
        self.assertEqual(len(clips), 3)
        self.assertTrue(all(item.execution_group == "short-drama-segments" for item in clips))

        build = await runtime.start_build(
            project_id=project.id,
            plan_id=plan.id,
            base_project_version_id=version.id,
            idempotency_key="parallel-drama-build",
        )

        async def validate(*, build_id, artifact):
            return [ValidationResult(
                project_id=project.id,
                build_id=build_id,
                artifact_version_id=artifact.id,
                validator_id="test",
                passed=True,
            )]

        runtime.validate_artifact = validate
        executor = ConcurrencyProbeExecutor()
        completed, committed = await runtime.execute_build(
            project_id=project.id,
            build_id=build.id,
            executor=executor,
        )
        self.assertEqual(completed.status, "completed")
        self.assertIsNotNone(committed)
        # The Workflow declares all three clips independent, while the
        # deployment-level Cuti safety cap limits live provider work to two.
        self.assertEqual(executor.max_active_group_steps, 2)

    async def test_parallel_failure_preserves_successful_segments_for_retry(self):
        runtime = await video_runtime(max_parallel_generation_tasks=2)
        project, version = await runtime.create_project(user_id="user", title="Recover drama")
        base = video_spec()
        spec = base.model_copy(deep=True, update={
            "workflow_id": "short-drama-workflow",
            "target_duration_seconds": 45,
            "shots": [
                shot.model_copy(update={"duration_seconds": 15})
                for shot in base.shots
            ],
            "audio": base.audio.model_copy(update={"subtitles": False}),
            "providers": base.providers.model_copy(update={"video": "seedance-2.5"}),
        })
        plan = await runtime.plan_project(
            project_id=project.id,
            base_project_version_id=version.id,
            video_spec=spec,
            idempotency_key="recover-parallel-plan",
        )
        build = await runtime.start_build(
            project_id=project.id,
            plan_id=plan.id,
            base_project_version_id=version.id,
            idempotency_key="recover-parallel-build",
        )
        with self.assertRaisesRegex(RuntimeError, "parallel test failure"):
            await runtime.execute_build(
                project_id=project.id,
                build_id=build.id,
                executor=FailOneParallelExecutor("shot-two-video"),
            )
        failed_states = {
            item.plan_step_id: item
            for item in await runtime.repo.list_build_steps(project.id, build.id)
        }
        self.assertEqual(failed_states["shot-one-video"].status, "completed")
        self.assertEqual(failed_states["shot-two-video"].status, "failed")
        self.assertEqual(failed_states["shot-three-video"].status, "completed")

        await runtime.retry_failed_build(project_id=project.id, build_id=build.id)

        async def validate(*, build_id, artifact):
            return [ValidationResult(
                project_id=project.id,
                build_id=build_id,
                artifact_version_id=artifact.id,
                validator_id="test",
                passed=True,
            )]

        runtime.validate_artifact = validate
        retry_executor = FakePlanExecutor()
        completed, committed = await runtime.execute_build(
            project_id=project.id,
            build_id=build.id,
            executor=retry_executor,
        )
        self.assertEqual(completed.status, "completed")
        self.assertIsNotNone(committed)
        self.assertIn("shot-two-video", retry_executor.calls)
        self.assertNotIn("shot-one-video", retry_executor.calls)
        self.assertNotIn("shot-three-video", retry_executor.calls)

    async def test_failed_build_retry_preserves_completed_steps(self):
        runtime = await video_runtime()
        project, version = await runtime.create_project(user_id="user", title="Retry film")
        plan = await runtime.plan_project(
            project_id=project.id,
            base_project_version_id=version.id,
            video_spec=video_spec(),
            idempotency_key="retry-plan",
        )
        build = await runtime.start_build(
            project_id=project.id,
            plan_id=plan.id,
            base_project_version_id=version.id,
            idempotency_key="retry-build",
        )
        steps = await runtime.repo.list_build_steps(project.id, build.id)
        steps[0].status = "completed"
        await runtime.repo.update_build_step(steps[0])
        steps[1].status = "failed"
        steps[1].attempt = 3
        steps[1].error = "temporary provider failure"
        steps[1].remote_operation_id = "terminal-provider-job"
        steps[1].remote_provider = "fake-provider"
        await runtime.repo.update_build_step(steps[1])
        steps[2].status = "running"
        steps[2].attempt = 3
        steps[2].remote_operation_id = "orphaned-provider-job"
        steps[2].remote_provider = "fake-provider"
        await runtime.repo.update_build_step(steps[2])
        build.status = "failed"
        build.error = steps[1].error
        await runtime.repo.update_build(build)

        retried = await runtime.retry_failed_build(project_id=project.id, build_id=build.id)
        states = await runtime.repo.list_build_steps(project.id, build.id)
        self.assertEqual(retried.status, "queued")
        self.assertIsNone(retried.error)
        self.assertEqual(states[0].status, "completed")
        self.assertEqual(states[1].status, "pending")
        self.assertEqual(states[1].attempt, 0)
        self.assertIsNone(states[1].error)
        self.assertIsNone(states[1].remote_operation_id)
        self.assertIsNone(states[1].remote_provider)
        self.assertEqual(states[2].status, "pending")
        self.assertEqual(states[2].attempt, 3)
        self.assertEqual(states[2].remote_operation_id, "orphaned-provider-job")
        self.assertEqual(states[2].remote_provider, "fake-provider")



    async def test_workflow_free_multi_turn_media_plan_patches_reuse_project_artifacts(self):
        runtime = await video_runtime()
        project, _version = await runtime.create_project(user_id="user", title="Media conversation")

        music_plan = await runtime.preview_plan_patch(
            project_id=project.id,
            base_project_version_id=project.current_version_id,
            description="Generate only the requested music",
            idempotency_key="multi-turn-music",
            operations=[MediaEditOperation.model_validate({
                "step_id": "music",
                "capability": "atomic.music.generate",
                "parameters": {"prompt": "Warm instrumental music"},
            })],
        )
        self.assertEqual(music_plan.workflow_id, "")
        self.assertEqual(music_plan.items[0].output_artifact_type, "music")
        self.assertEqual(music_plan.items[0].resolved_skills, [])
        with self.assertRaisesRegex(ValueError, "invalid parameters"):
            await runtime.preview_plan_patch(
                project_id=project.id,
                base_project_version_id=project.current_version_id,
                description="Reject contradictory lyric modes before provider dispatch",
                idempotency_key="invalid-music-lyrics",
                operations=[MediaEditOperation.model_validate({
                    "step_id": "invalid-music",
                    "capability": "atomic.music.generate",
                    "parameters": {
                        "prompt": "Ethereal English song",
                        "has_lyrics": True,
                        "auto_lyrics": True,
                    },
                })],
            )
        music = await runtime.repo.add_artifact(MediaArtifactVersion(
            artifact_id=f"{project.id}:music",
            project_id=project.id,
            type="music",
            uri="https://media.test/music.mp3",
            title="Music",
            metadata={"plan_step_id": "music"},
        ))

        image = await runtime.repo.add_artifact(MediaArtifactVersion(
            artifact_id=f"{project.id}:image",
            project_id=project.id,
            type="image",
            uri="https://media.test/image.png",
            title="Image",
            metadata={"plan_step_id": "image"},
        ))
        project = await runtime.repo.get_project(project.id)
        video_plan = await runtime.preview_plan_patch(
            project_id=project.id,
            base_project_version_id=project.current_version_id,
            description="Generate one video from the existing music and image",
            idempotency_key="multi-turn-video",
            operations=[MediaEditOperation.model_validate({
                "step_id": "video",
                "capability": "atomic.video.generate",
                "inputs": [
                    {"role": "images", "artifact_version_id": image.id},
                    {"role": "audio", "artifact_version_id": music.id},
                ],
                "parameters": {"prompt": "Animate the existing image to the existing music"},
            })],
        )
        video_step = next(item for item in video_plan.items if item.step_id == "video")
        self.assertEqual(video_step.parameters["reference_from_steps"], ["source-1"])
        self.assertEqual(video_step.parameters["audio_reference_from_steps"], ["source-2"])
        self.assertEqual(video_step.resolved_skills, [])

        video = await runtime.repo.add_artifact(MediaArtifactVersion(
            artifact_id=f"{project.id}:video",
            project_id=project.id,
            type="video",
            uri="https://media.test/video.mp4",
            title="Video",
            metadata={"plan_step_id": "video"},
        ))
        project = await runtime.repo.get_project(project.id)
        mix_plan = await runtime.preview_plan_patch(
            project_id=project.id,
            base_project_version_id=project.current_version_id,
            description="Mix the existing music into the existing video",
            idempotency_key="multi-turn-mix",
            operations=[MediaEditOperation.model_validate({
                "step_id": "mixed-video",
                "capability": "media.mix_audio",
                "inputs": [
                    {"role": "video", "artifact_version_id": video.id},
                    {"role": "audio", "artifact_version_id": music.id},
                ],
                "parameters": {"mode": "replace"},
            })],
        )
        mixed = next(item for item in mix_plan.items if item.step_id == "mixed-video")
        self.assertEqual(mixed.parameters["video_step"], "source-1")
        self.assertEqual(mixed.parameters["audio_step"], "source-2")
        self.assertEqual(mixed.action, "rebuild")
        self.assertEqual(mixed.resolved_skills, [])

    async def test_workflow_free_media_plan_patch_adds_subtitles_to_any_video(self):
        runtime = await video_runtime()
        project, version = await runtime.create_project(user_id="user", title="Any workflow")
        selected_video = await runtime.repo.add_artifact(MediaArtifactVersion(
            artifact_id=f"{project.id}:final-video",
            project_id=project.id,
            type="final_video",
            uri="https://media.test/original.mp4",
            title="Original final video",
            metadata={"plan_step_id": "final-video"},
        ))
        project = await runtime.repo.get_project(project.id)

        catalog = {item.capability: item for item in runtime.plan_patch_capability_catalog()}
        self.assertIn("runtime.artifact.persist", catalog)
        self.assertEqual(catalog["runtime.artifact.persist"].estimated_cost, 0)
        self.assertIn("atomic.music.generate", catalog)
        self.assertEqual(catalog["atomic.music.generate"].output_artifact_type, "music")
        self.assertIsNone(catalog["atomic.music.generate"].skill_id)
        self.assertIn("atomic.image.generate", catalog)
        self.assertEqual(catalog["atomic.image.generate"].output_artifact_type, "image")
        self.assertIsNone(catalog["atomic.image.generate"].skill_id)
        image_plan = await runtime.preview_plan_patch(
            project_id=project.id,
            base_project_version_id=project.current_version_id,
            description="Generate one requested image without a workflow",
            idempotency_key="workflow-free-image-preview",
            operations=[MediaEditOperation.model_validate({
                "step_id": "requested-image",
                "capability": "atomic.image.generate",
                "parameters": {"prompt": "A blue fox on a white background"},
            })],
        )
        self.assertEqual(image_plan.workflow_id, "")
        self.assertEqual(image_plan.items[0].output_artifact_type, "image")
        self.assertEqual(image_plan.items[0].resolved_skills, [])
        self.assertIn("media.transcribe", catalog)
        self.assertIn("subtitle.compose", catalog)
        self.assertIn("media.subtitle_burn", catalog)
        self.assertIn("media.audio_analyze", catalog)
        self.assertIn("media.video_analyze", catalog)
        self.assertEqual(catalog["media.video_analyze"].output_artifact_type, "videomap")
        self.assertIn("media.audio_cut", catalog)
        self.assertNotIn("media.audio.trim", catalog)
        self.assertNotIn("media.audio.analyze", catalog)
        self.assertEqual(catalog["media.audio_cut"].output_artifact_type, "audio_cut")
        self.assertIn("segments", catalog["media.audio_cut"].parameters_schema.get("properties", {}))

        plan = await runtime.preview_plan_patch(
            project_id=project.id,
            base_project_version_id=project.current_version_id,
            description="Add clean source-language subtitles",
            idempotency_key="caption-preview",
            operations=[
                MediaEditOperation.model_validate({
                    "step_id": "transcribe",
                    "capability": "media.transcribe",
                    "inputs": [{
                        "role": "video", "artifact_version_id": selected_video.id,
                    }],
                }),
                MediaEditOperation.model_validate({
                    "step_id": "captions",
                    "capability": "subtitle.compose",
                    "inputs": [{
                        "role": "transcript", "operation_step_id": "transcribe",
                    }],
                    "parameters": {"format": "srt", "max_lines": 2},
                }),
                MediaEditOperation.model_validate({
                    "step_id": "burn-captions",
                    "capability": "media.subtitle_burn",
                    "inputs": [
                        {"role": "video", "artifact_version_id": selected_video.id},
                        {"role": "subtitle", "operation_step_id": "captions"},
                    ],
                    "parameters": {
                        "style_preset": "clean", "position": "bottom-safe",
                    },
                }),
            ],
        )
        by_step = {item.step_id: item for item in plan.items}
        self.assertEqual(plan.workflow_id, "")
        self.assertIsNone(plan.video_spec)
        self.assertEqual(by_step["transcribe"].depends_on, ["source-1"])
        self.assertEqual(by_step["captions"].depends_on, ["transcribe"])
        self.assertEqual(
            by_step["burn-captions"].depends_on,
            ["source-1", "captions"],
        )
        self.assertEqual(by_step["burn-captions"].action, "rebuild")
        self.assertEqual(
            by_step["burn-captions"].artifact_version_id, selected_video.id,
        )
        self.assertEqual(
            [item.skill_id for item in by_step["transcribe"].resolved_skills],
            ["subtitle-authoring"],
        )
        self.assertEqual(
            [item.skill_id for item in by_step["captions"].resolved_skills],
            ["subtitle-authoring"],
        )
        self.assertEqual(
            [item.skill_id for item in by_step["burn-captions"].resolved_skills],
            ["subtitle-authoring"],
        )
        self.assertEqual(
            [item.skill_id for item in runtime.plan_patch_capability_catalog() if item.skill_id],
            [
                "hyperframes-captions",
                "hyperframes-core",
                "subtitle-authoring",
                "subtitle-authoring",
                "subtitle-authoring",
            ],
        )
        topological_steps(plan.items)

        build = await runtime.start_build(
            project_id=project.id,
            plan_id=plan.id,
            base_project_version_id=project.current_version_id,
            idempotency_key="caption-build",
        )
        completed, new_version = await runtime.execute_build(
            project_id=project.id,
            build_id=build.id,
            executor=FakePlanExecutor(),
        )
        self.assertEqual(completed.status, "completed")
        self.assertIsNotNone(new_version)
        current = await runtime.repo.current_artifacts(project.id)
        captioned = next(item for item in current if item.artifact_id == selected_video.artifact_id)
        self.assertNotEqual(captioned.id, selected_video.id)
        self.assertEqual(captioned.version, selected_video.version + 1)
        dependencies = await runtime.repo.current_dependencies(project.id)
        self.assertTrue(dependencies)
        self.assertFalse(any(
            item.source_version_id == item.target_version_id
            for item in dependencies
        ))
        self.assertFalse(any(
            item.source_version_id == captioned.id
            and item.target_version_id != captioned.id
            for item in dependencies
        ))

    async def test_workflow_free_concat_ignores_stale_generation_workflow_lock(self):
        runtime = await video_runtime()
        project, _version = await runtime.create_project(
            user_id="user", title="Finish an existing build",
        )
        workflow_id = "cuti-scenario-product-workflow"
        if not runtime.skills.catalog.has(workflow_id):
            self.skipTest(f"{workflow_id} Skill is not installed")
        lock = await runtime.set_project_skill_enabled(
            project_id=project.id,
            skill_id=workflow_id,
            enabled=True,
        )
        await runtime.repo.upsert_project_skill_lock(
            lock.model_copy(update={"digest": "stale-project-lock"}),
        )
        clips = []
        for number in (1, 2):
            clips.append(await runtime.repo.add_artifact(MediaArtifactVersion(
                artifact_id=f"{project.id}:segment-{number}",
                project_id=project.id,
                type="video_clip",
                uri=f"https://media.test/segment-{number}.mp4",
            )))
        project = await runtime.repo.get_project(project.id)

        plan = await runtime.preview_plan_patch(
            project_id=project.id,
            base_project_version_id=project.current_version_id,
            description="Join the completed clips",
            idempotency_key="concat-stale-workflow-lock",
            operations=[MediaEditOperation.model_validate({
                "step_id": "concat-segments",
                "capability": "media.concat",
                "inputs": [
                    {"role": "videos", "artifact_version_id": item.id}
                    for item in clips
                ],
                "parameters": {"normalize": True, "transition_duration": 0},
            })],
        )

        concat = next(item for item in plan.items if item.step_id == "concat-segments")
        self.assertEqual(plan.workflow_id, "")
        self.assertEqual(concat.depends_on, ["source-1", "source-2"])
        self.assertEqual(concat.resolved_skills, [])

    async def test_timeline_edit_can_author_new_shots_for_duration_extension(self):
        runtime = await video_runtime()
        project, _version = await runtime.create_project(user_id="user", title="Extend duration")
        spec = video_spec()
        await runtime.repo.add_artifact(MediaArtifactVersion(
            artifact_id=f"{project.id}:spec",
            project_id=project.id,
            type="video_spec",
            metadata={"content": spec.model_dump(mode="json") | {"execution_tasks": [{"step_id": "done", "status": "succeeded"}]}, "plan_step_id": "spec"},
        ))
        await runtime.repo.add_artifact(MediaArtifactVersion(
            artifact_id=f"{project.id}:seedance-prompts",
            project_id=project.id,
            type="shot_plan",
            metadata={"plan_step_id": "seedance-prompts"},
        ))
        for shot in spec.shots:
            await runtime.repo.add_artifact(MediaArtifactVersion(
                artifact_id=f"{project.id}:shot:{shot.id}",
                project_id=project.id,
                type="video_clip",
                uri=f"https://media.test/{shot.id}.mp4",
                metadata={"plan_step_id": f"shot-{shot.id}-video"},
            ))
        project = await runtime.repo.get_project(project.id)
        shot_patches = [
            shot.model_dump(mode="json") | {"duration_seconds": 15}
            for shot in spec.shots
        ]
        shot_patches.append({
            "id": "four",
            "order": 4,
            "duration_seconds": 15,
            "beat": "returns",
            "visual_prompt": "0-15秒，主角回到站台，镜头完成故事收束。",
            "character_ids": ["hero"],
        })

        plan = await runtime.preview_edits(
            project_id=project.id,
            base_project_version_id=project.current_version_id,
            description="Extend to one minute",
            idempotency_key="extend-duration-preview",
            edits=[{
                "type": "patch_timeline",
                "patch": {
                    "target_duration_seconds": 60,
                    "shots": shot_patches,
                },
            }],
        )

        self.assertEqual(plan.video_spec.target_duration_seconds, 60)
        self.assertEqual(len(plan.video_spec.shots), 4)
        new_shot = next(item for item in plan.items if item.step_id == "shot-four-video")
        self.assertEqual(new_shot.action, "create")
        topological_steps(plan.items)

    async def test_dynamic_edits_allow_matching_url_and_chained_transforms(self):
        runtime = await video_runtime()
        project, version = await runtime.create_project(user_id="user", title="Repeated edits")
        source = await runtime.repo.add_artifact(MediaArtifactVersion(
            artifact_id="source", project_id=project.id, type="video",
            uri="https://media.test/source.mp4",
        ))
        project = await runtime.repo.get_project(project.id)
        plan = await runtime.preview_plan_patch(
            project_id=project.id, base_project_version_id=project.current_version_id,
            operations=[MediaEditOperation.model_validate(item) for item in [
                {"step_id": "first", "capability": "media.hyperframes_caption",
                 "inputs": [{"role": "video", "artifact_version_id": source.id}],
                 "parameters": {"video_url": source.uri, "caption_html": "<body>First</body>"}},
                {"step_id": "second", "capability": "media.hyperframes_caption",
                 "inputs": [{"role": "video", "operation_step_id": "first"}],
                 "parameters": {"caption_html": "<body>Second</body>"}},
            ]],
        )
        first = next(item for item in plan.items if item.step_id == "first")
        second = next(item for item in plan.items if item.step_id == "second")
        self.assertNotIn("video_url", first.parameters)
        self.assertIn("first", second.depends_on)
        self.assertNotEqual(first.output_artifact_id, second.output_artifact_id)
        topological_steps(plan.items)

    async def test_dynamic_patch_can_generate_video_then_concat(self):
        runtime = await video_runtime()
        project, _ = await runtime.create_project(user_id="user", title="Append clip")
        source = await runtime.repo.add_artifact(MediaArtifactVersion(
            artifact_id="source", project_id=project.id, type="video", uri="https://media.test/source.mp4",
        ))
        reference = await runtime.repo.add_artifact(MediaArtifactVersion(
            artifact_id="reference", project_id=project.id, type="image", uri="https://media.test/reference.png",
        ))
        project = await runtime.repo.get_project(project.id)
        with self.assertRaisesRegex(ValueError, "Accepted input roles: audio, images"):
            await runtime.preview_plan_patch(
                project_id=project.id, base_project_version_id=project.current_version_id,
                operations=[MediaEditOperation.model_validate({
                    "step_id": "invalid-reference", "capability": "api.provider.generate",
                    "inputs": [{"role": "reference", "artifact_version_id": reference.id}],
                    "parameters": {"prompt": "A bottle on a table"},
                })],
            )
        plan = await runtime.preview_plan_patch(
            project_id=project.id, base_project_version_id=project.current_version_id,
            operations=[MediaEditOperation.model_validate(item) for item in [
                {"step_id": "new-video", "capability": "api.provider.generate", "inputs": [
                    {"role": "image", "artifact_version_id": reference.id}],
                 "parameters": {"provider": "ark", "model": "seedance-2.5", "duration": 5,
                                "prompt": "0-5 seconds: a slow cinematic camera push toward a bottle on a wooden table, warm lighting, no dialogue or text."}},
                {"step_id": "append", "capability": "media.concat", "inputs": [
                    {"role": "videos", "artifact_version_id": source.id},
                    {"role": "videos", "operation_step_id": "new-video"}],
                 "parameters": {"video_steps": ["redundant-source", "new-video"], "transition_duration": 0}},
            ]],
        )
        appended = next(item for item in plan.items if item.step_id == "append")
        self.assertIn("new-video", appended.depends_on)
        self.assertNotIn("redundant-source", appended.parameters["video_steps"])
        self.assertEqual(plan.workflow_id, "")
        topological_steps(plan.items)

    async def test_dynamic_media_patch_rejects_direct_urls_and_unselected_artifacts(self):
        runtime = await video_runtime()
        project, version = await runtime.create_project(user_id="user", title="Secure edits")
        with self.assertRaisesRegex(ValueError, "direct resource references"):
            await runtime.preview_plan_patch(
                project_id=project.id,
                base_project_version_id=version.id,
                idempotency_key="bad-url",
                operations=[MediaEditOperation.model_validate({
                    "step_id": "transcribe", "capability": "media.transcribe",
                    "inputs": [{"role": "video", "artifact_version_id": "missing"}],
                    "parameters": {"video_url": "https://attacker.invalid/video.mp4"},
                })],
            )

    async def test_hyperframes_render_requires_persisted_code_video_mode(self):
        runtime = await video_runtime()
        project, version = await runtime.create_project(
            user_id="user", title="Local code video",
        )
        operation = MediaEditOperation.model_validate({
            "step_id": "code-video",
            "capability": "media.hyperframes_render",
            "parameters": {
                "composition_html": (
                    '<!doctype html><html><body><main data-composition-id="code-video" '
                    'data-duration="5" data-fps="30" data-width="1280" '
                    'data-height="720">Hello</main></body></html>'
                ),
                "width": 1280,
                "height": 720,
                "duration": 5,
                "fps": 30,
            },
        })

        with self.assertRaisesRegex(ValueError, "Code-to-Video mode"):
            await runtime.preview_plan_patch(
                project_id=project.id,
                base_project_version_id=version.id,
                operations=[operation],
            )

        await runtime.repo.remember_operation_result(
            project.id,
            "compat-run-context",
            "initial",
            '{"code_video_mode": true}',
        )
        plan = await runtime.preview_plan_patch(
            project_id=project.id,
            base_project_version_id=version.id,
            operations=[operation],
        )
        self.assertEqual(plan.items[0].capability, "media.hyperframes_render")
        with self.assertRaisesRegex(LookupError, "currently selected"):
            await runtime.preview_plan_patch(
                project_id=project.id,
                base_project_version_id=version.id,
                idempotency_key="bad-artifact",
                operations=[MediaEditOperation.model_validate({
                    "step_id": "transcribe", "capability": "media.transcribe",
                    "inputs": [{"role": "video", "artifact_version_id": "missing"}],
                })],
            )

    def test_plan_rejects_cycle(self):
        with self.assertRaises(BuildPlanValidationError):
            topological_steps([
                RebuildPlanItem(step_id="a", action="create", depends_on=["b"]),
                RebuildPlanItem(step_id="b", action="create", depends_on=["a"]),
            ])

    def test_spec_rejects_unknown_character(self):
        raw = video_spec().model_dump()
        raw["shots"][0]["character_ids"] = ["missing"]
        with self.assertRaises(ValueError):
            VideoSpec.model_validate(raw)
