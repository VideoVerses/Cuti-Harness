from __future__ import annotations

import asyncio
from collections import defaultdict
from copy import deepcopy
from datetime import timedelta
from typing import NamedTuple

from .models import (
    ArtifactDependency,
    Build,
    BuildPlanRevision,
    BuildStep,
    ChangeRequest,
    ExportRecord,
    MediaArtifactVersion,
    Project,
    PlanCheckpoint,
    ProjectEvent,
    ProjectSessionBinding,
    ProjectSkillLock,
    ProjectVersion,
    RebuildPlan,
    VideoSpecRevision,
    ValidationResult,
    now,
    uid,
)


class ProjectVersionConflict(RuntimeError):
    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(f"project version changed: expected {expected}, current {actual}")
        self.expected = expected
        self.actual = actual


class PlanRevisionConflict(RuntimeError):
    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"build plan revision changed: expected {expected}, current {actual}")
        self.expected = expected
        self.actual = actual


class VideoSpecRevisionConflict(RuntimeError):
    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"VideoSpec revision changed: expected {expected}, current {actual}")
        self.expected = expected
        self.actual = actual


class SidebarRun(NamedTuple):
    project: Project
    session_id: str
    latest_build_status: str | None
    context_json: str | None


class InMemoryVideoProjectRepository:
    """Reference repository with transaction-equivalent lock semantics for tests and local use."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.projects: dict[str, Project] = {}
        self.versions: dict[str, ProjectVersion] = {}
        self.project_versions: dict[str, list[str]] = defaultdict(list)
        self.artifacts: dict[str, dict[str, MediaArtifactVersion]] = defaultdict(dict)
        self.dependencies: dict[str, list[ArtifactDependency]] = defaultdict(list)
        self.bindings: dict[tuple[str, str], ProjectSessionBinding] = {}
        self.skill_locks: dict[str, dict[str, ProjectSkillLock]] = defaultdict(dict)
        self.changes: dict[str, ChangeRequest] = {}
        self.plans: dict[str, RebuildPlan] = {}
        self.video_spec_revisions: dict[str, VideoSpecRevision] = {}
        self.project_spec_revisions: dict[str, list[str]] = defaultdict(list)
        self.plan_revisions: dict[str, list[BuildPlanRevision]] = defaultdict(list)
        self.checkpoints: dict[str, PlanCheckpoint] = {}
        self.build_checkpoints: dict[str, list[str]] = defaultdict(list)
        self.builds: dict[str, Build] = {}
        self.build_steps: dict[str, dict[str, BuildStep]] = defaultdict(dict)
        self.validations: dict[str, list[ValidationResult]] = defaultdict(list)
        self.exports: dict[str, ExportRecord] = {}
        self.events: dict[str, list[ProjectEvent]] = defaultdict(list)
        self.idempotency: dict[tuple[str, str, str], str] = {}
        self.project_creation_idempotency: dict[tuple[str, str], str] = {}
        self.compatibility_runs: dict[tuple[str, str], tuple[str, str]] = {}

    async def create_project(self, project: Project) -> tuple[Project, ProjectVersion]:
        async with self.lock:
            if project.id in self.projects:
                raise ValueError("project already exists")
            version = ProjectVersion(project_id=project.id)
            project.current_version_id = version.id
            self.projects[project.id] = deepcopy(project)
            self.versions[version.id] = deepcopy(version)
            self.project_versions[project.id].append(version.id)
            self._append_event(project.id, "project.created", {"project_version_id": version.id})
            return deepcopy(project), deepcopy(version)

    async def create_project_idempotently(
        self, project: Project, idempotency_key: str,
    ) -> tuple[Project, ProjectVersion]:
        async with self.lock:
            existing_id = self.project_creation_idempotency.get(
                (project.user_id, idempotency_key),
            )
            if existing_id is not None:
                existing = self.projects[existing_id]
                version = self.versions[existing.current_version_id]
                return deepcopy(existing), deepcopy(version)
            version = ProjectVersion(project_id=project.id)
            project.current_version_id = version.id
            self.projects[project.id] = deepcopy(project)
            self.versions[version.id] = deepcopy(version)
            self.project_versions[project.id].append(version.id)
            self.project_creation_idempotency[(project.user_id, idempotency_key)] = project.id
            self._append_event(project.id, "project.created", {"project_version_id": version.id})
            return deepcopy(project), deepcopy(version)

    async def get_project(self, project_id: str) -> Project:
        project = self.projects.get(project_id)
        if project is None:
            raise LookupError("project not found")
        return deepcopy(project)

    async def list_projects(self, user_id: str, *, limit: int | None = None) -> list[Project]:
        projects = sorted(
            (deepcopy(item) for item in self.projects.values() if item.user_id == user_id),
            key=lambda item: item.updated_at,
            reverse=True,
        )
        return projects[:limit] if limit is not None else projects

    async def list_sidebar_runs(self, user_id: str, *, limit: int = 50) -> list[SidebarRun]:
        rows: list[SidebarRun] = []
        for project in await self.list_projects(user_id):
            if len(rows) >= limit:
                break
            try:
                binding = await self.latest_session_binding(project.id, user_id)
            except LookupError:
                continue
            builds = [item for item in self.builds.values() if item.project_id == project.id]
            latest = max(builds, key=lambda item: item.created_at) if builds else None
            encoded = self.idempotency.get((project.id, "compat-run-context", "initial"))
            rows.append(SidebarRun(
                project=deepcopy(project),
                session_id=binding.session_id,
                latest_build_status=latest.status if latest is not None else None,
                context_json=encoded if isinstance(encoded, str) else None,
            ))
        return rows

    async def list_all_projects(self) -> list[Project]:
        return [deepcopy(item) for item in self.projects.values()]

    async def get_project_version(self, version_id: str) -> ProjectVersion:
        version = self.versions.get(version_id)
        if version is None:
            raise LookupError("project version not found")
        return deepcopy(version)

    async def list_project_versions(self, project_id: str) -> list[ProjectVersion]:
        return [deepcopy(self.versions[item]) for item in self.project_versions[project_id]]

    async def bind_session(self, binding: ProjectSessionBinding) -> ProjectSessionBinding:
        project = await self.get_project(binding.project_id)
        if project.user_id != binding.user_id:
            raise PermissionError("project does not belong to user")
        async with self.lock:
            key = (binding.project_id, binding.session_id)
            existing = self.bindings.get(key)
            if existing is not None:
                return deepcopy(existing)
            self.bindings[key] = deepcopy(binding)
            self._append_event(binding.project_id, "project.session_bound", {"session_id": binding.session_id})
        return deepcopy(binding)

    async def latest_session_binding(
        self, project_id: str, user_id: str,
    ) -> ProjectSessionBinding:
        matches = [
            item for item in self.bindings.values()
            if item.project_id == project_id and item.user_id == user_id
        ]
        if not matches:
            raise LookupError("project has no DeepSeek Session binding")
        return deepcopy(max(matches, key=lambda item: item.created_at))

    async def project_for_session(
        self, session_id: str, user_id: str,
    ) -> tuple[Project, ProjectSessionBinding]:
        matches = [
            item for item in self.bindings.values()
            if item.session_id == session_id and item.user_id == user_id
        ]
        if not matches:
            raise LookupError("DeepSeek Session is not bound to a project")
        binding = max(matches, key=lambda item: item.created_at)
        return await self.get_project(binding.project_id), deepcopy(binding)

    async def list_project_skill_locks(self, project_id: str) -> list[ProjectSkillLock]:
        await self.get_project(project_id)
        return sorted(
            (deepcopy(item) for item in self.skill_locks[project_id].values()),
            key=lambda item: item.skill_id,
        )

    async def upsert_project_skill_lock(
        self, lock: ProjectSkillLock,
    ) -> ProjectSkillLock:
        async with self.lock:
            self._project(lock.project_id)
            lock.updated_at = now()
            self.skill_locks[lock.project_id][lock.skill_id] = deepcopy(lock)
            self._append_event(lock.project_id, "project.skill_lock.updated", {
                "skill_id": lock.skill_id,
                "version": lock.version,
                "digest": lock.digest,
                "source": lock.source,
                "enabled": lock.enabled,
            })
            return deepcopy(lock)

    async def get_compatibility_run(
        self, user_id: str, idempotency_key: str,
    ) -> tuple[str, str] | None:
        return self.compatibility_runs.get((user_id, idempotency_key))

    async def remember_compatibility_run(
        self,
        *,
        user_id: str,
        idempotency_key: str,
        project_id: str,
        session_id: str,
    ) -> None:
        async with self.lock:
            self.compatibility_runs.setdefault(
                (user_id, idempotency_key), (project_id, session_id),
            )

    async def get_operation_result(
        self, project_id: str, operation: str, idempotency_key: str,
    ) -> str | None:
        return self.idempotency.get((project_id, operation, idempotency_key))

    async def remember_operation_result(
        self,
        project_id: str,
        operation: str,
        idempotency_key: str,
        result_id: str,
    ) -> None:
        async with self.lock:
            self.idempotency.setdefault(
                (project_id, operation, idempotency_key), result_id,
            )

    async def replace_operation_result(
        self,
        project_id: str,
        operation: str,
        idempotency_key: str,
        result_id: str,
    ) -> None:
        async with self.lock:
            self.idempotency[(project_id, operation, idempotency_key)] = result_id

    async def add_artifact(
        self, artifact: MediaArtifactVersion, dependencies: list[ArtifactDependency] | None = None,
    ) -> MediaArtifactVersion:
        async with self.lock:
            project = self._project(artifact.project_id)
            same = [
                item.version for item in self.artifacts[artifact.project_id].values()
                if item.artifact_id == artifact.artifact_id
            ]
            artifact.version = max(same, default=0) + 1
            self.artifacts[artifact.project_id][artifact.id] = deepcopy(artifact)
            if dependencies:
                self.dependencies[artifact.project_id].extend(deepcopy(dependencies))
            current = self.versions[project.current_version_id]
            selections = {**current.selections, artifact.artifact_id: artifact.id}
            version = ProjectVersion(
                project_id=project.id,
                parent_version_id=current.id,
                timeline_version_id=(artifact.id if artifact.type == "timeline" else current.timeline_version_id),
                selections=selections,
                video_spec_revision_id=current.video_spec_revision_id,
            )
            self._commit_version(project, version)
            self._append_event(project.id, "artifact.committed", {
                "artifact_id": artifact.artifact_id,
                "artifact_version_id": artifact.id,
                "project_version_id": version.id,
            })
            return deepcopy(artifact)

    async def current_artifacts(self, project_id: str) -> list[MediaArtifactVersion]:
        project = await self.get_project(project_id)
        version = self.versions[project.current_version_id]
        return [
            deepcopy(self.artifacts[project_id][version_id])
            for version_id in version.selections.values()
        ]

    async def list_artifact_versions(self, project_id: str) -> list[MediaArtifactVersion]:
        await self.get_project(project_id)
        return sorted(
            (deepcopy(item) for item in self.artifacts[project_id].values()),
            key=lambda item: item.created_at,
        )

    async def current_dependencies(self, project_id: str) -> list[ArtifactDependency]:
        current_ids = {item.id for item in await self.current_artifacts(project_id)}
        return deepcopy([
            edge for edge in self.dependencies[project_id]
            if edge.source_version_id in current_ids and edge.target_version_id in current_ids
        ])

    async def save_change_and_plan(
        self, change: ChangeRequest, plan: RebuildPlan, idempotency_key: str | None = None,
    ) -> RebuildPlan:
        async with self.lock:
            project = self._project(change.project_id)
            if idempotency_key:
                existing_id = self.idempotency.get(
                    (change.project_id, "preview", idempotency_key),
                )
                if existing_id is not None:
                    return deepcopy(self.plans[existing_id])
            self._assert_version(project, change.base_project_version_id)
            self.changes[change.id] = deepcopy(change)
            self.plans[plan.id] = deepcopy(plan)
            if idempotency_key:
                self.idempotency[(change.project_id, "preview", idempotency_key)] = plan.id
            self._append_event(project.id, "rebuild.previewed", {
                "change_request_id": change.id,
                "plan_id": plan.id,
                "base_project_version_id": plan.base_project_version_id,
            })
            return deepcopy(plan)

    async def save_plan(
        self, plan: RebuildPlan, idempotency_key: str,
    ) -> RebuildPlan:
        async with self.lock:
            project = self._project(plan.project_id)
            key = (plan.project_id, "plan", idempotency_key)
            existing_id = self.idempotency.get(key)
            if existing_id is not None:
                return deepcopy(self.plans[existing_id])
            self._assert_version(project, plan.base_project_version_id)
            self.plans[plan.id] = deepcopy(plan)
            self.idempotency[key] = plan.id
            self._append_event(project.id, "build.plan_created", {
                "plan_id": plan.id, "kind": plan.kind,
                "estimated_cost": plan.estimated_cost,
            })
            return deepcopy(plan)

    async def save_staged_plan(
        self,
        plan: RebuildPlan,
        spec_revision: VideoSpecRevision,
        plan_revision: BuildPlanRevision,
        idempotency_key: str,
    ) -> RebuildPlan:
        async with self.lock:
            project = self._project(plan.project_id)
            key = (plan.project_id, "plan", idempotency_key)
            existing_id = self.idempotency.get(key)
            if existing_id is not None:
                return deepcopy(self.plans[existing_id])
            self._assert_version(project, plan.base_project_version_id)
            self.video_spec_revisions[spec_revision.id] = deepcopy(spec_revision)
            self.project_spec_revisions[plan.project_id].append(spec_revision.id)
            self.plan_revisions[plan.id].append(deepcopy(plan_revision))
            self.plans[plan.id] = deepcopy(plan)
            self.idempotency[key] = plan.id
            self._append_event(project.id, "video_spec.revised", {
                "video_spec_revision_id": spec_revision.id,
                "revision": spec_revision.revision,
                "complete": spec_revision.complete,
            })
            self._append_event(project.id, "plan.revised", {
                "plan_id": plan.id,
                "revision": plan_revision.revision,
                "phase": plan.current_phase,
            })
            return deepcopy(plan)

    async def get_video_spec_revision(self, revision_id: str) -> VideoSpecRevision:
        revision = self.video_spec_revisions.get(revision_id)
        if revision is None:
            raise LookupError("VideoSpec revision not found")
        return deepcopy(revision)

    async def list_video_spec_revisions(self, project_id: str) -> list[VideoSpecRevision]:
        await self.get_project(project_id)
        return [
            deepcopy(self.video_spec_revisions[item])
            for item in self.project_spec_revisions[project_id]
        ]

    async def list_plan_revisions(self, plan_id: str) -> list[BuildPlanRevision]:
        await self.get_plan(plan_id)
        return deepcopy(self.plan_revisions[plan_id])

    async def list_plan_revisions_for_plans(
        self, plan_ids: list[str],
    ) -> dict[str, list[BuildPlanRevision]]:
        return {
            plan_id: deepcopy(self.plan_revisions.get(plan_id, []))
            for plan_id in plan_ids
        }

    async def claim_build_step(self, project_id: str, build_id: str, step_id: str) -> BuildStep | None:
        """Atomically admit pending work; a cancelled step can never be admitted."""
        async with self.lock:
            build = self.builds.get(build_id)
            if build is None or build.project_id != project_id:
                raise LookupError("build not found")
            state = self.build_steps[build_id].get(step_id)
            if build.status in {"failed", "cancelled", "completed"} or state is None or state.status != "pending":
                return None
            state.status = "running"
            state.started_at = state.started_at or now()
            state.updated_at = now()
            self._append_event(project_id, "build.step_status", {
                "build_id": build_id, "step_id": step_id, "status": state.status,
            })
            return deepcopy(state)

    def _mark_build_waiting_agent(self, build: Build, checkpoint: PlanCheckpoint) -> None:
        if not checkpoint.phase.startswith("repair:"):
            build.status = "waiting_agent"
        build.message = f"Waiting for Agent planning after {checkpoint.phase}"
        build.updated_at = now()
        self.builds[build.id] = deepcopy(build)
        self._append_event(checkpoint.project_id, "build.phase.completed", {
            "build_id": build.id, "phase": checkpoint.phase,
        })
        self._append_event(checkpoint.project_id, "build.checkpoint.waiting", {
            "build_id": build.id, "checkpoint_id": checkpoint.id,
            "phase": checkpoint.phase, "next_phase": checkpoint.next_phase,
        })

    async def create_checkpoint(self, checkpoint: PlanCheckpoint) -> PlanCheckpoint:
        async with self.lock:
            build = self.builds.get(checkpoint.build_id)
            if build is None or build.project_id != checkpoint.project_id:
                raise LookupError("build not found")
            if build.status in {"cancelled", "completed"} or (build.status == "failed" and not checkpoint.phase.startswith("repair:")):
                raise ValueError("cannot open a checkpoint for a stopped build")
            if checkpoint.phase.startswith(("task-update:", "live:")):
                outstanding = next((self.checkpoints[item] for item in self.build_checkpoints[build.id]
                                    if self.checkpoints[item].status in {"pending", "planning", "failed"}), None)
                if outstanding is not None:
                    return deepcopy(outstanding)
                current_plan = self.plans[checkpoint.plan_id]
                if current_plan.current_revision != checkpoint.base_plan_revision:
                    raise PlanRevisionConflict(checkpoint.base_plan_revision, current_plan.current_revision)
            existing = next((
                self.checkpoints[item]
                for item in self.build_checkpoints[checkpoint.build_id]
                if (
                    self.checkpoints[item].phase == checkpoint.phase
                    and self.checkpoints[item].base_plan_revision
                    == checkpoint.base_plan_revision
                )
            ), None)
            if existing is not None:
                if (
                    existing.status in {"pending", "planning"}
                    and build.status != "waiting_agent"
                ):
                    self._mark_build_waiting_agent(build, existing)
                return deepcopy(existing)
            self.checkpoints[checkpoint.id] = deepcopy(checkpoint)
            self.build_checkpoints[checkpoint.build_id].append(checkpoint.id)
            self._mark_build_waiting_agent(build, checkpoint)
            return deepcopy(checkpoint)

    async def get_checkpoint(
        self, project_id: str, build_id: str, checkpoint_id: str,
    ) -> PlanCheckpoint:
        checkpoint = self.checkpoints.get(checkpoint_id)
        if checkpoint is None or checkpoint.project_id != project_id or checkpoint.build_id != build_id:
            raise LookupError("plan checkpoint not found")
        return deepcopy(checkpoint)

    async def list_build_checkpoints(
        self, project_id: str, build_id: str,
    ) -> list[PlanCheckpoint]:
        await self.get_build(project_id, build_id)
        return [deepcopy(self.checkpoints[item]) for item in self.build_checkpoints[build_id]]

    async def list_build_checkpoints_for_builds(
        self, project_id: str, build_ids: list[str],
    ) -> dict[str, list[PlanCheckpoint]]:
        grouped: dict[str, list[PlanCheckpoint]] = {build_id: [] for build_id in build_ids}
        for checkpoint in self.checkpoints.values():
            if checkpoint.project_id != project_id or checkpoint.build_id not in grouped:
                continue
            grouped[checkpoint.build_id].append(deepcopy(checkpoint))
        for build_id, items in grouped.items():
            order = {item: index for index, item in enumerate(self.build_checkpoints.get(build_id, []))}
            items.sort(key=lambda item: order.get(item.id, len(order)))
        return grouped

    async def list_planning_checkpoints(self) -> list[PlanCheckpoint]:
        return [deepcopy(item) for item in self.checkpoints.values()
                if item.status == "planning"
                and not item.phase.startswith("live:")
                and self.builds[item.build_id].status not in {"cancelled", "failed", "completed"}]

    async def claim_pending_checkpoint(self, lease_seconds: int = 120) -> PlanCheckpoint | None:
        async with self.lock:
            current_time = now()
            candidates = sorted(self.checkpoints.values(), key=lambda item: item.created_at)
            checkpoint = next((
                item for item in candidates
                if not item.phase.startswith("live:")
                and self.builds[item.build_id].status not in {"cancelled", "failed", "completed"}
                and (item.status == "pending" or (
                    item.status == "planning"
                    and item.lease_expires_at is not None
                    and item.lease_expires_at <= current_time
                ))
            ), None)
            if checkpoint is None:
                return None
            checkpoint.status = "planning"
            checkpoint.delivery_attempts += 1
            checkpoint.delivery_id = checkpoint.delivery_id or uid()
            checkpoint.lease_expires_at = current_time + timedelta(seconds=lease_seconds)
            checkpoint.updated_at = current_time
            self.checkpoints[checkpoint.id] = deepcopy(checkpoint)
            self._append_event(checkpoint.project_id, "build.checkpoint.planning", {
                "build_id": checkpoint.build_id,
                "checkpoint_id": checkpoint.id,
                "attempt": checkpoint.delivery_attempts,
            })
            return deepcopy(checkpoint)

    async def fail_checkpoint_delivery(
        self, checkpoint_id: str, error: str, *, expected_attempt: int | None = None,
    ) -> PlanCheckpoint:
        async with self.lock:
            checkpoint = self.checkpoints.get(checkpoint_id)
            if checkpoint is None:
                raise LookupError("plan checkpoint not found")
            if checkpoint.status == "resolved":
                return deepcopy(checkpoint)
            if expected_attempt is not None and (
                checkpoint.status != "planning" or checkpoint.delivery_attempts != expected_attempt
                or self.builds[checkpoint.build_id].status in {"cancelled", "failed", "completed"}
            ):
                return deepcopy(checkpoint)
            checkpoint.error = error
            checkpoint.lease_expires_at = None
            checkpoint.updated_at = now()
            build = self.builds[checkpoint.build_id]
            if checkpoint.delivery_attempts >= checkpoint.max_delivery_attempts:
                checkpoint.status = "failed"
                build.status = "failed"
                build.message = "Agent planning failed; the previous project version remains active"
                build.error = error
                event_type = "build.checkpoint.failed"
            else:
                checkpoint.status = "pending"
                event_type = "build.checkpoint.waiting"
            self.checkpoints[checkpoint.id] = deepcopy(checkpoint)
            self.builds[build.id] = deepcopy(build)
            self._append_event(checkpoint.project_id, event_type, {
                "build_id": checkpoint.build_id,
                "checkpoint_id": checkpoint.id,
                "attempt": checkpoint.delivery_attempts,
                "error": error,
            })
            return deepcopy(checkpoint)

    async def acknowledge_terminal_checkpoint(self, checkpoint_id: str) -> PlanCheckpoint:
        """Consume a checkpoint whose build already reached a terminal state.

        Task completion can race the queued Agent continuation.  Such a
        continuation is stale, not a planning failure, and must not be
        redelivered or allowed to turn a successful build into an error.
        """
        async with self.lock:
            checkpoint = self.checkpoints.get(checkpoint_id)
            if checkpoint is None:
                raise LookupError("plan checkpoint not found")
            build = self.builds[checkpoint.build_id]
            terminal = build.status in {"cancelled", "completed"} or (
                build.status == "failed" and not checkpoint.phase.startswith("repair:")
            )
            if not terminal or checkpoint.status == "resolved":
                return deepcopy(checkpoint)
            checkpoint.status = "resolved"
            checkpoint.lease_expires_at = None
            checkpoint.error = None
            checkpoint.updated_at = now()
            self.checkpoints[checkpoint.id] = deepcopy(checkpoint)
            self._append_event(checkpoint.project_id, "build.checkpoint.resolved", {
                "build_id": checkpoint.build_id,
                "checkpoint_id": checkpoint.id,
                "acknowledged_only": True,
                "terminal_build": True,
            })
            return deepcopy(checkpoint)

    async def resolve_checkpoint(
        self,
        *,
        checkpoint: PlanCheckpoint,
        updated_plan: RebuildPlan,
        spec_revision: VideoSpecRevision,
        plan_revision: BuildPlanRevision,
        idempotency_key: str,
    ) -> RebuildPlan:
        async with self.lock:
            stored = self.checkpoints.get(checkpoint.id)
            if stored is None:
                raise LookupError("plan checkpoint not found")
            key = (stored.project_id, f"checkpoint:{stored.id}", idempotency_key)
            existing = self.idempotency.get(key)
            if existing is not None:
                return deepcopy(self.plans[existing])
            plan = self.plans[stored.plan_id]
            build = self.builds[stored.build_id]
            if build.status in {"cancelled", "completed"} or (build.status == "failed" and not stored.phase.startswith("repair:")):
                stored.status = "resolved"
                stored.lease_expires_at = None
                stored.error = None
                stored.updated_at = now()
                self.checkpoints[stored.id] = deepcopy(stored)
                self.idempotency[key] = plan.id
                self._append_event(stored.project_id, "build.checkpoint.resolved", {
                    "build_id": build.id,
                    "checkpoint_id": stored.id,
                    "acknowledged_only": True,
                    "terminal_build": True,
                })
                return deepcopy(plan)
            self._assert_version(self._project(stored.project_id), build.base_project_version_id)
            latest_specs = self.project_spec_revisions[stored.project_id]
            latest_spec = self.video_spec_revisions[latest_specs[-1]]
            if plan.current_revision != checkpoint.base_plan_revision:
                raise PlanRevisionConflict(checkpoint.base_plan_revision, plan.current_revision)
            if latest_spec.revision != checkpoint.base_spec_revision:
                raise VideoSpecRevisionConflict(checkpoint.base_spec_revision, latest_spec.revision)
            if stored.status not in {"pending", "planning"}:
                raise ValueError(f"cannot resolve {stored.status} checkpoint")
            if updated_plan.current_revision == plan.current_revision:
                stored.status = "resolved"
                stored.lease_expires_at = None
                stored.error = None
                stored.updated_at = now()
                build.status = "running"
                build.message = "Waiting for task results"
                self.idempotency[key] = plan.id
                self._append_event(stored.project_id, "build.checkpoint.resolved", {
                    "build_id": build.id, "checkpoint_id": stored.id,
                    "acknowledged_only": True,
                })
                return deepcopy(plan)
            existing_steps = set(self.build_steps[stored.build_id])
            added = [item for item in updated_plan.items if item.step_id not in existing_steps]
            for step_id in plan_revision.cancelled_step_ids:
                state = self.build_steps[stored.build_id].get(step_id)
                if state is None or state.status != "pending":
                    raise ValueError(f"cannot cancel non-pending build step: {step_id}")
            # Validate all cancellations before mutating any state.
            for step_id in plan_revision.cancelled_step_ids:
                state = self.build_steps[stored.build_id][step_id]
                state.status = "cancelled"
                state.completed_at = now()
                state.updated_at = now()
                self.build_steps[stored.build_id][step_id] = state
            self.video_spec_revisions[spec_revision.id] = deepcopy(spec_revision)
            self.project_spec_revisions[stored.project_id].append(spec_revision.id)
            self.plan_revisions[plan.id].append(deepcopy(plan_revision))
            self.plans[plan.id] = deepcopy(updated_plan)
            for item in added:
                self.build_steps[stored.build_id][item.step_id] = BuildStep(
                    build_id=stored.build_id,
                    project_id=stored.project_id,
                    plan_step_id=item.step_id,
                    action=item.action,
                    capability=item.capability,
                    resolved_skills=list(item.resolved_skills),
                    skill_context=item.skill_context,
                )
            stored.status = "resolved"
            if stored.phase.startswith("repair:"):
                for checkpoint_id in self.build_checkpoints[stored.build_id]:
                    previous = self.checkpoints[checkpoint_id]
                    if previous.id != stored.id and previous.status in {"pending", "planning", "failed"}:
                        previous.status = "resolved"
                        previous.lease_expires_at = None
                        previous.updated_at = now()
            stored.lease_expires_at = None
            stored.error = None
            stored.updated_at = now()
            self.checkpoints[stored.id] = deepcopy(stored)
            build = self.builds[stored.build_id]
            build.status = "queued"
            build.error = None
            build.message = f"Agent planned {stored.next_phase}"
            build.estimated_cost = updated_plan.estimated_cost
            build.updated_at = now()
            self.builds[build.id] = deepcopy(build)
            self.idempotency[key] = plan.id
            self._append_event(stored.project_id, "video_spec.revised", {
                "video_spec_revision_id": spec_revision.id,
                "revision": spec_revision.revision,
                "checkpoint_id": stored.id,
                "complete": spec_revision.complete,
            })
            self._append_event(stored.project_id, "plan.revised", {
                "plan_id": plan.id,
                "revision": plan_revision.revision,
                "checkpoint_id": stored.id,
                "added_step_ids": plan_revision.added_step_ids,
                "cancelled_step_ids": plan_revision.cancelled_step_ids,
            })
            self._append_event(stored.project_id, "build.checkpoint.resolved", {
                "build_id": stored.build_id,
                "checkpoint_id": stored.id,
                "next_phase": stored.next_phase,
            })
            self._append_event(stored.project_id, "build.phase.started", {
                "build_id": stored.build_id,
                "phase": stored.next_phase,
            })
            return deepcopy(updated_plan)

    async def retry_checkpoint(
        self, project_id: str, build_id: str, checkpoint_id: str,
    ) -> PlanCheckpoint:
        async with self.lock:
            checkpoint = self.checkpoints.get(checkpoint_id)
            if checkpoint is None or checkpoint.project_id != project_id or checkpoint.build_id != build_id:
                raise LookupError("plan checkpoint not found")
            if checkpoint.status != "failed":
                raise ValueError("only failed checkpoints can be retried")
            checkpoint.status = "pending"
            checkpoint.delivery_attempts = 0
            checkpoint.delivery_id = None
            checkpoint.lease_expires_at = None
            checkpoint.error = None
            checkpoint.updated_at = now()
            self.checkpoints[checkpoint.id] = deepcopy(checkpoint)
            build = self.builds[build_id]
            build.status = "waiting_agent"
            build.error = None
            build.message = f"Retrying Agent planning after {checkpoint.phase}"
            self.builds[build.id] = deepcopy(build)
            self._append_event(project_id, "build.checkpoint.waiting", {
                "build_id": build_id, "checkpoint_id": checkpoint_id, "retry": True,
            })
            return deepcopy(checkpoint)

    async def get_plan(self, plan_id: str) -> RebuildPlan:
        plan = self.plans.get(plan_id)
        if plan is None:
            raise LookupError("rebuild plan not found")
        return deepcopy(plan)

    async def submit_build(
        self, *, project_id: str, plan_id: str, base_version_id: str,
        idempotency_key: str, session_id: str | None = None, user_id: str | None = None,
    ) -> Build:
        async with self.lock:
            project = self._project(project_id)
            key = (project_id, "rebuild", idempotency_key)
            existing_id = self.idempotency.get(key)
            if existing_id is not None:
                return deepcopy(self.builds[existing_id])
            self._assert_version(project, base_version_id)
            plan = self.plans.get(plan_id)
            if plan is None or plan.project_id != project_id:
                raise LookupError("rebuild plan not found")
            if plan.base_project_version_id != base_version_id or plan.status != "preview":
                raise ProjectVersionConflict(plan.base_project_version_id, project.current_version_id)
            build = Build(
                project_id=project_id,
                plan_id=plan_id,
                base_project_version_id=base_version_id,
                idempotency_key=idempotency_key,
                session_id=session_id,
                user_id=user_id,
                kind=plan.kind,
                estimated_cost=plan.estimated_cost,
            )
            plan.status = "applied"
            self.plans[plan.id] = deepcopy(plan)
            self.builds[build.id] = deepcopy(build)
            self.build_steps[build.id] = {
                item.step_id: BuildStep(
                    build_id=build.id,
                    project_id=project_id,
                    plan_step_id=item.step_id,
                    action=item.action,
                    capability=item.capability,
                    resolved_skills=list(item.resolved_skills),
                    skill_context=item.skill_context,
                )
                for item in plan.items
            }
            self.idempotency[key] = build.id
            self._append_event(project_id, "build.queued", {"build_id": build.id, "plan_id": plan.id})
            return deepcopy(build)

    async def list_build_steps(self, project_id: str, build_id: str) -> list[BuildStep]:
        await self.get_build(project_id, build_id)
        return deepcopy(list(self.build_steps[build_id].values()))

    async def list_build_steps_for_builds(
        self, project_id: str, build_ids: list[str],
    ) -> dict[str, list[BuildStep]]:
        grouped: dict[str, list[BuildStep]] = {}
        for build_id in build_ids:
            grouped[build_id] = deepcopy(list(self.build_steps.get(build_id, {}).values()))
        return grouped

    async def list_validation_results(
        self, project_id: str, build_id: str,
    ) -> list[ValidationResult]:
        await self.get_build(project_id, build_id)
        return deepcopy(self.validations[build_id])

    async def list_validation_results_for_builds(
        self, project_id: str, build_ids: list[str],
    ) -> dict[str, list[ValidationResult]]:
        return {
            build_id: deepcopy(self.validations.get(build_id, []))
            for build_id in build_ids
        }

    async def update_build_step(self, step: BuildStep) -> BuildStep:
        async with self.lock:
            build = self.builds.get(step.build_id)
            if build is None or build.project_id != step.project_id:
                raise LookupError("build not found")
            current = self.build_steps[step.build_id].get(step.plan_step_id)
            if current is None:
                raise LookupError("build step not found")
            if (
                current.status in {"failed", "cancelled", "completed"}
                and current.status != step.status
            ):
                return deepcopy(current)
            step.updated_at = now()
            self.build_steps[step.build_id][step.plan_step_id] = deepcopy(step)
            self._append_event(step.project_id, "build.step_status", {
                "build_id": step.build_id,
                "step_id": step.plan_step_id,
                "status": step.status,
                "attempt": step.attempt,
                "artifact_version_id": step.result_artifact_version_id,
            })
            return deepcopy(step)

    async def update_build_progress(
        self, project_id: str, build_id: str, progress: float, actual_cost: float,
    ) -> None:
        """Update counters without overwriting a concurrent planning/cancel status."""
        async with self.lock:
            build = self.builds[build_id]
            if build.project_id != project_id:
                raise LookupError("build not found")
            if build.status in {"failed", "cancelled", "completed"}:
                return
            build.progress, build.actual_cost = progress, actual_cost
            build.updated_at = now()
            self._append_event(project_id, "build.status", {
                "build_id": build.id, "status": build.status,
                "progress": progress, "message": build.message,
            })

    async def stage_artifact(self, artifact: MediaArtifactVersion) -> MediaArtifactVersion:
        """Persist a draft without changing the active ProjectVersion."""
        async with self.lock:
            self._project(artifact.project_id)
            existing = self.artifacts[artifact.project_id].get(artifact.id)
            if existing is not None:
                return deepcopy(existing)
            same = [
                item.version for item in self.artifacts[artifact.project_id].values()
                if item.artifact_id == artifact.artifact_id
            ]
            artifact.version = max(same, default=0) + 1
            artifact.status = "draft"
            self.artifacts[artifact.project_id][artifact.id] = deepcopy(artifact)
            return deepcopy(artifact)

    async def get_artifact(self, project_id: str, version_id: str) -> MediaArtifactVersion:
        artifact = self.artifacts[project_id].get(version_id)
        if artifact is None:
            raise LookupError("artifact version not found")
        return deepcopy(artifact)

    async def commit_initial_build(
        self,
        *,
        build_id: str,
        artifacts: list[MediaArtifactVersion],
        dependencies: list[ArtifactDependency],
        validation_results: list[ValidationResult],
    ) -> tuple[Build, ProjectVersion]:
        async with self.lock:
            build = self.builds.get(build_id)
            if build is None:
                raise LookupError("build not found")
            if build.status == "completed" and build.project_version_id:
                return deepcopy(build), deepcopy(self.versions[build.project_version_id])
            if build.status in {"failed", "cancelled"}:
                raise ValueError(f"cannot commit {build.status} build")
            project = self._project(build.project_id)
            self._assert_version(project, build.base_project_version_id)
            if any(not item.passed for item in validation_results):
                raise ValueError("all initial build validations must pass before commit")
            current = self.versions[project.current_version_id]
            plan = self.plans[build.plan_id]
            selections: dict[str, str] = dict(current.selections)
            timeline_id = current.timeline_version_id
            for artifact in artifacts:
                stored = self.artifacts[project.id].get(artifact.id)
                if stored is None:
                    raise LookupError(f"draft artifact not found: {artifact.id}")
                stored.status = "ready"
                self.artifacts[project.id][stored.id] = deepcopy(stored)
                selections[stored.artifact_id] = stored.id
                if stored.type == "timeline":
                    timeline_id = stored.id
            self.dependencies[project.id].extend(deepcopy(dependencies))
            version = ProjectVersion(
                project_id=project.id,
                parent_version_id=project.current_version_id,
                timeline_version_id=timeline_id,
                selections=selections,
                video_spec_revision_id=plan.video_spec_revision_id,
            )
            self._commit_version(project, version)
            self.validations[build.id] = deepcopy(validation_results)
            build.status = "completed"
            build.progress = 1.0
            build.message = "Initial video build committed"
            build.project_version_id = version.id
            build.updated_at = now()
            self.builds[build.id] = deepcopy(build)
            self._append_event(project.id, "project.version_committed", {
                "build_id": build.id,
                "kind": "initial",
                "project_version_id": version.id,
                "parent_version_id": version.parent_version_id,
            })
            return deepcopy(build), deepcopy(version)

    async def get_build(self, project_id: str, build_id: str) -> Build:
        build = self.builds.get(build_id)
        if build is None or build.project_id != project_id:
            raise LookupError("build not found")
        return deepcopy(build)

    async def update_build(self, build: Build) -> Build:
        async with self.lock:
            current = self.builds.get(build.id)
            if current is None or current.project_id != build.project_id:
                raise LookupError("build not found")
            if current.status in {"failed", "cancelled", "completed"}:
                return deepcopy(current)
            build.updated_at = now()
            self.builds[build.id] = deepcopy(build)
            self._append_event(build.project_id, "build.status", {
                "build_id": build.id,
                "status": build.status,
                "progress": build.progress,
                "message": build.message,
            })
            return deepcopy(build)

    async def requeue_failed_build(self, project_id: str, build_id: str) -> Build:
        """Retry only failed work while preserving completed draft artifacts."""
        async with self.lock:
            build = self.builds.get(build_id)
            if build is None or build.project_id != project_id:
                raise LookupError("build not found")
            if build.status != "failed":
                raise ValueError("only failed builds can be retried")
            superseded = {item.step_id for item in self.plans[build.plan_id].items if item.superseded_by}
            for step in self.build_steps[build_id].values():
                if step.status in {"completed", "cancelled"} or step.plan_step_id in superseded:
                    continue
                terminal_step_failure = step.status == "failed"
                step.status = "pending"
                if terminal_step_failure or not step.remote_operation_id:
                    step.attempt = 0
                step.error = None
                step.started_at = None
                step.completed_at = None
                # A failed build is retried only after its provider operation
                # reached a terminal failure. Reusing that operation id would
                # merely reconcile the same failed job forever instead of
                # submitting the failed step again.
                if terminal_step_failure:
                    step.remote_operation_id = None
                    step.remote_provider = None
                step.updated_at = now()
            build.status = "queued"
            build.message = "Retry queued"
            build.error = None
            build.updated_at = now()
            self.builds[build.id] = deepcopy(build)
            self._append_event(project_id, "build.retry_queued", {"build_id": build.id})
            return deepcopy(build)

    async def resume_cancelled_build(self, project_id: str, build_id: str) -> Build:
        """Resume a user-stopped build without resetting paid or cancelled steps."""
        async with self.lock:
            build = self.builds.get(build_id)
            if build is None or build.project_id != project_id:
                raise LookupError("build not found")
            if build.status != "cancelled":
                return deepcopy(build)
            self._assert_version(self._project(project_id), build.base_project_version_id)
            build.status = "queued"
            build.message = "Resumed by user; reconciling existing work"
            build.updated_at = now()
            self._append_event(project_id, "build.status", {
                "build_id": build.id, "status": build.status, "message": build.message,
            })
            return deepcopy(build)

    async def cancel_build(self, project_id: str, build_id: str) -> Build:
        async with self.lock:
            build = self.builds.get(build_id)
            if build is None or build.project_id != project_id:
                raise LookupError("build not found")
            if build.status not in {"completed", "failed", "cancelled"}:
                build.status = "cancelled"
                build.message = "Cancellation requested"
                build.updated_at = now()
                self.builds[build.id] = deepcopy(build)
                self._append_event(project_id, "build.cancelled", {"build_id": build.id})
            return deepcopy(build)

    async def commit_build(
        self,
        *,
        build_id: str,
        replacements: dict[str, MediaArtifactVersion],
        validation_results: list[ValidationResult],
        created_artifacts: dict[str, MediaArtifactVersion] | None = None,
    ) -> tuple[Build, ProjectVersion]:
        async with self.lock:
            build = self.builds.get(build_id)
            if build is None:
                raise LookupError("build not found")
            if build.status == "completed" and build.project_version_id:
                return deepcopy(build), deepcopy(self.versions[build.project_version_id])
            if build.status in {"failed", "cancelled"}:
                raise ValueError(f"cannot commit {build.status} build")
            project = self._project(build.project_id)
            self._assert_version(project, build.base_project_version_id)
            plan = self.plans[build.plan_id]
            rebuild_ids = {item for item in plan.ids_for("rebuild") if item}
            if set(replacements) != rebuild_ids:
                raise ValueError("build replacements must cover every rebuild plan item exactly")
            created_artifacts = created_artifacts or {}
            create_steps = {item.step_id for item in plan.items if item.action == "create"}
            if set(created_artifacts) != create_steps:
                raise ValueError("created artifacts must cover every create plan item exactly")
            validate_ids = {item for item in plan.ids_for("validate") if item}
            passed_ids = {
                item.artifact_version_id for item in validation_results
                if item.passed and item.artifact_version_id is not None
            }
            if not validate_ids <= passed_ids:
                raise ValueError("all validation plan items must pass before commit")

            current = self.versions[project.current_version_id]
            replacement_ids: dict[str, str] = {}
            next_selections = dict(current.selections)
            for old_version_id, replacement in replacements.items():
                old = self.artifacts[project.id].get(old_version_id)
                if old is None:
                    raise LookupError(f"artifact version not found: {old_version_id}")
                replacement.project_id = project.id
                replacement.artifact_id = old.artifact_id
                staged = self.artifacts[project.id].get(replacement.id)
                if staged is not None:
                    replacement.version = staged.version
                else:
                    versions = [
                        item.version for item in self.artifacts[project.id].values()
                        if item.artifact_id == old.artifact_id
                    ]
                    replacement.version = max(versions, default=0) + 1
                replacement.status = "ready"
                self.artifacts[project.id][replacement.id] = deepcopy(replacement)
                replacement_ids[old_version_id] = replacement.id
                next_selections[old.artifact_id] = replacement.id

            created_ids: dict[str, str] = {}
            for step_id, artifact in created_artifacts.items():
                artifact.project_id = project.id
                staged = self.artifacts[project.id].get(artifact.id)
                if staged is not None:
                    artifact.version = staged.version
                else:
                    versions = [
                        item.version for item in self.artifacts[project.id].values()
                        if item.artifact_id == artifact.artifact_id
                    ]
                    artifact.version = max(versions, default=0) + 1
                artifact.status = "ready"
                self.artifacts[project.id][artifact.id] = deepcopy(artifact)
                next_selections[artifact.artifact_id] = artifact.id
                created_ids[step_id] = artifact.id

            old_edges = list(self.dependencies[project.id])
            for edge in old_edges:
                # Artifact versions are immutable provenance nodes. Replacing a source
                # does not make an already-built target depend on the new source, and
                # a reuse step still consumes the exact old version. Only carry an
                # existing incoming edge forward when its target is rebuilt; explicit
                # plan dependencies below connect rebuilt sources to rebuilt targets.
                target = replacement_ids.get(edge.target_version_id)
                if target is None:
                    continue
                source = edge.source_version_id
                if source == target:
                    continue
                self.dependencies[project.id].append(ArtifactDependency(
                    project_id=project.id,
                    source_version_id=source,
                    target_version_id=target,
                    relation=edge.relation,
                    invalidation_policy=edge.invalidation_policy,
                ))

            output_by_step: dict[str, str] = {}
            for item in plan.items:
                if item.action == "create":
                    output_id = created_ids.get(item.step_id)
                elif item.action == "rebuild" and item.artifact_version_id:
                    output_id = replacement_ids.get(item.artifact_version_id)
                elif item.artifact_version_id:
                    output_id = item.artifact_version_id
                else:
                    output_id = None
                if output_id:
                    output_by_step[item.step_id] = output_id
            existing_edges = {
                (item.source_version_id, item.target_version_id)
                for item in self.dependencies[project.id]
            }
            for item in plan.items:
                target = output_by_step.get(item.step_id)
                if target is None:
                    continue
                for dependency in item.depends_on:
                    source = output_by_step.get(dependency)
                    if source is None or source == target or (source, target) in existing_edges:
                        continue
                    self.dependencies[project.id].append(ArtifactDependency(
                        project_id=project.id,
                        source_version_id=source,
                        target_version_id=target,
                        relation="build_step_dependency",
                    ))
                    existing_edges.add((source, target))

            timeline_version_id = current.timeline_version_id
            if timeline_version_id in replacement_ids:
                timeline_version_id = replacement_ids[timeline_version_id]
            for artifact in created_artifacts.values():
                if artifact.type == "timeline":
                    timeline_version_id = artifact.id
            version = ProjectVersion(
                project_id=project.id,
                parent_version_id=current.id,
                timeline_version_id=timeline_version_id,
                selections=next_selections,
                change_request_id=plan.change_request_id,
                video_spec_revision_id=(
                    plan.video_spec_revision_id or current.video_spec_revision_id
                ),
            )
            self._commit_version(project, version)
            self.validations[build.id] = deepcopy(validation_results)
            build.status = "completed"
            build.progress = 1.0
            build.message = "Build committed"
            build.project_version_id = version.id
            build.updated_at = now()
            self.builds[build.id] = deepcopy(build)
            self._append_event(project.id, "project.version_committed", {
                "build_id": build.id,
                "project_version_id": version.id,
                "parent_version_id": version.parent_version_id,
            })
            return deepcopy(build), deepcopy(version)

    async def select_artifact(
        self, *, project_id: str, version_id: str, base_version_id: str, idempotency_key: str,
    ) -> tuple[MediaArtifactVersion, ProjectVersion]:
        async with self.lock:
            project = self._project(project_id)
            key = (project_id, "select", idempotency_key)
            existing_id = self.idempotency.get(key)
            if existing_id is not None:
                version = self.versions[existing_id]
                selected_id = next(
                    item for item in version.selections.values() if item == version_id
                )
                return deepcopy(self.artifacts[project_id][selected_id]), deepcopy(version)
            self._assert_version(project, base_version_id)
            artifact = self.artifacts[project_id].get(version_id)
            if artifact is None:
                raise LookupError("artifact version not found")
            current = self.versions[project.current_version_id]
            version = ProjectVersion(
                project_id=project_id,
                parent_version_id=current.id,
                timeline_version_id=(version_id if artifact.type == "timeline" else current.timeline_version_id),
                selections={**current.selections, artifact.artifact_id: version_id},
            )
            self._commit_version(project, version)
            self.idempotency[key] = version.id
            self._append_event(project_id, "artifact.selected", {
                "artifact_id": artifact.artifact_id,
                "artifact_version_id": artifact.id,
                "project_version_id": version.id,
            })
            return deepcopy(artifact), deepcopy(version)

    async def restore_project_version(
        self, *, project_id: str, restore_version_id: str,
        base_version_id: str, idempotency_key: str,
    ) -> ProjectVersion:
        async with self.lock:
            project = self._project(project_id)
            key = (project_id, "restore", idempotency_key)
            existing_id = self.idempotency.get(key)
            if existing_id is not None:
                return deepcopy(self.versions[existing_id])
            self._assert_version(project, base_version_id)
            restored = self.versions.get(restore_version_id)
            if restored is None or restored.project_id != project_id:
                raise LookupError("project version not found")
            current = self.versions[project.current_version_id]
            version = ProjectVersion(
                project_id=project_id,
                parent_version_id=current.id,
                timeline_version_id=restored.timeline_version_id,
                selections=dict(restored.selections),
            )
            self._commit_version(project, version)
            self.idempotency[key] = version.id
            self._append_event(project_id, "project.version_restored", {
                "source_project_version_id": restore_version_id,
                "project_version_id": version.id,
            })
            return deepcopy(version)

    async def create_export(
        self, *, project_id: str, base_version_id: str, idempotency_key: str, format: str,
    ) -> ExportRecord:
        async with self.lock:
            project = self._project(project_id)
            key = (project_id, "export", idempotency_key)
            existing_id = self.idempotency.get(key)
            if existing_id is not None:
                return deepcopy(self.exports[existing_id])
            self._assert_version(project, base_version_id)
            export = ExportRecord(
                project_id=project_id,
                project_version_id=base_version_id,
                idempotency_key=idempotency_key,
                format=format,
            )
            if format == "mp4":
                version = self.versions[base_version_id]
                candidates = [
                    self.artifacts[project_id][version_id]
                    for version_id in version.selections.values()
                    if self.artifacts[project_id][version_id].type
                    in {"final_video", "video_mixed", "video_assembled"}
                    and self.artifacts[project_id][version_id].uri
                ]
                if candidates:
                    selected = max(
                        candidates,
                        key=lambda item: (
                            item.type == "final_video", item.type == "video_mixed", item.created_at,
                        ),
                    )
                    export.status = "completed"
                    export.uri = selected.uri
            self.exports[export.id] = deepcopy(export)
            self.idempotency[key] = export.id
            self._append_event(project_id, f"export.{export.status}", {
                "export_id": export.id, "format": format, "uri": export.uri,
            })
            return deepcopy(export)

    async def latest_event_sequence(self, project_id: str) -> int:
        await self.get_project(project_id)
        events = self.events.get(project_id) or []
        return events[-1].sequence if events else 0

    async def list_events(self, project_id: str, after: int = 0) -> list[ProjectEvent]:
        await self.get_project(project_id)
        return deepcopy([event for event in self.events[project_id] if event.sequence > after])

    async def active_builds(self, project_id: str) -> list[Build]:
        return deepcopy([
            build for build in self.builds.values()
            if build.project_id == project_id
            and build.status in {"queued", "running", "waiting_external", "waiting_agent"}
        ])

    async def list_builds(self, project_id: str) -> list[Build]:
        await self.get_project(project_id)
        return sorted(
            (deepcopy(item) for item in self.builds.values() if item.project_id == project_id),
            key=lambda item: item.created_at,
            reverse=True,
        )

    def _project(self, project_id: str) -> Project:
        project = self.projects.get(project_id)
        if project is None:
            raise LookupError("project not found")
        return project

    @staticmethod
    def _assert_version(project: Project, expected: str) -> None:
        if project.current_version_id != expected:
            raise ProjectVersionConflict(expected, project.current_version_id)

    def _commit_version(self, project: Project, version: ProjectVersion) -> None:
        self.versions[version.id] = deepcopy(version)
        self.project_versions[project.id].append(version.id)
        project.current_version_id = version.id
        project.updated_at = now()
        self.projects[project.id] = deepcopy(project)

    def _append_event(self, project_id: str, event_type: str, payload: dict) -> ProjectEvent:
        event = ProjectEvent(
            project_id=project_id,
            sequence=len(self.events[project_id]) + 1,
            type=event_type,
            payload=payload,
        )
        self.events[project_id].append(event)
        return event
