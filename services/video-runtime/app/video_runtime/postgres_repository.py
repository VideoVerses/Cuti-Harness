from __future__ import annotations

import json
import re
from datetime import timedelta

import asyncpg

from .models import (
    ArtifactDependency, Build, BuildPlanRevision, BuildStep, ChangeRequest, ExportRecord,
    MediaArtifactVersion, PlanCheckpoint, Project, ProjectEvent, ProjectSessionBinding,
    ProjectSkillLock, ProjectVersion, RebuildPlan, RebuildPlanItem, ValidationResult,
    VideoSpecRevision, now, uid,
)
from .repository import PlanRevisionConflict, ProjectVersionConflict, SidebarRun, VideoSpecRevisionConflict


class PostgresVideoProjectRepository:
    """Durable normalized repository for the incremental media core."""

    def __init__(self, pool: asyncpg.Pool, schema: str = "cuti_video_runtime") -> None:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
            raise ValueError("invalid video runtime database schema")
        self.pool = pool
        self.schema = schema

    @classmethod
    async def connect(
        cls, database_url: str, schema: str = "cuti_video_runtime",
    ) -> "PostgresVideoProjectRepository":
        return cls(await asyncpg.create_pool(database_url, min_size=1, max_size=10), schema)

    async def close(self) -> None:
        await self.pool.close()

    async def create_project(self, project: Project) -> tuple[Project, ProjectVersion]:
        version = ProjectVersion(project_id=project.id)
        project.current_version_id = version.id
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute(
                f"""INSERT INTO {self.schema}.projects
                (id,user_id,title,status,current_version_id,created_at,updated_at)
                VALUES($1,$2,$3,$4,$5,$6,$7)""",
                project.id, project.user_id, project.title, project.status,
                project.current_version_id, project.created_at, project.updated_at,
            )
            await self._insert_version(connection, version)
            await self._append_event(connection, project.id, "project.created", {
                "project_version_id": version.id,
            })
        return project, version

    async def create_project_idempotently(
        self, project: Project, idempotency_key: str,
    ) -> tuple[Project, ProjectVersion]:
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                f"video-project-create:{project.user_id}:{idempotency_key}",
            )
            existing_id = await connection.fetchval(
                f"""SELECT project_id FROM {self.schema}.project_creation_idempotency
                WHERE user_id=$1 AND idempotency_key=$2""",
                project.user_id, idempotency_key,
            )
            if existing_id is not None:
                row = await connection.fetchrow(
                    f"SELECT * FROM {self.schema}.projects WHERE id=$1", existing_id,
                )
                existing = Project(**dict(row))
                return existing, await self._get_version(connection, existing.current_version_id)
            version = ProjectVersion(project_id=project.id)
            project.current_version_id = version.id
            await connection.execute(
                f"""INSERT INTO {self.schema}.projects
                (id,user_id,title,status,current_version_id,created_at,updated_at)
                VALUES($1,$2,$3,$4,$5,$6,$7)""",
                project.id, project.user_id, project.title, project.status,
                project.current_version_id, project.created_at, project.updated_at,
            )
            await self._insert_version(connection, version)
            await connection.execute(
                f"""INSERT INTO {self.schema}.project_creation_idempotency
                (user_id,idempotency_key,project_id) VALUES($1,$2,$3)""",
                project.user_id, idempotency_key, project.id,
            )
            await self._append_event(connection, project.id, "project.created", {
                "project_version_id": version.id,
            })
        return project, version

    async def get_project(self, project_id: str) -> Project:
        row = await self.pool.fetchrow(f"SELECT * FROM {self.schema}.projects WHERE id=$1", project_id)
        if row is None:
            raise LookupError("project not found")
        return Project(**dict(row))

    async def list_projects(self, user_id: str, *, limit: int | None = None) -> list[Project]:
        if limit is None:
            rows = await self.pool.fetch(
                f"SELECT * FROM {self.schema}.projects WHERE user_id=$1 ORDER BY updated_at DESC",
                user_id,
            )
        else:
            rows = await self.pool.fetch(
                f"""SELECT * FROM {self.schema}.projects
                WHERE user_id=$1 ORDER BY updated_at DESC LIMIT $2""",
                user_id, limit,
            )
        return [Project(**dict(row)) for row in rows]

    async def list_sidebar_runs(self, user_id: str, *, limit: int = 50) -> list[SidebarRun]:
        rows = await self.pool.fetch(
            f"""SELECT
                  project.id, project.user_id, project.title, project.status,
                  project.current_version_id, project.created_at, project.updated_at,
                  binding.session_id,
                  latest_build.status AS latest_build_status,
                  ctx.result_id AS context_json
                FROM {self.schema}.projects project
                JOIN LATERAL (
                  SELECT session_id
                  FROM {self.schema}.project_session_bindings
                  WHERE project_id=project.id AND user_id=$1
                  ORDER BY created_at DESC
                  LIMIT 1
                ) binding ON true
                LEFT JOIN LATERAL (
                  SELECT status
                  FROM {self.schema}.builds
                  WHERE project_id=project.id
                  ORDER BY created_at DESC
                  LIMIT 1
                ) latest_build ON true
                LEFT JOIN {self.schema}.operation_idempotency ctx
                  ON ctx.project_id=project.id
                 AND ctx.operation='compat-run-context'
                 AND ctx.idempotency_key='initial'
                WHERE project.user_id=$1
                ORDER BY project.updated_at DESC
                LIMIT $2""",
            user_id, limit,
        )
        return [
            SidebarRun(
                project=Project(
                    id=row["id"], user_id=row["user_id"], title=row["title"],
                    status=row["status"], current_version_id=row["current_version_id"] or "",
                    created_at=row["created_at"], updated_at=row["updated_at"],
                ),
                session_id=row["session_id"],
                latest_build_status=row["latest_build_status"],
                context_json=row["context_json"],
            )
            for row in rows
        ]

    async def list_all_projects(self) -> list[Project]:
        rows = await self.pool.fetch(
            f"SELECT * FROM {self.schema}.projects ORDER BY updated_at DESC",
        )
        return [Project(**dict(row)) for row in rows]

    async def get_project_version(self, version_id: str) -> ProjectVersion:
        async with self.pool.acquire() as connection:
            return await self._get_version(connection, version_id)

    async def list_project_versions(self, project_id: str) -> list[ProjectVersion]:
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                f"""SELECT * FROM {self.schema}.project_versions
                WHERE project_id=$1 ORDER BY created_at""",
                project_id,
            )
            selected = await connection.fetch(
                f"""SELECT selected.project_version_id, selected.artifact_id, selected.artifact_version_id
                FROM {self.schema}.project_version_artifacts selected
                JOIN {self.schema}.project_versions version
                  ON version.id=selected.project_version_id
                WHERE version.project_id=$1""",
                project_id,
            )
        grouped: dict[str, dict[str, str]] = {}
        for item in selected:
            grouped.setdefault(item["project_version_id"], {})[
                item["artifact_id"]
            ] = item["artifact_version_id"]
        return [
            ProjectVersion(**dict(row), selections=grouped.get(row["id"], {}))
            for row in rows
        ]

    async def bind_session(self, binding: ProjectSessionBinding) -> ProjectSessionBinding:
        project = await self.get_project(binding.project_id)
        if project.user_id != binding.user_id:
            raise PermissionError("project does not belong to user")
        async with self.pool.acquire() as connection, connection.transaction():
            result = await connection.execute(
                f"""INSERT INTO {self.schema}.project_session_bindings
                (project_id,session_id,user_id,created_at) VALUES($1,$2,$3,$4)
                ON CONFLICT(project_id,session_id) DO NOTHING""",
                binding.project_id, binding.session_id, binding.user_id, binding.created_at,
            )
            if result != "INSERT 0 0":
                await self._append_event(connection, binding.project_id, "project.session_bound", {
                    "session_id": binding.session_id,
                })
        return binding

    async def latest_session_binding(
        self, project_id: str, user_id: str,
    ) -> ProjectSessionBinding:
        row = await self.pool.fetchrow(
            f"""SELECT * FROM {self.schema}.project_session_bindings
            WHERE project_id=$1 AND user_id=$2 ORDER BY created_at DESC LIMIT 1""",
            project_id, user_id,
        )
        if row is None:
            raise LookupError("project has no DeepSeek Session binding")
        return ProjectSessionBinding(**dict(row))

    async def project_for_session(
        self, session_id: str, user_id: str,
    ) -> tuple[Project, ProjectSessionBinding]:
        row = await self.pool.fetchrow(
            f"""SELECT binding.* FROM {self.schema}.project_session_bindings binding
            JOIN {self.schema}.projects project ON project.id=binding.project_id
            WHERE binding.session_id=$1 AND binding.user_id=$2 AND project.user_id=$2
            ORDER BY binding.created_at DESC LIMIT 1""",
            session_id, user_id,
        )
        if row is None:
            raise LookupError("DeepSeek Session is not bound to a project")
        binding = ProjectSessionBinding(**dict(row))
        return await self.get_project(binding.project_id), binding

    async def list_project_skill_locks(self, project_id: str) -> list[ProjectSkillLock]:
        await self.get_project(project_id)
        rows = await self.pool.fetch(
            f"""SELECT * FROM {self.schema}.project_skill_locks
            WHERE project_id=$1 ORDER BY skill_id""",
            project_id,
        )
        return [ProjectSkillLock(**dict(row)) for row in rows]

    async def upsert_project_skill_lock(
        self, lock: ProjectSkillLock,
    ) -> ProjectSkillLock:
        lock.updated_at = now()
        async with self.pool.acquire() as connection, connection.transaction():
            if not await connection.fetchval(
                f"SELECT 1 FROM {self.schema}.projects WHERE id=$1",
                lock.project_id,
            ):
                raise LookupError("project not found")
            await connection.execute(
                f"""INSERT INTO {self.schema}.project_skill_locks
                (project_id,skill_id,version,digest,source,enabled,updated_at)
                VALUES($1,$2,$3,$4,$5,$6,$7)
                ON CONFLICT(project_id,skill_id) DO UPDATE SET
                  version=EXCLUDED.version,
                  digest=EXCLUDED.digest,
                  source=EXCLUDED.source,
                  enabled=EXCLUDED.enabled,
                  updated_at=EXCLUDED.updated_at""",
                lock.project_id, lock.skill_id, lock.version, lock.digest,
                lock.source, lock.enabled, lock.updated_at,
            )
            await self._append_event(connection, lock.project_id, "project.skill_lock.updated", {
                "skill_id": lock.skill_id,
                "version": lock.version,
                "digest": lock.digest,
                "source": lock.source,
                "enabled": lock.enabled,
            })
        return lock

    async def get_compatibility_run(
        self, user_id: str, idempotency_key: str,
    ) -> tuple[str, str] | None:
        row = await self.pool.fetchrow(
            f"""SELECT project_id,session_id FROM {self.schema}.compatibility_runs
            WHERE user_id=$1 AND idempotency_key=$2""",
            user_id, idempotency_key,
        )
        return None if row is None else (row["project_id"], row["session_id"])

    async def remember_compatibility_run(
        self,
        *,
        user_id: str,
        idempotency_key: str,
        project_id: str,
        session_id: str,
    ) -> None:
        await self.pool.execute(
            f"""INSERT INTO {self.schema}.compatibility_runs
            (user_id,idempotency_key,project_id,session_id) VALUES($1,$2,$3,$4)
            ON CONFLICT(user_id,idempotency_key) DO NOTHING""",
            user_id, idempotency_key, project_id, session_id,
        )

    async def get_operation_result(
        self, project_id: str, operation: str, idempotency_key: str,
    ) -> str | None:
        async with self.pool.acquire() as connection:
            return await self._idempotent_result(
                connection, project_id, operation, idempotency_key,
            )

    async def remember_operation_result(
        self,
        project_id: str,
        operation: str,
        idempotency_key: str,
        result_id: str,
    ) -> None:
        await self.pool.execute(
            f"""INSERT INTO {self.schema}.operation_idempotency
            (project_id,operation,idempotency_key,result_id) VALUES($1,$2,$3,$4)
            ON CONFLICT(project_id,operation,idempotency_key) DO NOTHING""",
            project_id, operation, idempotency_key, result_id,
        )

    async def replace_operation_result(
        self,
        project_id: str,
        operation: str,
        idempotency_key: str,
        result_id: str,
    ) -> None:
        await self.pool.execute(
            f"""INSERT INTO {self.schema}.operation_idempotency
            (project_id,operation,idempotency_key,result_id) VALUES($1,$2,$3,$4)
            ON CONFLICT(project_id,operation,idempotency_key) DO UPDATE SET
              result_id=EXCLUDED.result_id""",
            project_id, operation, idempotency_key, result_id,
        )

    async def add_artifact(
        self, artifact: MediaArtifactVersion, dependencies: list[ArtifactDependency] | None = None,
    ) -> MediaArtifactVersion:
        async with self.pool.acquire() as connection, connection.transaction():
            project = await self._locked_project(connection, artifact.project_id)
            artifact.version = await connection.fetchval(
                f"SELECT COALESCE(max(version),0)+1 FROM {self.schema}.artifact_versions WHERE artifact_id=$1",
                artifact.artifact_id,
            )
            await self._insert_artifact(connection, artifact)
            for edge in dependencies or []:
                await self._insert_edge(connection, edge)
            current = await self._get_version(connection, project.current_version_id)
            selections = {**current.selections, artifact.artifact_id: artifact.id}
            version = ProjectVersion(
                project_id=project.id, parent_version_id=current.id,
                timeline_version_id=artifact.id if artifact.type == "timeline" else current.timeline_version_id,
                selections=selections,
                video_spec_revision_id=current.video_spec_revision_id,
            )
            await self._commit_version(connection, project, version)
            await self._append_event(connection, project.id, "artifact.committed", {
                "artifact_id": artifact.artifact_id, "artifact_version_id": artifact.id,
                "project_version_id": version.id,
            })
        return artifact

    async def current_artifacts(self, project_id: str) -> list[MediaArtifactVersion]:
        project = await self.get_project(project_id)
        rows = await self.pool.fetch(
            f"""SELECT artifact.* FROM {self.schema}.project_version_artifacts selected
            JOIN {self.schema}.artifact_versions artifact ON artifact.id=selected.artifact_version_id
            WHERE selected.project_version_id=$1 ORDER BY artifact.created_at""",
            project.current_version_id,
        )
        return [self._artifact(row) for row in rows]

    async def list_artifact_versions(self, project_id: str) -> list[MediaArtifactVersion]:
        await self.get_project(project_id)
        rows = await self.pool.fetch(
            f"SELECT * FROM {self.schema}.artifact_versions WHERE project_id=$1 ORDER BY created_at,version",
            project_id,
        )
        return [self._artifact(row) for row in rows]

    async def current_dependencies(self, project_id: str) -> list[ArtifactDependency]:
        project = await self.get_project(project_id)
        rows = await self.pool.fetch(
            f"""SELECT edge.* FROM {self.schema}.artifact_edges edge
            JOIN {self.schema}.artifact_versions source ON source.id=edge.source_version_id
            JOIN {self.schema}.artifact_versions target ON target.id=edge.target_version_id
            WHERE edge.project_id=$1
              AND edge.source_version_id IN (SELECT artifact_version_id FROM {self.schema}.project_version_artifacts WHERE project_version_id=$2)
              AND edge.target_version_id IN (SELECT artifact_version_id FROM {self.schema}.project_version_artifacts WHERE project_version_id=$2)
              AND edge.source_version_id <> edge.target_version_id
              AND NOT (edge.relation='build_step_dependency' AND source.created_at > target.created_at)
            ORDER BY edge.created_at""",
            project_id, project.current_version_id,
        )
        return [ArtifactDependency(**dict(row)) for row in rows]

    async def save_change_and_plan(
        self, change: ChangeRequest, plan: RebuildPlan, idempotency_key: str | None = None,
    ) -> RebuildPlan:
        async with self.pool.acquire() as connection, connection.transaction():
            project = await self._locked_project(connection, change.project_id)
            if idempotency_key:
                existing_id = await self._idempotent_result(
                    connection, change.project_id, "preview", idempotency_key,
                )
                if existing_id is not None:
                    return await self._get_plan(connection, existing_id)
            self._assert_version(project, change.base_project_version_id)
            await connection.execute(
                f"""INSERT INTO {self.schema}.change_requests
                (id,project_id,base_project_version_id,description,targets,edits,proposed_video_spec,created_at)
                VALUES($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7::jsonb,$8)""",
                change.id, change.project_id, change.base_project_version_id,
                change.description, json.dumps(change.target_artifact_version_ids),
                json.dumps(change.edits),
                json.dumps(change.proposed_video_spec.model_dump(mode="json") if change.proposed_video_spec else None),
                change.created_at,
            )
            await connection.execute(
                f"""INSERT INTO {self.schema}.rebuild_plans
                (id,project_id,change_request_id,base_project_version_id,kind,workflow_id,
                 video_spec,items,estimated_cost,status,created_at)
                VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8::jsonb,$9,$10,$11)""",
                plan.id, plan.project_id, plan.change_request_id, plan.base_project_version_id,
                plan.kind, plan.workflow_id,
                json.dumps(plan.video_spec.model_dump(mode="json") if plan.video_spec else None),
                json.dumps([item.model_dump(mode="json") for item in plan.items]),
                plan.estimated_cost, plan.status, plan.created_at,
            )
            if idempotency_key:
                await self._remember(
                    connection, change.project_id, "preview", idempotency_key, plan.id,
                )
            await self._append_event(connection, project.id, "rebuild.previewed", {
                "change_request_id": change.id, "plan_id": plan.id,
                "base_project_version_id": plan.base_project_version_id,
            })
        return plan

    async def get_plan(self, plan_id: str) -> RebuildPlan:
        async with self.pool.acquire() as connection:
            return await self._get_plan(connection, plan_id)

    async def save_plan(self, plan: RebuildPlan, idempotency_key: str) -> RebuildPlan:
        async with self.pool.acquire() as connection, connection.transaction():
            project = await self._locked_project(connection, plan.project_id)
            existing = await self._idempotent_result(
                connection, plan.project_id, "plan", idempotency_key,
            )
            if existing:
                return await self._get_plan(connection, existing)
            self._assert_version(project, plan.base_project_version_id)
            await connection.execute(
                f"""INSERT INTO {self.schema}.rebuild_plans
                (id,project_id,change_request_id,base_project_version_id,kind,workflow_id,
                 video_spec,items,estimated_cost,status,created_at)
                VALUES($1,$2,NULL,$3,$4,$5,$6::jsonb,$7::jsonb,$8,$9,$10)""",
                plan.id, plan.project_id, plan.base_project_version_id, plan.kind,
                plan.workflow_id,
                json.dumps(plan.video_spec.model_dump(mode="json") if plan.video_spec else None),
                json.dumps([item.model_dump(mode="json") for item in plan.items]),
                plan.estimated_cost, plan.status, plan.created_at,
            )
            await self._remember(connection, plan.project_id, "plan", idempotency_key, plan.id)
            await self._append_event(connection, plan.project_id, "build.plan_created", {
                "plan_id": plan.id, "kind": plan.kind,
                "estimated_cost": plan.estimated_cost,
            })
        return plan

    async def save_staged_plan(
        self,
        plan: RebuildPlan,
        spec_revision: VideoSpecRevision,
        plan_revision: BuildPlanRevision,
        idempotency_key: str,
    ) -> RebuildPlan:
        async with self.pool.acquire() as connection, connection.transaction():
            project = await self._locked_project(connection, plan.project_id)
            existing = await self._idempotent_result(
                connection, plan.project_id, "plan", idempotency_key,
            )
            if existing:
                return await self._get_plan(connection, existing)
            self._assert_version(project, plan.base_project_version_id)
            await self._insert_spec_revision(connection, spec_revision)
            await connection.execute(
                f"""INSERT INTO {self.schema}.rebuild_plans
                (id,project_id,change_request_id,base_project_version_id,kind,workflow_id,
                 video_spec,items,estimated_cost,status,created_at,schema_version,current_revision,
                 project_intent,video_spec_revision_id,current_phase,next_checkpoint)
                VALUES($1,$2,NULL,$3,$4,$5,$6::jsonb,$7::jsonb,$8,$9,$10,$11,$12,
                 $13::jsonb,$14,$15,$16::jsonb)""",
                plan.id, plan.project_id, plan.base_project_version_id, plan.kind,
                plan.workflow_id,
                json.dumps(plan.video_spec.model_dump(mode="json") if plan.video_spec else None),
                json.dumps([item.model_dump(mode="json") for item in plan.items]),
                plan.estimated_cost, plan.status, plan.created_at, plan.schema_version,
                plan.current_revision,
                json.dumps(plan.project_intent.model_dump(mode="json") if plan.project_intent else None),
                plan.video_spec_revision_id, plan.current_phase,
                json.dumps(plan.next_checkpoint.model_dump(mode="json") if plan.next_checkpoint else None),
            )
            await self._insert_plan_revision(connection, plan_revision)
            await self._remember(connection, plan.project_id, "plan", idempotency_key, plan.id)
            await self._append_event(connection, plan.project_id, "video_spec.revised", {
                "video_spec_revision_id": spec_revision.id,
                "revision": spec_revision.revision,
                "complete": spec_revision.complete,
            })
            await self._append_event(connection, plan.project_id, "plan.revised", {
                "plan_id": plan.id,
                "revision": plan_revision.revision,
                "phase": plan.current_phase,
            })
        return plan

    async def get_video_spec_revision(self, revision_id: str) -> VideoSpecRevision:
        row = await self.pool.fetchrow(
            f"SELECT * FROM {self.schema}.video_spec_revisions WHERE id=$1", revision_id,
        )
        if row is None:
            raise LookupError("VideoSpec revision not found")
        return self._spec_revision(row)

    async def list_video_spec_revisions(self, project_id: str) -> list[VideoSpecRevision]:
        await self.get_project(project_id)
        rows = await self.pool.fetch(
            f"""SELECT * FROM {self.schema}.video_spec_revisions
            WHERE project_id=$1 ORDER BY revision""", project_id,
        )
        return [self._spec_revision(row) for row in rows]

    async def list_plan_revisions(self, plan_id: str) -> list[BuildPlanRevision]:
        rows = await self.pool.fetch(
            f"""SELECT * FROM {self.schema}.build_plan_revisions
            WHERE plan_id=$1 ORDER BY revision""", plan_id,
        )
        return [self._plan_revision(row) for row in rows]

    async def list_plan_revisions_for_plans(
        self, plan_ids: list[str],
    ) -> dict[str, list[BuildPlanRevision]]:
        grouped: dict[str, list[BuildPlanRevision]] = {plan_id: [] for plan_id in plan_ids}
        if not plan_ids:
            return grouped
        rows = await self.pool.fetch(
            f"""SELECT * FROM {self.schema}.build_plan_revisions
            WHERE plan_id=ANY($1::text[]) ORDER BY plan_id, revision""",
            plan_ids,
        )
        for row in rows:
            grouped.setdefault(row["plan_id"], []).append(self._plan_revision(row))
        return grouped

    async def submit_build(
        self, *, project_id: str, plan_id: str, base_version_id: str,
        idempotency_key: str, session_id: str | None = None, user_id: str | None = None,
    ) -> Build:
        async with self.pool.acquire() as connection, connection.transaction():
            existing = await connection.fetchrow(
                f"SELECT * FROM {self.schema}.builds WHERE project_id=$1 AND idempotency_key=$2",
                project_id, idempotency_key,
            )
            if existing:
                return self._build(existing)
            project = await self._locked_project(connection, project_id)
            self._assert_version(project, base_version_id)
            plan_row = await connection.fetchrow(
                f"SELECT * FROM {self.schema}.rebuild_plans WHERE id=$1 AND project_id=$2 FOR UPDATE",
                plan_id, project_id,
            )
            if plan_row is None:
                raise LookupError("rebuild plan not found")
            if plan_row["base_project_version_id"] != base_version_id or plan_row["status"] != "preview":
                raise ProjectVersionConflict(plan_row["base_project_version_id"], project.current_version_id)
            build = Build(
                project_id=project_id, plan_id=plan_id,
                base_project_version_id=base_version_id,
                idempotency_key=idempotency_key,
                session_id=session_id, user_id=user_id,
                kind=str(plan_row.get("kind") or "incremental"),
                estimated_cost=float(plan_row["estimated_cost"] or 0),
            )
            await connection.execute(
                f"""INSERT INTO {self.schema}.builds
                (id,project_id,plan_id,base_project_version_id,idempotency_key,session_id,user_id,
                 kind,status,progress,message,estimated_cost,actual_cost,created_at,updated_at)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)""",
                build.id, build.project_id, build.plan_id, build.base_project_version_id,
                build.idempotency_key, build.session_id, build.user_id,
                build.kind, build.status, build.progress, build.message,
                build.estimated_cost, build.actual_cost, build.created_at, build.updated_at,
            )
            items = plan_row["items"] if isinstance(plan_row["items"], list) else json.loads(plan_row["items"])
            await connection.executemany(
                f"""INSERT INTO {self.schema}.build_steps
                (id,build_id,project_id,plan_step_id,action,capability,status,attempt,
                 resolved_skills,skill_context,updated_at)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10::jsonb,$11)""",
                [
                    (BuildStep(build_id=build.id, project_id=project_id,
                               plan_step_id=item["step_id"], action=item["action"],
                               capability=item.get("capability", "")).id,
                     build.id, project_id, item["step_id"], item["action"],
                     item.get("capability", ""), "pending", 0,
                     json.dumps(item.get("resolved_skills") or []),
                     json.dumps(item.get("skill_context")), now())
                    for item in items
                ],
            )
            await connection.execute(f"UPDATE {self.schema}.rebuild_plans SET status='applied' WHERE id=$1", plan_id)
            await self._append_event(connection, project_id, "build.queued", {"build_id": build.id, "plan_id": plan_id})
        return build

    async def list_build_steps(self, project_id: str, build_id: str) -> list[BuildStep]:
        rows = await self.pool.fetch(
            f"SELECT * FROM {self.schema}.build_steps WHERE project_id=$1 AND build_id=$2 ORDER BY created_at,id",
            project_id, build_id,
        )
        return [self._step(row) for row in rows]

    async def list_build_steps_for_builds(
        self, project_id: str, build_ids: list[str],
    ) -> dict[str, list[BuildStep]]:
        grouped: dict[str, list[BuildStep]] = {build_id: [] for build_id in build_ids}
        if not build_ids:
            return grouped
        rows = await self.pool.fetch(
            f"""SELECT * FROM {self.schema}.build_steps
            WHERE project_id=$1 AND build_id=ANY($2::text[])
            ORDER BY created_at,id""",
            project_id, build_ids,
        )
        for row in rows:
            grouped.setdefault(row["build_id"], []).append(self._step(row))
        return grouped

    async def list_validation_results(
        self, project_id: str, build_id: str,
    ) -> list[ValidationResult]:
        await self.get_build(project_id, build_id)
        rows = await self.pool.fetch(
            f"SELECT * FROM {self.schema}.validation_results WHERE project_id=$1 AND build_id=$2 ORDER BY created_at",
            project_id, build_id,
        )
        return [self._validation(row) for row in rows]

    async def list_validation_results_for_builds(
        self, project_id: str, build_ids: list[str],
    ) -> dict[str, list[ValidationResult]]:
        grouped: dict[str, list[ValidationResult]] = {build_id: [] for build_id in build_ids}
        if not build_ids:
            return grouped
        rows = await self.pool.fetch(
            f"""SELECT * FROM {self.schema}.validation_results
            WHERE project_id=$1 AND build_id=ANY($2::text[])
            ORDER BY created_at""",
            project_id, build_ids,
        )
        for row in rows:
            grouped.setdefault(row["build_id"], []).append(self._validation(row))
        return grouped

    async def claim_build_step(self, project_id: str, build_id: str, step_id: str) -> BuildStep | None:
        """Admit a pending step with the same row lock used by cancellation."""
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                f"""UPDATE {self.schema}.build_steps SET status='running',
                started_at=COALESCE(started_at,$4),updated_at=$4
                WHERE project_id=$1 AND build_id=$2 AND plan_step_id=$3 AND status='pending'
                AND EXISTS (SELECT 1 FROM {self.schema}.builds b
                            WHERE b.id=$2 AND b.status NOT IN ('failed','cancelled','completed'))
                RETURNING *""", project_id, build_id, step_id, now(),
            )
            if row is None:
                return None
            await self._append_event(connection, project_id, "build.step_status", {
                "build_id": build_id, "step_id": step_id, "status": "running",
            })
            values = dict(row)
            for key in ("resolved_skills", "skill_context"):
                if isinstance(values.get(key), str):
                    values[key] = json.loads(values[key])
            return BuildStep(**values)

    async def update_build_step(self, step: BuildStep) -> BuildStep:
        step.updated_at = now()
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                f"""UPDATE {self.schema}.build_steps SET status=$4,attempt=$5,
                remote_operation_id=$6,result_artifact_version_id=$7,error=$8,
                started_at=$9,completed_at=$10,updated_at=$11,remote_provider=$12
                WHERE id=$1 AND build_id=$2 AND project_id=$3
                  AND (status NOT IN ('failed','cancelled','completed') OR status=$4)
                RETURNING *""",
                step.id, step.build_id, step.project_id, step.status, step.attempt,
                step.remote_operation_id, step.result_artifact_version_id, step.error,
                step.started_at, step.completed_at, step.updated_at,
                step.remote_provider,
            )
            if row is None:
                row = await connection.fetchrow(
                    f"""SELECT * FROM {self.schema}.build_steps
                    WHERE id=$1 AND build_id=$2 AND project_id=$3""",
                    step.id, step.build_id, step.project_id,
                )
                if row is None:
                    raise LookupError("build step not found")
                return self._step(row)
            await self._append_event(connection, step.project_id, "build.step_status", {
                "build_id": step.build_id, "step_id": step.plan_step_id,
                "status": step.status, "attempt": step.attempt,
                "artifact_version_id": step.result_artifact_version_id,
            })
        return self._step(row)

    async def update_build_progress(
        self, project_id: str, build_id: str, progress: float, actual_cost: float,
    ) -> None:
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                f"""UPDATE {self.schema}.builds SET progress=$3,actual_cost=$4,updated_at=$5
                WHERE id=$1 AND project_id=$2 AND status NOT IN ('failed','cancelled','completed')
                RETURNING status,message""", build_id, project_id, progress, actual_cost, now(),
            )
            if row is not None:
                await self._append_event(connection, project_id, "build.status", {
                    "build_id": build_id, "status": row["status"],
                    "progress": progress, "message": row["message"],
                })

    async def stage_artifact(self, artifact: MediaArtifactVersion) -> MediaArtifactVersion:
        async with self.pool.acquire() as connection, connection.transaction():
            existing = await connection.fetchrow(
                f"SELECT * FROM {self.schema}.artifact_versions WHERE id=$1", artifact.id,
            )
            if existing:
                return self._artifact(existing)
            await self._locked_project(connection, artifact.project_id)
            artifact.version = await connection.fetchval(
                f"SELECT COALESCE(max(version),0)+1 FROM {self.schema}.artifact_versions WHERE artifact_id=$1",
                artifact.artifact_id,
            )
            artifact.status = "draft"
            await self._insert_artifact(connection, artifact)
        return artifact

    async def get_artifact(self, project_id: str, version_id: str) -> MediaArtifactVersion:
        row = await self.pool.fetchrow(
            f"SELECT * FROM {self.schema}.artifact_versions WHERE project_id=$1 AND id=$2",
            project_id, version_id,
        )
        if row is None:
            raise LookupError("artifact version not found")
        return self._artifact(row)

    async def commit_initial_build(
        self, *, build_id: str, artifacts: list[MediaArtifactVersion],
        dependencies: list[ArtifactDependency], validation_results: list[ValidationResult],
    ) -> tuple[Build, ProjectVersion]:
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                f"SELECT * FROM {self.schema}.builds WHERE id=$1 FOR UPDATE", build_id,
            )
            if row is None:
                raise LookupError("build not found")
            build = self._build(row)
            if build.status == "completed" and build.project_version_id:
                return build, await self._get_version(connection, build.project_version_id)
            if build.status in {"failed", "cancelled"}:
                raise ValueError(f"cannot commit {build.status} build")
            project = await self._locked_project(connection, build.project_id)
            self._assert_version(project, build.base_project_version_id)
            if any(not item.passed for item in validation_results):
                raise ValueError("all initial build validations must pass before commit")
            current = await self._get_version(connection, project.current_version_id)
            plan = await self._get_plan(connection, build.plan_id)
            selections = dict(current.selections)
            timeline_id = current.timeline_version_id
            for artifact in artifacts:
                row = await connection.fetchrow(
                    f"""SELECT status FROM {self.schema}.artifact_versions
                    WHERE id=$1 AND project_id=$2 FOR UPDATE""",
                    artifact.id, project.id,
                )
                if row is None:
                    raise LookupError(f"artifact not found: {artifact.id}")
                if row["status"] == "draft":
                    await connection.execute(
                        f"""UPDATE {self.schema}.artifact_versions SET status='ready'
                        WHERE id=$1 AND project_id=$2""",
                        artifact.id, project.id,
                    )
                elif row["status"] not in {"ready", "approved"}:
                    raise ValueError(
                        f"artifact cannot be selected by initial build: {artifact.id} ({row['status']})"
                    )
                selections[artifact.artifact_id] = artifact.id
                if artifact.type == "timeline":
                    timeline_id = artifact.id
            for edge in dependencies:
                await self._insert_edge(connection, edge)
            version = ProjectVersion(
                project_id=project.id, parent_version_id=project.current_version_id,
                timeline_version_id=timeline_id, selections=selections,
                video_spec_revision_id=plan.video_spec_revision_id,
            )
            await self._commit_version(connection, project, version)
            for item in validation_results:
                await connection.execute(
                    f"""INSERT INTO {self.schema}.validation_results
                    (id,project_id,build_id,artifact_version_id,validator_id,passed,score,issues,metadata,created_at)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb,$10)""",
                    item.id, item.project_id, item.build_id, item.artifact_version_id,
                    item.validator_id, item.passed, item.score, json.dumps(item.issues),
                    json.dumps(item.metadata), item.created_at,
                )
            build.status, build.progress = "completed", 1.0
            build.message, build.project_version_id, build.updated_at = (
                "Initial video build committed", version.id, now(),
            )
            await connection.execute(
                f"""UPDATE {self.schema}.builds SET status=$2,progress=$3,message=$4,
                project_version_id=$5,updated_at=$6 WHERE id=$1""",
                build.id, build.status, build.progress, build.message, version.id, build.updated_at,
            )
            await self._append_event(connection, project.id, "project.version_committed", {
                "build_id": build.id, "kind": "initial",
                "project_version_id": version.id, "parent_version_id": version.parent_version_id,
            })
        return build, version

    async def get_build(self, project_id: str, build_id: str) -> Build:
        row = await self.pool.fetchrow(
            f"SELECT * FROM {self.schema}.builds WHERE id=$1 AND project_id=$2", build_id, project_id,
        )
        if row is None:
            raise LookupError("build not found")
        return self._build(row)

    async def create_checkpoint(self, checkpoint: PlanCheckpoint) -> PlanCheckpoint:
        async with self.pool.acquire() as connection, connection.transaction():
            build_row = await connection.fetchrow(
                f"""SELECT * FROM {self.schema}.builds
                WHERE id=$1 AND project_id=$2 FOR UPDATE""",
                checkpoint.build_id, checkpoint.project_id,
            )
            if build_row is None:
                raise LookupError("build not found")
            if build_row["status"] in {"cancelled", "completed"} or (build_row["status"] == "failed" and not checkpoint.phase.startswith("repair:")):
                raise ValueError("cannot open a checkpoint for a stopped build")
            if checkpoint.phase.startswith(("task-update:", "live:")):
                outstanding = await connection.fetchrow(
                    f"""SELECT * FROM {self.schema}.plan_checkpoints
                    WHERE build_id=$1 AND status IN ('pending','planning','failed') LIMIT 1""",
                    checkpoint.build_id,
                )
                if outstanding is not None:
                    return self._checkpoint(outstanding)
                current_plan = await self._get_plan(connection, checkpoint.plan_id)
                if current_plan.current_revision != checkpoint.base_plan_revision:
                    raise PlanRevisionConflict(checkpoint.base_plan_revision, current_plan.current_revision)
            inserted = await connection.fetchrow(
                f"""INSERT INTO {self.schema}.plan_checkpoints
                (id,project_id,build_id,plan_id,workflow_id,session_id,user_id,phase,next_phase,
                 status,required_artifact_types,artifact_version_ids,artifact_summaries,
                 resolved_sections,unresolved_sections,planner_instruction,planning_mode,
                 base_plan_revision,base_spec_revision,delivery_attempts,max_delivery_attempts,
                 semantic_repair_attempts,delivery_id,lease_expires_at,error,created_at,updated_at)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb,$12::jsonb,$13::jsonb,
                 $14::jsonb,$15::jsonb,$16,$17,$18,$19,$20,$21,$22,$23,$24,$25,$26,$27)
                ON CONFLICT(build_id,phase,base_plan_revision) DO NOTHING RETURNING *""",
                checkpoint.id, checkpoint.project_id, checkpoint.build_id, checkpoint.plan_id,
                checkpoint.workflow_id, checkpoint.session_id, checkpoint.user_id,
                checkpoint.phase, checkpoint.next_phase, checkpoint.status,
                json.dumps(checkpoint.required_artifact_types),
                json.dumps(checkpoint.artifact_version_ids),
                json.dumps(checkpoint.artifact_summaries),
                json.dumps(checkpoint.resolved_sections),
                json.dumps(checkpoint.unresolved_sections), checkpoint.planner_instruction,
                checkpoint.planning_mode, checkpoint.base_plan_revision,
                checkpoint.base_spec_revision, checkpoint.delivery_attempts,
                checkpoint.max_delivery_attempts, checkpoint.semantic_repair_attempts,
                checkpoint.delivery_id, checkpoint.lease_expires_at, checkpoint.error,
                checkpoint.created_at, checkpoint.updated_at,
            )
            if inserted is None:
                existing = await connection.fetchrow(
                    f"""SELECT * FROM {self.schema}.plan_checkpoints
                    WHERE build_id=$1 AND phase=$2 AND base_plan_revision=$3""",
                    checkpoint.build_id, checkpoint.phase, checkpoint.base_plan_revision,
                )
                stored = self._checkpoint(existing)
                if (
                    stored.status in {"pending", "planning"}
                    and build_row["status"] != "waiting_agent"
                ):
                    await connection.execute(
                        f"""UPDATE {self.schema}.builds SET status='waiting_agent',message=$3,updated_at=$4
                        WHERE id=$1 AND project_id=$2""",
                        checkpoint.build_id, checkpoint.project_id,
                        f"Waiting for Agent planning after {checkpoint.phase}", now(),
                    )
                    await self._append_event(connection, checkpoint.project_id, "build.phase.completed", {
                        "build_id": checkpoint.build_id, "phase": checkpoint.phase,
                    })
                    await self._append_event(connection, checkpoint.project_id, "build.checkpoint.waiting", {
                        "build_id": checkpoint.build_id, "checkpoint_id": stored.id,
                        "phase": checkpoint.phase, "next_phase": checkpoint.next_phase,
                    })
                return stored
            await connection.execute(
                f"""UPDATE {self.schema}.builds SET status=CASE WHEN $5 THEN status ELSE 'waiting_agent' END,message=$3,updated_at=$4
                WHERE id=$1 AND project_id=$2""",
                checkpoint.build_id, checkpoint.project_id,
                f"Waiting for Agent planning after {checkpoint.phase}", now(), checkpoint.phase.startswith("repair:"),
            )
            await self._append_event(connection, checkpoint.project_id, "build.phase.completed", {
                "build_id": checkpoint.build_id, "phase": checkpoint.phase,
            })
            await self._append_event(connection, checkpoint.project_id, "build.checkpoint.waiting", {
                "build_id": checkpoint.build_id, "checkpoint_id": checkpoint.id,
                "phase": checkpoint.phase, "next_phase": checkpoint.next_phase,
            })
        return checkpoint

    async def get_checkpoint(
        self, project_id: str, build_id: str, checkpoint_id: str,
    ) -> PlanCheckpoint:
        row = await self.pool.fetchrow(
            f"""SELECT * FROM {self.schema}.plan_checkpoints
            WHERE id=$1 AND project_id=$2 AND build_id=$3""",
            checkpoint_id, project_id, build_id,
        )
        if row is None:
            raise LookupError("plan checkpoint not found")
        return self._checkpoint(row)

    async def list_build_checkpoints(
        self, project_id: str, build_id: str,
    ) -> list[PlanCheckpoint]:
        await self.get_build(project_id, build_id)
        rows = await self.pool.fetch(
            f"""SELECT * FROM {self.schema}.plan_checkpoints
            WHERE project_id=$1 AND build_id=$2 ORDER BY created_at""",
            project_id, build_id,
        )
        return [self._checkpoint(row) for row in rows]

    async def list_build_checkpoints_for_builds(
        self, project_id: str, build_ids: list[str],
    ) -> dict[str, list[PlanCheckpoint]]:
        grouped: dict[str, list[PlanCheckpoint]] = {build_id: [] for build_id in build_ids}
        if not build_ids:
            return grouped
        rows = await self.pool.fetch(
            f"""SELECT * FROM {self.schema}.plan_checkpoints
            WHERE project_id=$1 AND build_id=ANY($2::text[])
            ORDER BY created_at""",
            project_id, build_ids,
        )
        for row in rows:
            grouped.setdefault(row["build_id"], []).append(self._checkpoint(row))
        return grouped

    async def list_planning_checkpoints(self) -> list[PlanCheckpoint]:
        async with self.pool.acquire() as connection:
            rows = await connection.fetch(
                f"""SELECT c.* FROM {self.schema}.plan_checkpoints c
                JOIN {self.schema}.builds b ON b.id=c.build_id
                WHERE c.status='planning' AND c.phase NOT LIKE 'live:%'
                AND b.status NOT IN ('cancelled','failed','completed')""",
            )
        return [self._checkpoint(row) for row in rows]

    async def claim_pending_checkpoint(self, lease_seconds: int = 120) -> PlanCheckpoint | None:
        current_time = now()
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                f"""SELECT * FROM {self.schema}.plan_checkpoints c
                WHERE (status='pending'
                   OR (status='planning' AND lease_expires_at IS NOT NULL AND lease_expires_at <= $1))
                AND c.phase NOT LIKE 'live:%'
                AND EXISTS (SELECT 1 FROM {self.schema}.builds b
                            WHERE b.id=c.build_id AND b.status NOT IN ('cancelled','failed','completed'))
                ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1""",
                current_time,
            )
            if row is None:
                return None
            checkpoint = self._checkpoint(row)
            checkpoint.status = "planning"
            checkpoint.delivery_attempts += 1
            checkpoint.delivery_id = checkpoint.delivery_id or uid()
            checkpoint.lease_expires_at = current_time + timedelta(seconds=lease_seconds)
            checkpoint.updated_at = current_time
            await connection.execute(
                f"""UPDATE {self.schema}.plan_checkpoints
                SET status=$2,delivery_attempts=$3,delivery_id=$4,lease_expires_at=$5,updated_at=$6
                WHERE id=$1""",
                checkpoint.id, checkpoint.status, checkpoint.delivery_attempts,
                checkpoint.delivery_id, checkpoint.lease_expires_at, checkpoint.updated_at,
            )
            await self._append_event(connection, checkpoint.project_id, "build.checkpoint.planning", {
                "build_id": checkpoint.build_id,
                "checkpoint_id": checkpoint.id,
                "attempt": checkpoint.delivery_attempts,
            })
        return checkpoint

    async def fail_checkpoint_delivery(
        self, checkpoint_id: str, error: str, *, expected_attempt: int | None = None,
    ) -> PlanCheckpoint:
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                f"SELECT * FROM {self.schema}.plan_checkpoints WHERE id=$1 FOR UPDATE",
                checkpoint_id,
            )
            if row is None:
                raise LookupError("plan checkpoint not found")
            checkpoint = self._checkpoint(row)
            if checkpoint.status == "resolved":
                return checkpoint
            if expected_attempt is not None:
                build_status = await connection.fetchval(
                    f"SELECT status FROM {self.schema}.builds WHERE id=$1 FOR UPDATE", checkpoint.build_id,
                )
                if (checkpoint.status != "planning" or checkpoint.delivery_attempts != expected_attempt
                        or build_status in {"cancelled", "failed", "completed"}):
                    return checkpoint
            checkpoint.error = error
            checkpoint.lease_expires_at = None
            checkpoint.updated_at = now()
            terminal = checkpoint.delivery_attempts >= checkpoint.max_delivery_attempts
            checkpoint.status = "failed" if terminal else "pending"
            await connection.execute(
                f"""UPDATE {self.schema}.plan_checkpoints
                SET status=$2,lease_expires_at=NULL,error=$3,updated_at=$4 WHERE id=$1""",
                checkpoint.id, checkpoint.status, checkpoint.error, checkpoint.updated_at,
            )
            event_type = "build.checkpoint.failed" if terminal else "build.checkpoint.waiting"
            if terminal:
                await connection.execute(
                    f"""UPDATE {self.schema}.builds
                    SET status='failed',message=$2,error=$3,updated_at=$4 WHERE id=$1""",
                    checkpoint.build_id,
                    "Agent planning failed; the previous project version remains active",
                    error, now(),
                )
            await self._append_event(connection, checkpoint.project_id, event_type, {
                "build_id": checkpoint.build_id,
                "checkpoint_id": checkpoint.id,
                "attempt": checkpoint.delivery_attempts,
                "error": error,
            })
        return checkpoint

    async def acknowledge_terminal_checkpoint(self, checkpoint_id: str) -> PlanCheckpoint:
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                f"SELECT * FROM {self.schema}.plan_checkpoints WHERE id=$1 FOR UPDATE",
                checkpoint_id,
            )
            if row is None:
                raise LookupError("plan checkpoint not found")
            checkpoint = self._checkpoint(row)
            build_status = await connection.fetchval(
                f"SELECT status FROM {self.schema}.builds WHERE id=$1 FOR UPDATE",
                checkpoint.build_id,
            )
            terminal = build_status in {"cancelled", "completed"} or (
                build_status == "failed" and not checkpoint.phase.startswith("repair:")
            )
            if not terminal or checkpoint.status == "resolved":
                return checkpoint
            checkpoint.status = "resolved"
            checkpoint.lease_expires_at = None
            checkpoint.error = None
            checkpoint.updated_at = now()
            await connection.execute(
                f"""UPDATE {self.schema}.plan_checkpoints
                SET status='resolved',lease_expires_at=NULL,error=NULL,updated_at=$2 WHERE id=$1""",
                checkpoint.id, checkpoint.updated_at,
            )
            await self._append_event(connection, checkpoint.project_id, "build.checkpoint.resolved", {
                "build_id": checkpoint.build_id,
                "checkpoint_id": checkpoint.id,
                "acknowledged_only": True,
                "terminal_build": True,
            })
        return checkpoint

    async def resolve_checkpoint(
        self,
        *,
        checkpoint: PlanCheckpoint,
        updated_plan: RebuildPlan,
        spec_revision: VideoSpecRevision,
        plan_revision: BuildPlanRevision,
        idempotency_key: str,
    ) -> RebuildPlan:
        async with self.pool.acquire() as connection, connection.transaction():
            build_row = await connection.fetchrow(
                f"SELECT * FROM {self.schema}.builds WHERE id=$1 AND project_id=$2 FOR UPDATE",
                checkpoint.build_id, checkpoint.project_id,
            )
            if build_row is None:
                raise LookupError("build not found")
            stored_row = await connection.fetchrow(
                f"""SELECT * FROM {self.schema}.plan_checkpoints
                WHERE id=$1 AND project_id=$2 AND build_id=$3 FOR UPDATE""",
                checkpoint.id, checkpoint.project_id, checkpoint.build_id,
            )
            if stored_row is None:
                raise LookupError("plan checkpoint not found")
            stored = self._checkpoint(stored_row)
            operation = f"checkpoint:{stored.id}"
            existing = await self._idempotent_result(
                connection, stored.project_id, operation, idempotency_key,
            )
            if existing:
                return await self._get_plan(connection, existing)
            current_plan = await self._get_plan(connection, stored.plan_id)
            if build_row["status"] in {"cancelled", "completed"} or (build_row["status"] == "failed" and not stored.phase.startswith("repair:")):
                await connection.execute(
                    f"UPDATE {self.schema}.plan_checkpoints SET status='resolved',lease_expires_at=NULL,error=NULL,updated_at=$2 WHERE id=$1",
                    stored.id, now(),
                )
                await self._remember(connection, stored.project_id, operation, idempotency_key, current_plan.id)
                await self._append_event(connection, stored.project_id, "build.checkpoint.resolved", {
                    "build_id": stored.build_id,
                    "checkpoint_id": stored.id,
                    "acknowledged_only": True,
                    "terminal_build": True,
                })
                return current_plan
            project_row = await connection.fetchrow(
                f"SELECT current_version_id FROM {self.schema}.projects WHERE id=$1 FOR UPDATE", stored.project_id,
            )
            if project_row["current_version_id"] != build_row["base_project_version_id"]:
                raise ProjectVersionConflict(build_row["base_project_version_id"], project_row["current_version_id"])
            latest_spec_row = await connection.fetchrow(
                f"""SELECT * FROM {self.schema}.video_spec_revisions
                WHERE project_id=$1 ORDER BY revision DESC LIMIT 1 FOR UPDATE""",
                stored.project_id,
            )
            if latest_spec_row is None:
                raise LookupError("VideoSpec revision not found")
            latest_spec = self._spec_revision(latest_spec_row)
            if current_plan.current_revision != checkpoint.base_plan_revision:
                raise PlanRevisionConflict(checkpoint.base_plan_revision, current_plan.current_revision)
            if latest_spec.revision != checkpoint.base_spec_revision:
                raise VideoSpecRevisionConflict(checkpoint.base_spec_revision, latest_spec.revision)
            if stored.status not in {"pending", "planning"}:
                raise ValueError(f"cannot resolve {stored.status} checkpoint")
            if updated_plan.current_revision == current_plan.current_revision:
                await connection.execute(
                    f"UPDATE {self.schema}.plan_checkpoints SET status='resolved',lease_expires_at=NULL,error=NULL,updated_at=$2 WHERE id=$1",
                    stored.id, now(),
                )
                await connection.execute(
                    f"UPDATE {self.schema}.builds SET status='running',message='Waiting for task results',updated_at=$2 WHERE id=$1",
                    stored.build_id, now(),
                )
                await self._remember(connection, stored.project_id, operation, idempotency_key, current_plan.id)
                await self._append_event(connection, stored.project_id, "build.checkpoint.resolved", {
                    "build_id": stored.build_id, "checkpoint_id": stored.id, "acknowledged_only": True,
                })
                return current_plan
            existing_step_ids = {
                row["plan_step_id"] for row in await connection.fetch(
                    f"SELECT plan_step_id FROM {self.schema}.build_steps WHERE build_id=$1",
                    stored.build_id,
                )
            }
            added = [item for item in updated_plan.items if item.step_id not in existing_step_ids]
            if plan_revision.cancelled_step_ids:
                cancelled_rows = await connection.fetch(
                    f"""UPDATE {self.schema}.build_steps
                    SET status='cancelled',completed_at=$3,updated_at=$3
                    WHERE build_id=$1 AND plan_step_id=ANY($2::text[]) AND status='pending'
                    RETURNING plan_step_id""",
                    stored.build_id, plan_revision.cancelled_step_ids, now(),
                )
                cancelled = {row["plan_step_id"] for row in cancelled_rows}
                missed = sorted(set(plan_revision.cancelled_step_ids) - cancelled)
                if missed:
                    raise ValueError(
                        "cannot cancel non-pending build steps: " + ", ".join(missed)
                    )
            await self._insert_spec_revision(connection, spec_revision)
            await self._insert_plan_revision(connection, plan_revision)
            await connection.execute(
                f"""UPDATE {self.schema}.rebuild_plans
                SET video_spec=$2::jsonb,items=$3::jsonb,estimated_cost=$4,
                    current_revision=$5,video_spec_revision_id=$6,current_phase=$7,
                    next_checkpoint=$8::jsonb
                WHERE id=$1""",
                updated_plan.id,
                json.dumps(updated_plan.video_spec.model_dump(mode="json") if updated_plan.video_spec else None),
                json.dumps([item.model_dump(mode="json") for item in updated_plan.items]),
                updated_plan.estimated_cost, updated_plan.current_revision,
                updated_plan.video_spec_revision_id, updated_plan.current_phase,
                json.dumps(
                    updated_plan.next_checkpoint.model_dump(mode="json")
                    if updated_plan.next_checkpoint else None
                ),
            )
            if added:
                await connection.executemany(
                    f"""INSERT INTO {self.schema}.build_steps
                    (id,build_id,project_id,plan_step_id,action,capability,status,attempt,
                     resolved_skills,skill_context,updated_at)
                    VALUES($1,$2,$3,$4,$5,$6,'pending',0,$7::jsonb,$8::jsonb,$9)""",
                    [
                        (
                            BuildStep(
                                build_id=stored.build_id,
                                project_id=stored.project_id,
                                plan_step_id=item.step_id,
                                action=item.action,
                                capability=item.capability,
                            ).id,
                            stored.build_id, stored.project_id, item.step_id, item.action,
                            item.capability,
                            json.dumps([value.model_dump(mode="json") for value in item.resolved_skills]),
                            json.dumps(item.skill_context.model_dump(mode="json") if item.skill_context else None),
                            now(),
                        )
                        for item in added
                    ],
                )
            if stored.phase.startswith("repair:"):
                await connection.execute(
                    f"""UPDATE {self.schema}.plan_checkpoints
                    SET status='resolved',lease_expires_at=NULL,updated_at=$2
                    WHERE build_id=$1 AND status IN ('pending','planning','failed')""", stored.build_id, now(),
                )
            await connection.execute(
                f"""UPDATE {self.schema}.plan_checkpoints
                SET status='resolved',lease_expires_at=NULL,error=NULL,updated_at=$2 WHERE id=$1""",
                stored.id, now(),
            )
            await connection.execute(
                f"""UPDATE {self.schema}.builds
                SET status='queued',message=$2,estimated_cost=$3,error=NULL,updated_at=$4
                WHERE id=$1""",
                stored.build_id, f"Agent planned {stored.next_phase}",
                updated_plan.estimated_cost, now(),
            )
            await self._remember(connection, stored.project_id, operation, idempotency_key, current_plan.id)
            await self._append_event(connection, stored.project_id, "video_spec.revised", {
                "video_spec_revision_id": spec_revision.id,
                "revision": spec_revision.revision,
                "checkpoint_id": stored.id,
                "complete": spec_revision.complete,
            })
            await self._append_event(connection, stored.project_id, "plan.revised", {
                "plan_id": current_plan.id,
                "revision": plan_revision.revision,
                "checkpoint_id": stored.id,
                "added_step_ids": plan_revision.added_step_ids,
            })
            await self._append_event(connection, stored.project_id, "build.checkpoint.resolved", {
                "build_id": stored.build_id,
                "checkpoint_id": stored.id,
                "next_phase": stored.next_phase,
            })
            await self._append_event(connection, stored.project_id, "build.phase.started", {
                "build_id": stored.build_id, "phase": stored.next_phase,
            })
        return updated_plan

    async def retry_checkpoint(
        self, project_id: str, build_id: str, checkpoint_id: str,
    ) -> PlanCheckpoint:
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                f"""SELECT * FROM {self.schema}.plan_checkpoints
                WHERE id=$1 AND project_id=$2 AND build_id=$3 FOR UPDATE""",
                checkpoint_id, project_id, build_id,
            )
            if row is None:
                raise LookupError("plan checkpoint not found")
            checkpoint = self._checkpoint(row)
            if checkpoint.status != "failed":
                raise ValueError("only failed checkpoints can be retried")
            checkpoint.status = "pending"
            checkpoint.delivery_attempts = 0
            checkpoint.delivery_id = None
            checkpoint.lease_expires_at = None
            checkpoint.error = None
            checkpoint.updated_at = now()
            await connection.execute(
                f"""UPDATE {self.schema}.plan_checkpoints
                SET status='pending',delivery_attempts=0,delivery_id=NULL,
                    lease_expires_at=NULL,error=NULL,updated_at=$2 WHERE id=$1""",
                checkpoint.id, checkpoint.updated_at,
            )
            await connection.execute(
                f"""UPDATE {self.schema}.builds
                SET status='waiting_agent',message=$2,error=NULL,updated_at=$3 WHERE id=$1""",
                build_id, f"Retrying Agent planning after {checkpoint.phase}", now(),
            )
            await self._append_event(connection, project_id, "build.checkpoint.waiting", {
                "build_id": build_id, "checkpoint_id": checkpoint_id, "retry": True,
            })
        return checkpoint

    async def update_build(self, build: Build) -> Build:
        build.updated_at = now()
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                f"""UPDATE {self.schema}.builds SET status=$3,progress=$4,message=$5,
                project_version_id=$6,error=$7,updated_at=$8,actual_cost=$9
                WHERE id=$1 AND project_id=$2
                  AND status NOT IN ('failed','cancelled','completed')
                RETURNING *""",
                build.id, build.project_id, build.status, build.progress, build.message,
                build.project_version_id, build.error, build.updated_at, build.actual_cost,
            )
            if row is None:
                row = await connection.fetchrow(
                    f"SELECT * FROM {self.schema}.builds WHERE id=$1 AND project_id=$2",
                    build.id, build.project_id,
                )
                if row is None:
                    raise LookupError("build not found")
                return self._build(row)
            await self._append_event(connection, build.project_id, "build.status", {
                "build_id": build.id, "status": build.status,
                "progress": build.progress, "message": build.message,
            })
        return self._build(row)

    async def requeue_failed_build(self, project_id: str, build_id: str) -> Build:
        """Retry failed steps without discarding successful, already-paid outputs."""
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                f"SELECT * FROM {self.schema}.builds WHERE id=$1 AND project_id=$2 FOR UPDATE",
                build_id, project_id,
            )
            if row is None:
                raise LookupError("build not found")
            build = self._build(row)
            if build.status != "failed":
                raise ValueError("only failed builds can be retried")
            plan = await self._get_plan(connection, build.plan_id)
            superseded = [item.step_id for item in plan.items if item.superseded_by]
            await connection.execute(
                f"""UPDATE {self.schema}.build_steps
                SET status='pending',attempt=0,error=NULL,started_at=NULL,completed_at=NULL,
                    remote_operation_id=NULL,remote_provider=NULL,updated_at=$3
                WHERE build_id=$1 AND project_id=$2 AND status='failed'
                  AND NOT (plan_step_id=ANY($4::text[]))""",
                build_id, project_id, now(), superseded,
            )
            await connection.execute(
                f"""UPDATE {self.schema}.build_steps
                SET status='pending',error=NULL,started_at=NULL,completed_at=NULL,updated_at=$3
                WHERE build_id=$1 AND project_id=$2
                  AND status NOT IN ('completed','failed','cancelled')""",
                build_id, project_id, now(),
            )
            build.status = "queued"
            build.message = "Retry queued"
            build.error = None
            build.updated_at = now()
            await connection.execute(
                f"""UPDATE {self.schema}.builds
                SET status=$3,message=$4,error=NULL,updated_at=$5
                WHERE id=$1 AND project_id=$2""",
                build_id, project_id, build.status, build.message, build.updated_at,
            )
            await self._append_event(
                connection, project_id, "build.retry_queued", {"build_id": build.id},
            )
        return build

    async def resume_cancelled_build(self, project_id: str, build_id: str) -> Build:
        """Resume stopped work, preserving step state and remote operation identities."""
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                f"SELECT * FROM {self.schema}.builds WHERE id=$1 AND project_id=$2 FOR UPDATE",
                build_id, project_id,
            )
            if row is None:
                raise LookupError("build not found")
            build = self._build(row)
            if build.status != "cancelled":
                return build
            project = await connection.fetchrow(
                f"SELECT * FROM {self.schema}.projects WHERE id=$1 FOR UPDATE", project_id,
            )
            if project["current_version_id"] != build.base_project_version_id:
                raise ProjectVersionConflict(build.base_project_version_id, project["current_version_id"])
            build.status = "queued"
            build.message = "Resumed by user; reconciling existing work"
            build.updated_at = now()
            await connection.execute(
                f"UPDATE {self.schema}.builds SET status=$2,message=$3,updated_at=$4 WHERE id=$1",
                build.id, build.status, build.message, build.updated_at,
            )
            await self._append_event(connection, project_id, "build.status", {
                "build_id": build.id, "status": build.status, "message": build.message,
            })
            return build

    async def cancel_build(self, project_id: str, build_id: str) -> Build:
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                f"SELECT * FROM {self.schema}.builds WHERE id=$1 AND project_id=$2 FOR UPDATE",
                build_id, project_id,
            )
            if row is None:
                raise LookupError("build not found")
            build = self._build(row)
            if build.status not in {"completed", "failed", "cancelled"}:
                build.status, build.message, build.updated_at = "cancelled", "Cancellation requested", now()
                await connection.execute(
                    f"UPDATE {self.schema}.builds SET status=$2,message=$3,updated_at=$4 WHERE id=$1",
                    build.id, build.status, build.message, build.updated_at,
                )
                await self._append_event(connection, project_id, "build.cancelled", {"build_id": build.id})
        return build

    async def commit_build(
        self, *, build_id: str, replacements: dict[str, MediaArtifactVersion],
        validation_results: list[ValidationResult],
        created_artifacts: dict[str, MediaArtifactVersion] | None = None,
    ) -> tuple[Build, ProjectVersion]:
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(f"SELECT * FROM {self.schema}.builds WHERE id=$1 FOR UPDATE", build_id)
            if row is None:
                raise LookupError("build not found")
            build = self._build(row)
            if build.status == "completed" and build.project_version_id:
                return build, await self._get_version(connection, build.project_version_id)
            if build.status in {"failed", "cancelled"}:
                raise ValueError(f"cannot commit {build.status} build")
            project = await self._locked_project(connection, build.project_id)
            self._assert_version(project, build.base_project_version_id)
            plan = await self._get_plan(connection, build.plan_id)
            rebuild_ids = {item for item in plan.ids_for("rebuild") if item}
            if set(replacements) != rebuild_ids:
                raise ValueError("build replacements must cover every rebuild plan item exactly")
            created_artifacts = created_artifacts or {}
            create_steps = {item.step_id for item in plan.items if item.action == "create"}
            if set(created_artifacts) != create_steps:
                raise ValueError("created artifacts must cover every create plan item exactly")
            passed = {item.artifact_version_id for item in validation_results if item.passed}
            if not {item for item in plan.ids_for("validate") if item} <= passed:
                raise ValueError("all validation plan items must pass before commit")
            current = await self._get_version(connection, project.current_version_id)
            next_selections, replacement_ids = dict(current.selections), {}
            for old_id, replacement in replacements.items():
                old_row = await connection.fetchrow(
                    f"SELECT * FROM {self.schema}.artifact_versions WHERE id=$1 AND project_id=$2", old_id, project.id,
                )
                if old_row is None:
                    raise LookupError(f"artifact version not found: {old_id}")
                old = self._artifact(old_row)
                replacement.project_id, replacement.artifact_id = project.id, old.artifact_id
                staged = await connection.fetchrow(
                    f"SELECT * FROM {self.schema}.artifact_versions WHERE id=$1 AND project_id=$2",
                    replacement.id, project.id,
                )
                if staged is not None:
                    replacement.version = int(staged["version"])
                    replacement.status = "ready"
                    await connection.execute(
                        f"""UPDATE {self.schema}.artifact_versions
                        SET artifact_id=$2,status='ready',artifact_type=$3,uri=$4,title=$5,summary=$6,
                            content_digest=$7,generation_spec_digest=$8,provider_id=$9,provider_version=$10,
                            plugin_id=$11,plugin_version=$12,provenance=$13::jsonb,metadata=$14::jsonb
                        WHERE id=$1""",
                        replacement.id, old.artifact_id, replacement.type, replacement.uri,
                        replacement.title, replacement.summary, replacement.content_digest,
                        replacement.generation_spec_digest, replacement.provider_id,
                        replacement.provider_version, replacement.plugin_id, replacement.plugin_version,
                        json.dumps(replacement.provenance), json.dumps(replacement.metadata),
                    )
                else:
                    replacement.version = await connection.fetchval(
                        f"SELECT COALESCE(max(version),0)+1 FROM {self.schema}.artifact_versions WHERE artifact_id=$1",
                        old.artifact_id,
                    )
                    replacement.status = "ready"
                    await self._insert_artifact(connection, replacement)
                replacement_ids[old_id] = replacement.id
                next_selections[old.artifact_id] = replacement.id
            created_ids: dict[str, str] = {}
            for step_id, artifact in created_artifacts.items():
                artifact.project_id = project.id
                staged = await connection.fetchrow(
                    f"SELECT id,version FROM {self.schema}.artifact_versions WHERE id=$1 AND project_id=$2",
                    artifact.id, project.id,
                )
                artifact.status = "ready"
                if staged is not None:
                    artifact.version = int(staged["version"])
                    await connection.execute(
                        f"""UPDATE {self.schema}.artifact_versions
                        SET artifact_id=$2,status='ready',artifact_type=$3,uri=$4,title=$5,summary=$6,
                            content_digest=$7,generation_spec_digest=$8,provider_id=$9,provider_version=$10,
                            plugin_id=$11,plugin_version=$12,provenance=$13::jsonb,metadata=$14::jsonb
                        WHERE id=$1""",
                        artifact.id, artifact.artifact_id, artifact.type, artifact.uri,
                        artifact.title, artifact.summary, artifact.content_digest,
                        artifact.generation_spec_digest, artifact.provider_id,
                        artifact.provider_version, artifact.plugin_id, artifact.plugin_version,
                        json.dumps(artifact.provenance), json.dumps(artifact.metadata),
                    )
                else:
                    artifact.version = await connection.fetchval(
                        f"SELECT COALESCE(max(version),0)+1 FROM {self.schema}.artifact_versions WHERE artifact_id=$1",
                        artifact.artifact_id,
                    )
                    await self._insert_artifact(connection, artifact)
                next_selections[artifact.artifact_id] = artifact.id
                created_ids[step_id] = artifact.id
            edges = await connection.fetch(f"SELECT * FROM {self.schema}.artifact_edges WHERE project_id=$1", project.id)
            existing_edges = {
                (str(row["source_version_id"]), str(row["target_version_id"]))
                for row in edges
            }
            for row_edge in edges:
                edge = ArtifactDependency(**dict(row_edge))
                target = replacement_ids.get(edge.target_version_id)
                if target is None:
                    continue
                source = edge.source_version_id
                if source == target or (source, target) in existing_edges:
                    continue
                await self._insert_edge(connection, ArtifactDependency(
                    project_id=project.id, source_version_id=source, target_version_id=target,
                    relation=edge.relation, invalidation_policy=edge.invalidation_policy,
                ))
                existing_edges.add((source, target))
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
            for item in plan.items:
                target = output_by_step.get(item.step_id)
                if target is None:
                    continue
                for dependency in item.depends_on:
                    source = output_by_step.get(dependency)
                    if source is None or source == target or (source, target) in existing_edges:
                        continue
                    await self._insert_edge(connection, ArtifactDependency(
                        project_id=project.id,
                        source_version_id=source,
                        target_version_id=target,
                        relation="build_step_dependency",
                    ))
                    existing_edges.add((source, target))
            timeline_version_id = replacement_ids.get(
                current.timeline_version_id, current.timeline_version_id,
            )
            for artifact in created_artifacts.values():
                if artifact.type == "timeline":
                    timeline_version_id = artifact.id
            version = ProjectVersion(
                project_id=project.id, parent_version_id=current.id,
                timeline_version_id=timeline_version_id,
                selections=next_selections, change_request_id=plan.change_request_id,
                video_spec_revision_id=(
                    plan.video_spec_revision_id or current.video_spec_revision_id
                ),
            )
            await self._commit_version(connection, project, version)
            for item in validation_results:
                await connection.execute(
                    f"""INSERT INTO {self.schema}.validation_results
                    (id,project_id,build_id,artifact_version_id,validator_id,passed,score,issues,metadata,created_at)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb,$10)""",
                    item.id, item.project_id, item.build_id, item.artifact_version_id,
                    item.validator_id, item.passed, item.score, json.dumps(item.issues),
                    json.dumps(item.metadata), item.created_at,
                )
            build.status, build.progress, build.message = "completed", 1.0, "Build committed"
            build.project_version_id, build.updated_at = version.id, now()
            await connection.execute(
                f"""UPDATE {self.schema}.builds SET status=$2,progress=$3,message=$4,
                project_version_id=$5,updated_at=$6 WHERE id=$1""",
                build.id, build.status, build.progress, build.message, version.id, build.updated_at,
            )
            await self._append_event(connection, project.id, "project.version_committed", {
                "build_id": build.id, "project_version_id": version.id,
                "parent_version_id": version.parent_version_id,
            })
        return build, version

    async def select_artifact(
        self, *, project_id: str, version_id: str, base_version_id: str, idempotency_key: str,
    ) -> tuple[MediaArtifactVersion, ProjectVersion]:
        async with self.pool.acquire() as connection, connection.transaction():
            existing = await self._idempotent_result(connection, project_id, "select", idempotency_key)
            if existing:
                version = await self._get_version(connection, existing)
                artifact = self._artifact(await connection.fetchrow(
                    f"SELECT * FROM {self.schema}.artifact_versions WHERE id=$1", version_id,
                ))
                return artifact, version
            project = await self._locked_project(connection, project_id)
            self._assert_version(project, base_version_id)
            row = await connection.fetchrow(
                f"SELECT * FROM {self.schema}.artifact_versions WHERE id=$1 AND project_id=$2", version_id, project_id,
            )
            if row is None:
                raise LookupError("artifact version not found")
            artifact = self._artifact(row)
            current = await self._get_version(connection, project.current_version_id)
            version = ProjectVersion(
                project_id=project_id, parent_version_id=current.id,
                timeline_version_id=version_id if artifact.type == "timeline" else current.timeline_version_id,
                selections={**current.selections, artifact.artifact_id: version_id},
            )
            await self._commit_version(connection, project, version)
            await self._remember(connection, project_id, "select", idempotency_key, version.id)
            await self._append_event(connection, project_id, "artifact.selected", {
                "artifact_id": artifact.artifact_id, "artifact_version_id": artifact.id,
                "project_version_id": version.id,
            })
        return artifact, version

    async def restore_project_version(
        self, *, project_id: str, restore_version_id: str, base_version_id: str, idempotency_key: str,
    ) -> ProjectVersion:
        async with self.pool.acquire() as connection, connection.transaction():
            existing = await self._idempotent_result(connection, project_id, "restore", idempotency_key)
            if existing:
                return await self._get_version(connection, existing)
            project = await self._locked_project(connection, project_id)
            self._assert_version(project, base_version_id)
            restored = await self._get_version(connection, restore_version_id)
            if restored.project_id != project_id:
                raise LookupError("project version not found")
            current = await self._get_version(connection, project.current_version_id)
            version = ProjectVersion(
                project_id=project_id, parent_version_id=current.id,
                timeline_version_id=restored.timeline_version_id, selections=dict(restored.selections),
            )
            await self._commit_version(connection, project, version)
            await self._remember(connection, project_id, "restore", idempotency_key, version.id)
            await self._append_event(connection, project_id, "project.version_restored", {
                "source_project_version_id": restore_version_id, "project_version_id": version.id,
            })
        return version

    async def create_export(
        self, *, project_id: str, base_version_id: str, idempotency_key: str, format: str,
    ) -> ExportRecord:
        async with self.pool.acquire() as connection, connection.transaction():
            existing = await connection.fetchrow(
                f"SELECT * FROM {self.schema}.exports WHERE project_id=$1 AND idempotency_key=$2",
                project_id, idempotency_key,
            )
            if existing:
                return ExportRecord(**dict(existing))
            project = await self._locked_project(connection, project_id)
            self._assert_version(project, base_version_id)
            export = ExportRecord(
                project_id=project_id, project_version_id=base_version_id,
                idempotency_key=idempotency_key, format=format,
            )
            if format == "mp4":
                selected = await connection.fetchrow(
                    f"""SELECT artifact.uri FROM {self.schema}.project_version_artifacts selection
                    JOIN {self.schema}.artifact_versions artifact
                      ON artifact.id=selection.artifact_version_id
                    WHERE selection.project_version_id=$1
                      AND artifact.artifact_type=ANY($2::text[]) AND artifact.uri IS NOT NULL
                    ORDER BY CASE artifact.artifact_type WHEN 'final_video' THEN 0
                      WHEN 'video_mixed' THEN 1 ELSE 2 END, artifact.created_at DESC LIMIT 1""",
                    base_version_id, ["final_video", "video_mixed", "video_assembled"],
                )
                if selected:
                    export.status, export.uri = "completed", selected["uri"]
            await connection.execute(
                f"""INSERT INTO {self.schema}.exports
                (id,project_id,project_version_id,idempotency_key,format,status,uri,created_at)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8)""",
                export.id, export.project_id, export.project_version_id, export.idempotency_key,
                export.format, export.status, export.uri, export.created_at,
            )
            await self._append_event(connection, project_id, f"export.{export.status}", {
                "export_id": export.id, "format": format, "uri": export.uri,
            })
        return export

    async def latest_event_sequence(self, project_id: str) -> int:
        await self.get_project(project_id)
        value = await self.pool.fetchval(
            f"SELECT COALESCE(MAX(sequence), 0) FROM {self.schema}.project_events WHERE project_id=$1",
            project_id,
        )
        return int(value or 0)

    async def list_events(self, project_id: str, after: int = 0) -> list[ProjectEvent]:
        await self.get_project(project_id)
        rows = await self.pool.fetch(
            f"""SELECT project_id,sequence,event_type AS type,payload,created_at
            FROM {self.schema}.project_events WHERE project_id=$1 AND sequence>$2 ORDER BY sequence""",
            project_id, after,
        )
        events = []
        for row in rows:
            values = dict(row)
            if isinstance(values.get("payload"), str):
                values["payload"] = json.loads(values["payload"])
            events.append(ProjectEvent(**values))
        return events

    async def active_builds(self, project_id: str) -> list[Build]:
        rows = await self.pool.fetch(
            f"SELECT * FROM {self.schema}.builds WHERE project_id=$1 AND status=ANY($2::text[]) ORDER BY created_at",
            project_id, ["queued", "running", "waiting_external", "waiting_agent"],
        )
        return [self._build(row) for row in rows]

    async def list_builds(self, project_id: str) -> list[Build]:
        await self.get_project(project_id)
        rows = await self.pool.fetch(
            f"SELECT * FROM {self.schema}.builds WHERE project_id=$1 ORDER BY created_at DESC",
            project_id,
        )
        return [self._build(row) for row in rows]

    async def _locked_project(self, connection, project_id: str) -> Project:
        row = await connection.fetchrow(f"SELECT * FROM {self.schema}.projects WHERE id=$1 FOR UPDATE", project_id)
        if row is None:
            raise LookupError("project not found")
        return Project(**dict(row))

    async def _insert_version(self, connection, version: ProjectVersion) -> None:
        await connection.execute(
            f"""INSERT INTO {self.schema}.project_versions
            (id,project_id,parent_version_id,timeline_version_id,change_request_id,
             video_spec_revision_id,created_at)
            VALUES($1,$2,$3,$4,$5,$6,$7)""",
            version.id, version.project_id, version.parent_version_id,
            version.timeline_version_id, version.change_request_id,
            version.video_spec_revision_id, version.created_at,
        )
        if version.selections:
            await connection.executemany(
                f"""INSERT INTO {self.schema}.project_version_artifacts
                (project_version_id,artifact_id,artifact_version_id) VALUES($1,$2,$3)""",
                [(version.id, artifact_id, selected_id) for artifact_id, selected_id in version.selections.items()],
            )

    async def _get_version(self, connection, version_id: str) -> ProjectVersion:
        row = await connection.fetchrow(f"SELECT * FROM {self.schema}.project_versions WHERE id=$1", version_id)
        if row is None:
            raise LookupError("project version not found")
        selected = await connection.fetch(
            f"SELECT artifact_id,artifact_version_id FROM {self.schema}.project_version_artifacts WHERE project_version_id=$1",
            version_id,
        )
        return ProjectVersion(**dict(row), selections={item["artifact_id"]: item["artifact_version_id"] for item in selected})

    async def _commit_version(self, connection, project: Project, version: ProjectVersion) -> None:
        await self._insert_version(connection, version)
        project.current_version_id, project.updated_at = version.id, now()
        await connection.execute(
            f"UPDATE {self.schema}.projects SET current_version_id=$2,updated_at=$3 WHERE id=$1",
            project.id, project.current_version_id, project.updated_at,
        )

    async def _insert_artifact(self, connection, artifact: MediaArtifactVersion) -> None:
        await connection.execute(
            f"""INSERT INTO {self.schema}.artifact_versions
            (id,artifact_id,project_id,artifact_type,version,status,uri,title,summary,content_digest,
             generation_spec_digest,provider_id,provider_version,plugin_id,plugin_version,
             provenance,metadata,created_at)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16::jsonb,$17::jsonb,$18)""",
            artifact.id, artifact.artifact_id, artifact.project_id, artifact.type,
            artifact.version, artifact.status, artifact.uri, artifact.title, artifact.summary,
            artifact.content_digest,
            artifact.generation_spec_digest, artifact.provider_id, artifact.provider_version,
            artifact.plugin_id, artifact.plugin_version, json.dumps(artifact.provenance),
            json.dumps(artifact.metadata), artifact.created_at,
        )

    async def _insert_edge(self, connection, edge: ArtifactDependency) -> None:
        await connection.execute(
            f"""INSERT INTO {self.schema}.artifact_edges
            (id,project_id,source_version_id,target_version_id,relation,invalidation_policy,created_at)
            VALUES($1,$2,$3,$4,$5,$6,$7)""",
            edge.id, edge.project_id, edge.source_version_id, edge.target_version_id,
            edge.relation, edge.invalidation_policy.value, edge.created_at,
        )

    async def _get_plan(self, connection, plan_id: str) -> RebuildPlan:
        row = await connection.fetchrow(f"SELECT * FROM {self.schema}.rebuild_plans WHERE id=$1", plan_id)
        if row is None:
            raise LookupError("rebuild plan not found")
        items = row["items"] if isinstance(row["items"], list) else json.loads(row["items"])
        values = dict(row)
        video_spec = values.get("video_spec")
        if isinstance(video_spec, str):
            video_spec = json.loads(video_spec)
        project_intent = values.get("project_intent")
        if isinstance(project_intent, str):
            project_intent = json.loads(project_intent)
        next_checkpoint = values.get("next_checkpoint")
        if isinstance(next_checkpoint, str):
            next_checkpoint = json.loads(next_checkpoint)
        return RebuildPlan(**{
            **values,
            "video_spec": video_spec,
            "project_intent": project_intent,
            "next_checkpoint": next_checkpoint,
            "items": [RebuildPlanItem(**item) for item in items],
        })

    async def _insert_spec_revision(
        self, connection, revision: VideoSpecRevision,
    ) -> None:
        await connection.execute(
            f"""INSERT INTO {self.schema}.video_spec_revisions
            (id,project_id,revision,parent_revision_id,content,resolved_sections,
             unresolved_sections,source_artifact_version_ids,checkpoint_id,created_by,
             complete,created_at)
            VALUES($1,$2,$3,$4,$5::jsonb,$6::jsonb,$7::jsonb,$8::jsonb,$9,$10,$11,$12)""",
            revision.id, revision.project_id, revision.revision, revision.parent_revision_id,
            json.dumps(revision.content), json.dumps(revision.resolved_sections),
            json.dumps(revision.unresolved_sections),
            json.dumps(revision.source_artifact_version_ids), revision.checkpoint_id,
            revision.created_by, revision.complete, revision.created_at,
        )

    async def _insert_plan_revision(
        self, connection, revision: BuildPlanRevision,
    ) -> None:
        await connection.execute(
            f"""INSERT INTO {self.schema}.build_plan_revisions
            (id,plan_id,project_id,revision,base_revision,checkpoint_id,
             video_spec_revision_id,added_step_ids,cancelled_step_ids,reason,created_at)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb,$10,$11)""",
            revision.id, revision.plan_id, revision.project_id, revision.revision,
            revision.base_revision, revision.checkpoint_id, revision.video_spec_revision_id,
            json.dumps(revision.added_step_ids), json.dumps(revision.cancelled_step_ids),
            revision.reason, revision.created_at,
        )

    async def _append_event(self, connection, project_id: str, event_type: str, payload: dict) -> ProjectEvent:
        await connection.execute("SELECT pg_advisory_xact_lock(hashtext($1))", project_id)
        sequence = await connection.fetchval(
            f"SELECT COALESCE(max(sequence),0)+1 FROM {self.schema}.project_events WHERE project_id=$1", project_id,
        )
        event = ProjectEvent(project_id=project_id, sequence=sequence, type=event_type, payload=payload)
        await connection.execute(
            f"""INSERT INTO {self.schema}.project_events(project_id,sequence,event_type,payload,created_at)
            VALUES($1,$2,$3,$4::jsonb,$5)""",
            project_id, sequence, event_type, json.dumps(payload), event.created_at,
        )
        return event

    async def _idempotent_result(self, connection, project_id: str, operation: str, key: str) -> str | None:
        return await connection.fetchval(
            f"""SELECT result_id FROM {self.schema}.operation_idempotency
            WHERE project_id=$1 AND operation=$2 AND idempotency_key=$3""",
            project_id, operation, key,
        )

    async def _remember(self, connection, project_id: str, operation: str, key: str, result_id: str) -> None:
        await connection.execute(
            f"""INSERT INTO {self.schema}.operation_idempotency
            (project_id,operation,idempotency_key,result_id) VALUES($1,$2,$3,$4)""",
            project_id, operation, key, result_id,
        )

    @staticmethod
    def _assert_version(project: Project, expected: str) -> None:
        if project.current_version_id != expected:
            raise ProjectVersionConflict(expected, project.current_version_id)

    @staticmethod
    def _artifact(row) -> MediaArtifactVersion:
        values = dict(row)
        values["type"] = values.pop("artifact_type")
        for key in ("provenance", "metadata"):
            if isinstance(values.get(key), str):
                values[key] = json.loads(values[key])
        return MediaArtifactVersion(**values)

    @staticmethod
    def _build(row) -> Build:
        raw = dict(row)
        values = {key: raw[key] for key in Build.model_fields if key in raw}
        return Build(**values)

    @staticmethod
    def _step(row) -> BuildStep:
        item = dict(row)
        for key in ("resolved_skills", "skill_context"):
            if isinstance(item.get(key), str):
                item[key] = json.loads(item[key])
        return BuildStep(**item)

    @staticmethod
    def _validation(row) -> ValidationResult:
        values = dict(row)
        for key in ("issues", "metadata"):
            if isinstance(values.get(key), str):
                values[key] = json.loads(values[key])
        return ValidationResult(**values)

    @staticmethod
    def _spec_revision(row) -> VideoSpecRevision:
        values = dict(row)
        for key in (
            "content", "resolved_sections", "unresolved_sections",
            "source_artifact_version_ids",
        ):
            if isinstance(values.get(key), str):
                values[key] = json.loads(values[key])
        return VideoSpecRevision(**values)

    @staticmethod
    def _plan_revision(row) -> BuildPlanRevision:
        values = dict(row)
        for key in ("added_step_ids", "cancelled_step_ids"):
            if isinstance(values.get(key), str):
                values[key] = json.loads(values[key])
        return BuildPlanRevision(**values)

    @staticmethod
    def _checkpoint(row) -> PlanCheckpoint:
        values = dict(row)
        for key in (
            "required_artifact_types", "artifact_version_ids", "artifact_summaries",
            "resolved_sections", "unresolved_sections",
        ):
            if isinstance(values.get(key), str):
                values[key] = json.loads(values[key])
        return PlanCheckpoint(**values)
