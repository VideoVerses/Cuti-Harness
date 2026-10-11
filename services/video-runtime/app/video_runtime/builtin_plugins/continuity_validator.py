from __future__ import annotations

import asyncio
import json
import os

import httpx

from ..models import MediaArtifactVersion, ValidationResult
from ..plugins import BaseVideoPlugin, PluginContext


async def _video_info(uri: str) -> dict:
    """Probe local self-hosted outputs without depending on Cuti Media Service."""
    from app.utils import media_service_client as msc

    return await msc.video_info(uri)


def _scene_visual_validation_enabled() -> bool:
    configured = os.getenv(
        "VIDEO_SCENE_REFERENCE_VISUAL_VALIDATION_ENABLED", "",
    ).strip().lower()
    if configured:
        return configured in {"1", "true", "yes"}
    # The local/hosted Video profile already needs an OpenAI key for DeepSeek.
    # Enable the inexpensive low-detail scene check there, while keeping
    # provider-mock and offline test profiles deterministic.
    return bool(os.getenv("OPENAI_API_KEY", "").strip())


def _response_text(payload: dict) -> str:
    values: list[str] = []
    for output in payload.get("output") or []:
        if not isinstance(output, dict):
            continue
        for content in output.get("content") or []:
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                values.append(content["text"])
    return "\n".join(values).strip()


async def _inspect_scene_reference(uri: str) -> dict:
    """Classify the rendered pixels; prompt auditing alone cannot catch hallucinations."""
    # The Responses API cannot fetch a loopback `/files/*` URL.  Reuse Cuti's
    # storage-aware inliner so local self-hosted builds and public/S3 builds
    # follow the same validator path without a self-HTTP fetch or false 400.
    from app.utils.file_utils import inline_local_image_url_for_llm

    inspectable_uri = await inline_local_image_url_for_llm(uri)
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for scene visual validation")
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = (
        os.getenv("VIDEO_SCENE_REFERENCE_VALIDATOR_MODEL")
        or os.getenv("VIDEO_AGENT_MODEL")
        or "gpt-5.6-terra"
    )
    schema = {
        "type": "object",
        "properties": {
            "people_present": {"type": "boolean"},
            "product_present": {"type": "boolean"},
            "contact_sheet_present": {"type": "boolean"},
            "reason": {"type": "string"},
        },
        "required": [
            "people_present", "product_present", "contact_sheet_present", "reason",
        ],
        "additionalProperties": False,
    }
    body = {
        "model": model,
        "input": [{
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": (
                        "Inspect this generated environment reference. Set people_present=true "
                        "if any person, face, body, silhouette, portrait, mannequin, or character "
                        "sheet appears. Set product_present=true if a deliberately staged bottle, "
                        "package, branded consumer product, product sheet, or commercial hero object "
                        "appears. Ordinary furniture and architecture are not products. Set "
                        "contact_sheet_present=true for grids, panels, or multiple design views."
                    ),
                },
                {
                    "type": "input_image",
                    "image_url": inspectable_uri,
                    "detail": "low",
                },
            ],
        }],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "scene_isolation",
                "strict": True,
                "schema": schema,
            },
        },
        "max_output_tokens": 200,
    }
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            f"{base_url}/responses",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
        )
        response.raise_for_status()
        payload = response.json()
    raw = _response_text(payload)
    if not raw:
        raise RuntimeError("scene visual validator returned no structured output")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or not all(
        isinstance(parsed.get(key), bool)
        for key in ("people_present", "product_present", "contact_sheet_present")
    ):
        raise RuntimeError("scene visual validator returned an invalid result")
    return parsed


