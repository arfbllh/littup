from __future__ import annotations

import glob
import os
from dataclasses import dataclass

import yaml
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, TemplateNotFoundError
from app.draft.templates.schema import DraftTemplate


class TemplateValidationError(AppError):
    def __init__(self, message: str, *, file_path: str = "") -> None:
        detail = f"{message} (file: {file_path})" if file_path else message
        super().__init__(detail, code="TEMPLATE_VALIDATION_ERROR", status_code=422)


@dataclass
class TemplateInfo:
    id: str
    latest_version: int
    display_name: str
    description: str
    fingerprint: str


class TemplateRegistry:
    def __init__(self) -> None:
        self._templates: dict[str, DraftTemplate] = {}
        self._cache: dict[tuple[str, int], DraftTemplate] = {}

    # ------------------------------------------------------------------
    # Disk loading
    # ------------------------------------------------------------------

    def load_from_disk(self, config_dir: str) -> None:
        pattern = os.path.join(config_dir, "*.yaml")
        for file_path in glob.glob(pattern):
            with open(file_path, "r", encoding="utf-8") as fh:
                raw = fh.read()
            try:
                data = yaml.safe_load(raw)
                template = DraftTemplate.model_validate(data)
            except (PydanticValidationError, yaml.YAMLError, Exception) as exc:
                raise TemplateValidationError(str(exc), file_path=file_path) from exc
            self._templates[template.id] = template

    # ------------------------------------------------------------------
    # DB sync
    # ------------------------------------------------------------------

    async def sync_to_db(self, session: AsyncSession) -> list[dict]:
        results: list[dict] = []

        for template in self._templates.values():
            fingerprint = template.compute_fingerprint()

            row = (
                await session.execute(
                    text(
                        "SELECT version, prompt_fingerprint FROM app.templates "
                        "WHERE template_id = :tid ORDER BY version DESC LIMIT 1"
                    ),
                    {"tid": template.id},
                )
            ).fetchone()

            if row is not None and row[1] == fingerprint:
                # Already up-to-date; cache existing version
                existing_version: int = row[0]
                cache_key = (template.id, existing_version)
                if cache_key not in self._cache:
                    versioned = template.model_copy(update={"version": existing_version})
                    self._cache[cache_key] = versioned
                results.append(
                    {
                        "id": template.id,
                        "version": existing_version,
                        "fingerprint": fingerprint,
                    }
                )
                continue

            max_version: int = row[0] if row is not None else 0
            new_version = max_version + 1

            import json

            await session.execute(
                text(
                    "INSERT INTO app.templates "
                    "(template_id, version, yaml_body, system_prompt, appended_rules, prompt_fingerprint) "
                    "VALUES (:tid, :ver, :yaml_body, :system_prompt, CAST(:appended_rules AS jsonb), :fingerprint)"
                ),
                {
                    "tid": template.id,
                    "ver": new_version,
                    "yaml_body": yaml.dump(template.model_dump(mode="json")),
                    "system_prompt": template.system_prompt,
                    "appended_rules": json.dumps(template.appended_rules),
                    "fingerprint": fingerprint,
                },
            )

            versioned = template.model_copy(update={"version": new_version})
            self._cache[(template.id, new_version)] = versioned

            results.append(
                {
                    "id": template.id,
                    "version": new_version,
                    "fingerprint": fingerprint,
                }
            )

        return results

    # ------------------------------------------------------------------
    # Retrieval helpers
    # ------------------------------------------------------------------

    def _parse_from_row(
        self,
        template_id: str,
        version: int,
        yaml_body: str,
        system_prompt: str | None,
        appended_rules: list,
        prompt_fingerprint: str,
    ) -> DraftTemplate:
        data = yaml.safe_load(yaml_body)
        template = DraftTemplate.model_validate(data)
        # Overlay DB-authoritative fields
        if system_prompt is not None:
            template = template.model_copy(update={"system_prompt": system_prompt})
        if appended_rules:
            template = template.model_copy(update={"appended_rules": appended_rules})
        template = template.model_copy(update={"version": version})
        return template

    async def get_latest(
        self, template_id: str, session: AsyncSession
    ) -> DraftTemplate:
        row = (
            await session.execute(
                text(
                    "SELECT template_id, version, yaml_body, system_prompt, "
                    "appended_rules, prompt_fingerprint "
                    "FROM app.templates WHERE template_id = :tid "
                    "ORDER BY version DESC LIMIT 1"
                ),
                {"tid": template_id},
            )
        ).fetchone()

        if row is None:
            raise TemplateNotFoundError(
                f"Template '{template_id}' not found."
            )

        _, version, yaml_body, system_prompt, appended_rules, prompt_fingerprint = row

        cache_key = (template_id, version)
        if cache_key in self._cache:
            return self._cache[cache_key]

        template = self._parse_from_row(
            template_id, version, yaml_body, system_prompt, appended_rules, prompt_fingerprint
        )
        self._cache[cache_key] = template
        return template

    async def get_by_version(
        self, template_id: str, version: int, session: AsyncSession
    ) -> DraftTemplate:
        cache_key = (template_id, version)
        if cache_key in self._cache:
            return self._cache[cache_key]

        row = (
            await session.execute(
                text(
                    "SELECT template_id, version, yaml_body, system_prompt, "
                    "appended_rules, prompt_fingerprint "
                    "FROM app.templates WHERE template_id = :tid AND version = :ver"
                ),
                {"tid": template_id, "ver": version},
            )
        ).fetchone()

        if row is None:
            raise TemplateNotFoundError(
                f"Template '{template_id}' version {version} not found."
            )

        _, ver, yaml_body, system_prompt, appended_rules, prompt_fingerprint = row

        template = self._parse_from_row(
            template_id, ver, yaml_body, system_prompt, appended_rules, prompt_fingerprint
        )
        self._cache[cache_key] = template
        return template

    async def list_templates(self, session: AsyncSession) -> list[TemplateInfo]:
        rows = (
            await session.execute(
                text(
                    "SELECT DISTINCT ON (template_id) template_id, version, prompt_fingerprint "
                    "FROM app.templates ORDER BY template_id, version DESC"
                )
            )
        ).fetchall()

        infos: list[TemplateInfo] = []
        for row in rows:
            tid, version, fingerprint = row
            in_memory = self._templates.get(tid)
            display_name = in_memory.display_name if in_memory else tid
            description = in_memory.description if in_memory else ""
            infos.append(
                TemplateInfo(
                    id=tid,
                    latest_version=version,
                    display_name=display_name,
                    description=description,
                    fingerprint=fingerprint,
                )
            )
        return infos
