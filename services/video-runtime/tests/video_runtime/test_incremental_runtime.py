from __future__ import annotations

import asyncio
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.video_runtime import (
    ArtifactDependency,
    ArtifactInvalidationPolicy,
    MediaArtifactVersion,
    ProjectVersionConflict,
    VideoBuildRuntime,
)
from app.video_runtime.models import ValidationResult


class DeterministicExecutor:
    async def rebuild_artifact(
        self, *, build, source, completed_replacements, idempotency_key,
    ):
        return MediaArtifactVersion(
            project_id=build.project_id,
            type=source.type,
            uri=f"rebuilt-{source.id}",
            provenance={
                "idempotency_key": idempotency_key,
                "upstream_replacements": sorted(completed_replacements),
            },
        )


class RecordingExecutor(DeterministicExecutor):
    def __init__(self):
        self.calls = []

    async def rebuild_artifact(self, **kwargs):
        self.calls.append(kwargs["source"].id)
        return await super().rebuild_artifact(**kwargs)


class CancellationSwallowingExecutor(RecordingExecutor):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()

    async def rebuild_artifact(self, **kwargs):
        self.calls.append(kwargs["source"].id)
        if len(self.calls) == 1:
            self.entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass
        return await DeterministicExecutor.rebuild_artifact(self, **kwargs)


async def _project_fixture():
    runtime = VideoBuildRuntime()
    project, _ = await runtime.create_project(user_id="user-1", title="Three shots")
    character = await runtime.add_artifact(MediaArtifactVersion(
        project_id=project.id, type="character", title="Hero", uri="character-v1.png",
    ))
    shot1 = await runtime.add_artifact(MediaArtifactVersion(
        project_id=project.id, type="shot", title="Shot 1", uri="shot-1-v1.mp4",
    ))
    shot2 = await runtime.add_artifact(MediaArtifactVersion(
        project_id=project.id, type="shot", title="Shot 2", uri="shot-2-v1.mp4",
    ))
    keyframe3 = await runtime.add_artifact(MediaArtifactVersion(
        project_id=project.id, type="keyframe", title="Shot 3 keyframe", uri="shot-3-keyframe-v1.png",
    ), [ArtifactDependency(
        project_id=project.id,
        source_version_id=character.id,
        target_version_id="placeholder",
        invalidation_policy=ArtifactInvalidationPolicy.HARD,
    )])
    # The dependency target is only known after the version is created in this in-memory adapter.
    runtime.repo.dependencies[project.id][-1].target_version_id = keyframe3.id
    clip3 = await runtime.add_artifact(MediaArtifactVersion(
        project_id=project.id, type="shot", title="Shot 3", uri="shot-3-v1.mp4",
    ), [ArtifactDependency(
        project_id=project.id,
        source_version_id=keyframe3.id,
        target_version_id="placeholder",
        invalidation_policy=ArtifactInvalidationPolicy.HARD,
    )])
    runtime.repo.dependencies[project.id][-1].target_version_id = clip3.id
    timeline = await runtime.add_artifact(MediaArtifactVersion(
        project_id=project.id, type="timeline", title="Timeline", uri="timeline-v1.json",
    ), [
        ArtifactDependency(
            project_id=project.id,
            source_version_id=clip3.id,
            target_version_id="placeholder",
            invalidation_policy=ArtifactInvalidationPolicy.VALIDATE,
        ),
        ArtifactDependency(
            project_id=project.id,
            source_version_id=shot1.id,
            target_version_id="placeholder",
            invalidation_policy=ArtifactInvalidationPolicy.SOFT,
        ),
        ArtifactDependency(
            project_id=project.id,
            source_version_id=shot2.id,
            target_version_id="placeholder",
            invalidation_policy=ArtifactInvalidationPolicy.NONE,
        ),
    ])
    for edge in runtime.repo.dependencies[project.id][-3:]:
        edge.target_version_id = timeline.id
    return runtime, project.id, character, shot1, shot2, keyframe3, clip3, timeline


class IncrementalRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_build_stays_stopped_when_step_swallows_cancellation(self):
        runtime, project_id, character, *_ = await _project_fixture()
        project = await runtime.repo.get_project(project_id)
        original_version_id = project.current_version_id
        plan = await runtime.preview_change(
            project_id=project_id,
            description="Stop after the active rebuild step",
            target_artifact_version_ids=[character.id],
        )
        build = await runtime.apply_rebuild(
            project_id=project_id,
            plan_id=plan.id,
            base_project_version_id=original_version_id,
            idempotency_key="cancel-swallowed-step",
        )

        async def validate(*, build_id, artifact):
            return [ValidationResult(
                project_id=project_id,
                build_id=build_id,
                artifact_version_id=artifact.id,
                validator_id="test-continuity",
                passed=True,
            )]

        runtime.validate_artifact = validate
        executor = CancellationSwallowingExecutor()
        task = asyncio.create_task(runtime.execute_build(
            project_id=project_id,
            build_id=build.id,
            executor=executor,
        ))
        runtime._build_tasks[build.id] = task
        await executor.entered.wait()
        stopped = await runtime.cancel_build(project_id=project_id, build_id=build.id)

        self.assertEqual(stopped.status, "cancelled")
        self.assertEqual((await runtime.repo.get_build(project_id, build.id)).status, "cancelled")
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(
            (await runtime.repo.get_project(project_id)).current_version_id,
            original_version_id,
        )

        await runtime.resume_cancelled_build(project_id=project_id, build_id=build.id)
        resumed_executor = RecordingExecutor()
        completed, version = await runtime.execute_build(
            project_id=project_id,
            build_id=build.id,
            executor=resumed_executor,
        )
        self.assertEqual(completed.status, "completed")
        self.assertNotEqual(version.id, original_version_id)
        self.assertEqual(len(resumed_executor.calls), 2)

    async def test_stale_update_cannot_overwrite_terminal_build(self):
        runtime, project_id, character, *_ = await _project_fixture()
        project = await runtime.repo.get_project(project_id)
        plan = await runtime.preview_change(
            project_id=project_id,
            description="Cancel stale update test",
            target_artifact_version_ids=[character.id],
        )
        build = await runtime.apply_rebuild(
            project_id=project_id,
            plan_id=plan.id,
            base_project_version_id=project.current_version_id,
            idempotency_key="cancel-stale-update",
        )
        step = (await runtime.repo.list_build_steps(project_id, build.id))[0]
        stale_step = step.model_copy(update={"status": "running"})
        step.status = "completed"
        await runtime.repo.update_build_step(step)
        stale_step.status = "failed"
        stale_step.error = "late worker failure"

        step_result = await runtime.repo.update_build_step(stale_step)

        self.assertEqual(step_result.status, "completed")
        self.assertEqual(
            (await runtime.repo.list_build_steps(project_id, build.id))[0].status,
            "completed",
        )
        stale = build.model_copy(update={"status": "running"})
        await runtime.repo.cancel_build(project_id, build.id)

        result = await runtime.repo.update_build(stale)

        self.assertEqual(result.status, "cancelled")
        self.assertEqual((await runtime.repo.get_build(project_id, build.id)).status, "cancelled")

    async def test_execute_build_runs_in_dependency_order_and_commits_once(self):
        runtime, project_id, character, shot1, shot2, keyframe3, clip3, timeline = await _project_fixture()
        project = await runtime.repo.get_project(project_id)
        plan = await runtime.preview_change(
            project_id=project_id,
            description="Change the hero clothing in shot 3",
            target_artifact_version_ids=[character.id],
        )
        build = await runtime.apply_rebuild(
            project_id=project_id,
            plan_id=plan.id,
            base_project_version_id=project.current_version_id,
            idempotency_key="execute-build-1",
        )

        async def validate(*, build_id, artifact):
            return [ValidationResult(
                project_id=project_id,
                build_id=build_id,
                artifact_version_id=artifact.id,
                validator_id="test-continuity",
                passed=True,
            )]

        runtime.validate_artifact = validate
        completed, version = await runtime.execute_build(
            project_id=project_id,
            build_id=build.id,
            executor=DeterministicExecutor(),
        )
        self.assertEqual(completed.status, "completed")
        self.assertEqual(version.selections[shot1.artifact_id], shot1.id)
        self.assertEqual(version.selections[shot2.artifact_id], shot2.id)
        self.assertNotEqual(version.selections[keyframe3.artifact_id], keyframe3.id)
        self.assertNotEqual(version.selections[clip3.artifact_id], clip3.id)
        repeated, repeated_version = await runtime.execute_build(
            project_id=project_id,
            build_id=build.id,
            executor=DeterministicExecutor(),
        )
        self.assertEqual(repeated.id, completed.id)
        self.assertEqual(repeated_version.id, version.id)

    async def test_character_change_rebuilds_only_affected_shot_and_commits_atomically(self):
        runtime, project_id, character, shot1, shot2, keyframe3, clip3, timeline = await _project_fixture()
        before = await runtime.repo.get_project(project_id)
        plan = await runtime.preview_change(
            project_id=project_id,
            description="Change the hero clothing in shot 3",
            target_artifact_version_ids=[character.id],
        )

        self.assertEqual(plan.ids_for("rebuild"), [character.id, keyframe3.id, clip3.id])
        self.assertEqual(plan.ids_for("validate"), [timeline.id])
        self.assertLessEqual({shot1.id, shot2.id}, set(plan.ids_for("reuse")))

        build = await runtime.apply_rebuild(
            project_id=project_id,
            plan_id=plan.id,
            base_project_version_id=before.current_version_id,
            idempotency_key="wardrobe-change-1",
        )
        replacements = {
            old.id: MediaArtifactVersion(project_id=project_id, type=old.type, uri=f"{old.type}-v2")
            for old in (character, keyframe3, clip3)
        }
        validation = ValidationResult(
            project_id=project_id,
            build_id=build.id,
            artifact_version_id=timeline.id,
            validator_id="timeline-continuity",
            passed=True,
        )
        _completed, version = await runtime.complete_build(
            build_id=build.id, replacements=replacements, validation_results=[validation],
        )

        self.assertEqual(version.parent_version_id, before.current_version_id)
        self.assertEqual(version.selections[shot1.artifact_id], shot1.id)
        self.assertEqual(version.selections[shot2.artifact_id], shot2.id)
        self.assertNotEqual(version.selections[character.artifact_id], character.id)
        self.assertEqual((await runtime.repo.get_project(project_id)).current_version_id, version.id)


    async def test_rebuild_is_idempotent_and_stale_commit_conflicts(self):
        runtime, project_id, character, *_ = await _project_fixture()
        project = await runtime.repo.get_project(project_id)
        plan = await runtime.preview_change(
            project_id=project_id, description="change", target_artifact_version_ids=[character.id],
        )
        first = await runtime.apply_rebuild(
            project_id=project_id, plan_id=plan.id,
            base_project_version_id=project.current_version_id, idempotency_key="same-key",
        )
        second = await runtime.apply_rebuild(
            project_id=project_id, plan_id=plan.id,
            base_project_version_id=project.current_version_id, idempotency_key="same-key",
        )
        self.assertEqual(first.id, second.id)

        # A separately committed selection advances the project, so this old build cannot commit.
        current_artifact = (await runtime.repo.current_artifacts(project_id))[0]
        await runtime.select_artifact(
            project_id=project_id, version_id=current_artifact.id,
            base_project_version_id=project.current_version_id, idempotency_key="advance-version",
        )
        with self.assertRaises(ProjectVersionConflict):
            await runtime.complete_build(build_id=first.id, replacements={}, validation_results=[])