async def _inspect_character_reference(uri: str, expected_description: str) -> dict:
    """Verify that a generated cast sheet actually follows its locked character brief."""
    from app.utils.file_utils import inline_local_image_url_for_llm

    inspectable_uri = await inline_local_image_url_for_llm(uri)
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for character visual validation")
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = (
        os.getenv("VIDEO_CHARACTER_REFERENCE_VALIDATOR_MODEL")
        or os.getenv("VIDEO_SCENE_REFERENCE_VALIDATOR_MODEL")
        or os.getenv("VIDEO_AGENT_MODEL")
        or "gpt-5.6-terra"
    )
    schema = {
        "type": "object",
        "properties": {
            "character_present": {"type": "boolean"},
            "matches_expected_description": {"type": "boolean"},
            "product_present": {"type": "boolean"},
            "environment_dominant": {"type": "boolean"},
            "reason": {"type": "string"},
        },
        "required": [
            "character_present", "matches_expected_description", "product_present",
            "environment_dominant", "reason",
        ],
        "additionalProperties": False,
    }
    body = {
        "model": model,
        "input": [{
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": (
                        "Inspect this recurring-character identity reference. Compare only "
                        "observable character constraints in the expected description: number "
                        "of characters, apparent age, presented gender, species, face/hair, body "
                        "proportions, and clothing. Set matches_expected_description=false for a "
                        "material contradiction such as man instead of woman, wrong species, "
                        "missing principal character, or clearly different locked costume. Do not "
                        "fail for harmless pose, camera, or lighting differences. Also report any "
                        "advertised product and whether a full environment dominates the sheet.\n"
                        f"Expected description:\n{expected_description[:3000]}"
                    ),
                },
                {"type": "input_image", "image_url": inspectable_uri, "detail": "low"},
            ],
        }],
        "text": {"format": {
            "type": "json_schema", "name": "character_reference_identity",
            "strict": True, "schema": schema,
        }},
        "max_output_tokens": 250,
    }
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            f"{base_url}/responses",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
        )
        response.raise_for_status()
        raw = _response_text(response.json())
    if not raw:
        raise RuntimeError("character visual validator returned no structured output")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or not all(
        isinstance(parsed.get(key), bool)
        for key in (
            "character_present", "matches_expected_description", "product_present",
            "environment_dominant",
        )
    ):
        raise RuntimeError("character visual validator returned an invalid result")
    return parsed


class ContinuityValidatorPlugin(BaseVideoPlugin):
    """Structural checks always run; the existing Cuti VLM check is optional."""

    async def validate_artifact(
        self, context: PluginContext, artifact: MediaArtifactVersion,
    ) -> list[ValidationResult]:
        issues: list[str] = []
        metadata: dict = {"mode": "structural"}
        parameters = artifact.metadata.get("generation_parameters") or {}
        artifact_role = str(
            parameters.get("artifact_role")
            or artifact.metadata.get("artifact_role")
            or ""
        )

        if artifact_role == "character_setting_reference":
            metadata = {"mode": "character-reference-identity"}
            if not artifact.uri:
                issues.append("character setting reference has no image URI")
            resolved = artifact.metadata.get("resolved_generation_parameters") or parameters
            input_images = [
                value
                for key in ("images", "image_urls", "reference_urls")
                for value in (resolved.get(key) or [])
                if value
            ]
            if input_images:
                issues.append("character setting reference received product image inputs")
            if artifact.metadata.get("skill_prompt_applied") is not False:
                issues.append("Workflow Skill instructions were injected into the leaf character prompt")
            planned_prompt = str(parameters.get("prompt") or "").strip()
            final_prompt = str(artifact.metadata.get("final_prompt") or "").strip()
            if not planned_prompt or final_prompt != planned_prompt:
                issues.append("character setting reference provider prompt differs from the compiled prompt")
            normalized_prompt = planned_prompt.casefold()
            if (
                "recurring-character identity reference" not in normalized_prompt
                or "do not include the advertised product" not in normalized_prompt
            ):
                issues.append("character setting reference prompt lost its isolated identity contract")
            if not issues and artifact.uri and _scene_visual_validation_enabled():
                try:
                    visual = await _inspect_character_reference(
                        str(artifact.uri), planned_prompt,
                    )
                    metadata["visual_result"] = visual
                    if not visual["character_present"]:
                        issues.append("rendered character setting reference contains no character")
                    if not visual["matches_expected_description"]:
                        issues.append(
                            "rendered character setting reference does not match the locked "
                            "character definition: "
                            + str(visual.get("reason") or "material identity mismatch")
                        )
                    if visual["product_present"]:
                        issues.append("rendered character setting reference contains the advertised product")
                    if visual["environment_dominant"]:
                        issues.append("rendered character setting reference is dominated by a scene")
                except Exception as exc:
                    issues.append(f"character visual identity verification is incomplete: {exc}")
            return [ValidationResult(
                project_id=artifact.project_id,
                build_id=context.build_id or "",
                artifact_version_id=artifact.id,
                validator_id="cuti.continuity.character-reference-identity",
                passed=not issues,
                issues=issues,
                metadata=metadata,
            )]

        # A scenario setting reference is deliberately an empty environment.
        # Catch the implementation failure that previously appended the full
        # Workflow Skill (including its character and product sections) to this
        # leaf image prompt, or attached identity images to the scene request.
        if artifact_role == "scene_setting_reference":
            metadata = {"mode": "scene-reference-isolation"}
            if not artifact.uri:
                issues.append("scene setting reference has no image URI")
            resolved = artifact.metadata.get("resolved_generation_parameters") or parameters
            input_images = [
                value
                for key in ("images", "image_urls", "reference_urls")
                for value in (resolved.get(key) or [])
                if value
            ]
            if input_images:
                issues.append("scene setting reference received character or product image inputs")
            if artifact.metadata.get("skill_prompt_applied") is not False:
                issues.append("Workflow Skill instructions were injected into the leaf scene prompt")
            planned_prompt = str(parameters.get("prompt") or "").strip()
            final_prompt = str(artifact.metadata.get("final_prompt") or "").strip()
            if not planned_prompt or final_prompt != planned_prompt:
                issues.append("scene setting reference provider prompt differs from the compiled prompt")
            normalized_prompt = planned_prompt.casefold()
            if (
                "unoccupied environment-only" not in normalized_prompt
                or "single coherent" not in normalized_prompt
            ):
                issues.append("scene setting reference prompt does not enforce an isolated environment")
            if not issues and artifact.uri and _scene_visual_validation_enabled():
                try:
                    visual = await _inspect_scene_reference(str(artifact.uri))
                    metadata["visual_result"] = visual
                    if visual["people_present"]:
                        issues.append("rendered scene setting reference contains people or characters")
                    if visual["product_present"]:
                        issues.append("rendered scene setting reference contains a staged product")
                    if visual["contact_sheet_present"]:
                        issues.append("rendered scene setting reference is a multi-panel/contact sheet")
                except Exception as exc:
                    issues.append(f"scene visual isolation verification is incomplete: {exc}")
            return [ValidationResult(
                project_id=artifact.project_id,
                build_id=context.build_id or "",
                artifact_version_id=artifact.id,
                # Keep this result in the continuity family so a staged build
                # uses its single semantic-repair pass when the image model
                # hallucinates a person, product, or contact-sheet layout.
                validator_id="cuti.continuity.scene-reference-isolation",
                passed=not issues,
                issues=issues,
                metadata=metadata,
            )]

        if artifact.type not in {"timeline", "video_clip", "final_video", "video_mixed", "video_assembled"}:
            return []
        if artifact.type in {"video_clip", "final_video", "video_mixed", "video_assembled"} and not artifact.uri:
            issues.append("video artifact has no playable URI")
        if artifact.type == "timeline":
            timeline = artifact.metadata.get("timeline") or {}
            items = timeline.get("items") or []
            cursor = 0.0
            for item in items:
                start = float(item.get("startSeconds", -1))
                duration = float(item.get("durationSeconds", 0))
                if abs(start - cursor) > 0.001:
                    issues.append("timeline contains a gap or overlap")
                if duration <= 0 or not item.get("artifactVersionId"):
                    issues.append("timeline contains an invalid clip reference")
                cursor = start + duration

        if not issues and artifact.uri and parameters.get("final_output"):
            try:
                info = await _video_info(str(artifact.uri))
                metadata = {"mode": "ffprobe", "videoInfo": info}
                duration = float(info.get("duration") or 0)
                target = float(parameters.get("target_duration_seconds") or 0)
                if duration <= 0:
                    issues.append("final video cannot be decoded")
                elif target and abs(duration - target) > 1:
                    issues.append(f"final video duration {duration:.2f}s differs from target {target:.2f}s")
                if parameters.get("require_audio") and not info.get("has_audio"):
                    issues.append("final video has no audio track")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                issues.append(f"final video probe failed: {exc}")

        return [ValidationResult(
            project_id=artifact.project_id,
            build_id=context.build_id or "",
            artifact_version_id=artifact.id,
            validator_id="cuti.continuity-validator",
            passed=not issues,
            issues=issues,
            metadata=metadata,
        )]
