from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import re
from typing import Iterable

from db import get_db

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "configuration/@config-rgsl"
DEFAULT_SQL_TARGET = "PostgreSQL 17.5+"
DEFAULT_MAX_RELATED_FILES = 12
RELATED_FILE_MAX_CHARS = 12000
ADINSURE_PROFILE_SEED_KEY = "adinsure_implementation"


def default_adinsure_profile_json() -> dict:
    return {
        "version": 1,
        "prompt_title": "AdInsure constructor graph context",
        "prompt_intro": [
            "Treat this project as a configuration constructor: changed files can break linked configuration nodes.",
            "Validate links between dataSource, dataProvider, etlService, route, integrationService, sinkGroup, document, component, view, printoutRelation, notification, and JS package imports.",
        ],
        "constructor_checks": [
            "Check broken configuration references, missing sink/source mappings, missing dataProvider/dataSource links, and invalid document state/transition links.",
            "For SQL review use the configured SQL target from the graph context. Do not require multi-db compatibility unless explicitly requested.",
            "Cross-check schemas, mappings, UI/export fields, postgres query parameters, printout source mappings, notification templates, and JS package imports when related files are provided.",
        ],
        "index": {
            "config_glob": "**/configuration.json",
            "kind_segment": 1,
            "code_segment": 2,
            "code_segment_by_kind": {
                "dataProvider": 3,
                "route": 3,
            },
        },
        "preferred_files": [
            "configuration.json",
            "inputSchema.json",
            "resultSchema.json",
            "outputSchema.json",
            "dataSchema.json",
            "inputMapping.js",
            "resultMapping.js",
            "mapping.js",
            "apply.js",
            "Rule.js",
            "Premium.js",
            "query.postgres.handlebars",
            "UI/configuration.json",
            "UI/UiSchema.json",
            "UI/ActionsContent.json",
            "UI/ResultsContent.json",
            "UI/FiltersContent.json",
            "translation/translation.csv",
        ],
        "text_extensions": [".json", ".js", ".handlebars", ".csv", ".html", ".txt", ".md"],
        "js_import": {
            "enabled": True,
            "regex": r"@config-(?:rgsl|system)/[A-Za-z0-9_-]+/[A-Za-z0-9_./-]+",
            "path_prefix": "configuration",
            "extension": ".js",
        },
        "rules": [
            {
                "type": "json_field_link",
                "source_kinds": ["dataSource"],
                "field": "dataProvider.codeName",
                "target_kind": "dataProvider",
                "relation": "dataProvider for {code_name}",
                "required": True,
            },
            {
                "type": "reverse_json_field_link",
                "source_kinds": ["dataProvider"],
                "target_kinds": ["dataSource"],
                "field": "dataProvider.codeName",
                "relation": "dataSource using {code_name}",
            },
            {
                "type": "json_field_link",
                "source_kinds": ["view", "dataExport"],
                "field": "dataSource",
                "target_kind": "dataSource",
                "relation": "{kind}/{code_name} dataSource",
                "required": True,
            },
            {
                "type": "json_field_link",
                "source_kinds": ["view", "dataExport", "printoutRelation"],
                "field": "additionalDataSources",
                "target_kind": "dataSource",
                "relation": "{kind}/{code_name} additionalDataSource",
            },
            {
                "type": "sink_flow",
                "source_kinds": ["etlService", "route", "integrationService", "sinkGroup"],
                "main_data_source_field": "mainDataSource",
                "source_mapping_dir": "sourceMappings",
                "sink_sections": ["sinks", "completionSinks", "errorSinks"],
                "sink_mapping_dir": "sinkMappings",
            },
            {
                "type": "component_links",
                "source_kinds": ["document", "masterEntity", "view", "component"],
                "field": "components",
            },
            {
                "type": "component_owners",
                "source_kinds": ["component"],
                "owner_kinds": ["document", "masterEntity", "view"],
                "field": "components",
            },
            {
                "type": "json_field_link",
                "source_kinds": ["printoutRelation"],
                "field": "targetPrintout",
                "target_kind": "printout",
                "relation": "targetPrintout for {code_name}",
                "required": True,
            },
            {
                "type": "json_field_link_any",
                "source_kinds": ["printoutRelation"],
                "field": "sourceConfigurationName",
                "target_kinds": ["document", "masterEntity"],
                "relation": "source configuration for {code_name}",
                "required": True,
            },
            {
                "type": "named_directory_link",
                "source_kinds": ["printoutRelation"],
                "field": "additionalDataSources",
                "directory": "sourceMappings",
                "relation": "printout sourceMapping {name} for {code_name}",
            },
            {
                "type": "template_files",
                "source_kinds": ["notification"],
                "field": "channel.templates",
                "relation": "notification template for {code_name}",
            },
        ],
    }


@dataclass(frozen=True)
class ReviewProjectSettings:
    project_root: str = ""
    config_path: str = DEFAULT_CONFIG_PATH
    sql_target: str = DEFAULT_SQL_TARGET
    graph_context_enabled: bool = True
    graph_context_max_files: int = DEFAULT_MAX_RELATED_FILES
    profile_id: int | None = None
    profile_name: str = ""
    profile_json: dict | None = None


@dataclass
class ConfigNode:
    package: str
    kind: str
    code_name: str
    rel_path: str
    rel_dir: str
    abs_dir: Path
    config: dict


@dataclass
class RelatedFile:
    path: str
    relation: str
    content: str


@dataclass
class ProjectGraphContext:
    settings: ReviewProjectSettings
    related_files: list[RelatedFile]
    unresolved: list[str]
    notes: list[str]

    def to_prompt_text(self) -> str:
        if not self.related_files and not self.notes and not self.unresolved:
            return ""

        parts = [
            f"## {self.settings.profile_json.get('prompt_title') if self.settings.profile_json else 'Project graph context'}",
            f"- SQL target: {self.settings.sql_target}",
        ]
        if self.settings.profile_name:
            parts.append(f"- Profile: {self.settings.profile_name}")
        profile = self.settings.profile_json or {}
        parts.extend(f"- {line}" for line in profile.get("prompt_intro", []))
        if profile.get("constructor_checks"):
            parts.append("\n### Profile review rules")
            parts.extend(f"- {line}" for line in profile.get("constructor_checks", []))
        if self.notes:
            parts.append("\n### Graph notes")
            parts.extend(f"- {note}" for note in self.notes[:20])
        if self.unresolved:
            parts.append("\n### Unresolved links to verify")
            parts.extend(f"- {item}" for item in self.unresolved[:20])
        if self.related_files:
            parts.append("\n### Related files from local project profile")
            for item in self.related_files:
                parts.append(
                    f"#### {item.path}\nRelation: {item.relation}\n```text\n{item.content}\n```"
                )
        return "\n".join(parts)

    def to_summary(self) -> dict:
        return {
            "enabled": self.settings.graph_context_enabled,
            "project_root": self.settings.project_root,
            "config_path": self.settings.config_path,
            "sql_target": self.settings.sql_target,
            "profile_id": self.settings.profile_id,
            "profile_name": self.settings.profile_name,
            "related_files": [
                {"path": item.path, "relation": item.relation}
                for item in self.related_files
            ],
            "unresolved": self.unresolved,
            "notes": self.notes,
        }


class ConfigIndex:
    def __init__(self, root: Path, config_root: Path):
        self.root = root
        self.config_root = config_root
        self.nodes: list[ConfigNode] = []
        self.by_kind_code: dict[tuple[str, str], list[ConfigNode]] = {}
        self.by_code: dict[str, list[ConfigNode]] = {}

    def add(self, node: ConfigNode) -> None:
        self.nodes.append(node)
        self.by_kind_code.setdefault((node.kind, node.code_name), []).append(node)
        self.by_code.setdefault(node.code_name, []).append(node)

    def find(self, kind: str, code_name: str | None) -> ConfigNode | None:
        if not code_name:
            return None
        nodes = self.by_kind_code.get((kind, code_name), [])
        return nodes[0] if nodes else None

    def find_any(self, code_name: str | None, kinds: Iterable[str]) -> ConfigNode | None:
        if not code_name:
            return None
        for kind in kinds:
            node = self.find(kind, code_name)
            if node:
                return node
        return None

    def find_owner(self, rel_path: str) -> ConfigNode | None:
        normalized = _normalize_rel_path(rel_path)
        candidates = [
            node for node in self.nodes
            if normalized == node.rel_dir or normalized.startswith(node.rel_dir + "/")
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda node: len(node.rel_dir))


def get_review_project_settings() -> ReviewProjectSettings:
    conn = get_db()
    row = conn.execute(
        """
        SELECT review_project_root, review_project_config_path, review_sql_target,
               review_graph_context_enabled, review_graph_context_max_files,
               active_project_profile_id
        FROM review_settings
        WHERE id = 1
        """
    ).fetchone()
    profile_row = None
    if row and row["active_project_profile_id"]:
        profile_row = conn.execute(
            "SELECT * FROM review_project_profiles WHERE id = ?",
            (row["active_project_profile_id"],),
        ).fetchone()
    if not profile_row:
        profile_row = conn.execute(
            """
            SELECT * FROM review_project_profiles
            WHERE enabled = 1
            ORDER BY is_default DESC, id ASC
            LIMIT 1
            """
        ).fetchone()
    conn.close()
    if not row:
        return ReviewProjectSettings()

    max_files = row["review_graph_context_max_files"] or DEFAULT_MAX_RELATED_FILES
    try:
        max_files = max(1, min(50, int(max_files)))
    except (TypeError, ValueError):
        max_files = DEFAULT_MAX_RELATED_FILES

    profile_json = _parse_profile_json(profile_row["profile_json"]) if profile_row else default_adinsure_profile_json()
    return ReviewProjectSettings(
        project_root=((profile_row["project_root"] if profile_row else "") or row["review_project_root"] or "").strip(),
        config_path=((profile_row["config_path"] if profile_row else "") or row["review_project_config_path"] or DEFAULT_CONFIG_PATH).strip(),
        sql_target=((profile_row["sql_target"] if profile_row else "") or row["review_sql_target"] or DEFAULT_SQL_TARGET).strip(),
        graph_context_enabled=bool(profile_row["graph_context_enabled"] if profile_row else row["review_graph_context_enabled"]),
        graph_context_max_files=int(profile_row["graph_context_max_files"] if profile_row else max_files),
        profile_id=profile_row["id"] if profile_row else None,
        profile_name=profile_row["name"] if profile_row else "",
        profile_json=profile_json,
    )


def validate_profile_json(profile_json: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(profile_json, dict):
        return ["profile_json must be a JSON object"]
    if not isinstance(profile_json.get("index"), dict):
        errors.append("index object is required")
    if not isinstance(profile_json.get("preferred_files", []), list):
        errors.append("preferred_files must be an array")
    if not isinstance(profile_json.get("rules", []), list):
        errors.append("rules must be an array")
    allowed_rule_types = {
        "json_field_link",
        "json_field_link_any",
        "reverse_json_field_link",
        "sink_flow",
        "component_links",
        "component_owners",
        "named_directory_link",
        "template_files",
    }
    for index, rule in enumerate(profile_json.get("rules", [])):
        if not isinstance(rule, dict):
            errors.append(f"rules[{index}] must be an object")
            continue
        rule_type = rule.get("type")
        if rule_type not in allowed_rule_types:
            errors.append(f"rules[{index}].type is unknown: {rule_type}")
        if "source_kinds" in rule and not isinstance(rule.get("source_kinds"), list):
            errors.append(f"rules[{index}].source_kinds must be an array")
    return errors


def preview_project_graph_context(profile_id: int, changed_paths: Iterable[str]) -> ProjectGraphContext:
    settings = get_review_project_settings_by_profile(profile_id)
    return build_project_graph_context(changed_paths, settings)


def get_review_project_settings_by_profile(profile_id: int) -> ReviewProjectSettings:
    conn = get_db()
    row = conn.execute("SELECT * FROM review_project_profiles WHERE id = ?", (profile_id,)).fetchone()
    conn.close()
    if not row:
        raise ValueError("Project profile not found")
    profile_json = _parse_profile_json(row["profile_json"])
    return ReviewProjectSettings(
        project_root=(row["project_root"] or "").strip(),
        config_path=(row["config_path"] or DEFAULT_CONFIG_PATH).strip(),
        sql_target=(row["sql_target"] or DEFAULT_SQL_TARGET).strip(),
        graph_context_enabled=bool(row["graph_context_enabled"]),
        graph_context_max_files=max(1, min(50, int(row["graph_context_max_files"] or DEFAULT_MAX_RELATED_FILES))),
        profile_id=row["id"],
        profile_name=row["name"] or "",
        profile_json=profile_json,
    )


def _parse_profile_json(raw: str | dict | None) -> dict:
    if isinstance(raw, dict):
        profile_json = raw
    else:
        try:
            profile_json = json.loads(raw or "{}")
        except json.JSONDecodeError:
            logger.warning("Invalid review project profile JSON, using AdInsure defaults")
            profile_json = {}
    if validate_profile_json(profile_json):
        return default_adinsure_profile_json()
    return profile_json


def build_project_graph_context(
    changed_paths: Iterable[str],
    settings: ReviewProjectSettings | None = None,
) -> ProjectGraphContext:
    settings = settings or get_review_project_settings()
    if not settings.graph_context_enabled or not settings.project_root.strip():
        return ProjectGraphContext(settings, [], [], [])

    root = Path(settings.project_root)
    if not root.exists() or not root.is_dir():
        return ProjectGraphContext(
            settings,
            [],
            [f"Project root does not exist: {settings.project_root}"],
            [],
        )

    config_root = root / settings.config_path
    if not config_root.exists() or not config_root.is_dir():
        return ProjectGraphContext(
            settings,
            [],
            [f"Config path does not exist: {settings.config_path}"],
            [],
        )

    profile = settings.profile_json or default_adinsure_profile_json()
    index = _build_config_index(root, config_root, profile)
    related: dict[str, RelatedFile] = {}
    unresolved: list[str] = []
    notes: list[str] = []

    for raw_path in changed_paths:
        rel_path = _normalize_rel_path(raw_path)
        node = index.find_owner(rel_path)
        if node:
            notes.append(f"{rel_path} belongs to {node.kind}/{node.code_name}")
            _add_node_context(index, node, related, unresolved, f"owner of changed file {rel_path}", profile)
            _add_constructor_links(index, node, related, unresolved, profile)
        _add_js_import_context(root, rel_path, related, unresolved, profile)

    return ProjectGraphContext(
        settings=settings,
        related_files=list(related.values())[:settings.graph_context_max_files],
        unresolved=_dedupe(unresolved),
        notes=_dedupe(notes),
    )


def _build_config_index(root: Path, config_root: Path, profile: dict | None = None) -> ConfigIndex:
    profile = profile or default_adinsure_profile_json()
    index_config = profile.get("index", {})
    config_glob = index_config.get("config_glob", "**/configuration.json")
    kind_segment = int(index_config.get("kind_segment", 1))
    default_code_segment = int(index_config.get("code_segment", 2))
    code_segment_by_kind = index_config.get("code_segment_by_kind", {})
    index = ConfigIndex(root, config_root)
    for config_path in config_root.glob(config_glob):
        rel_to_config = config_path.relative_to(config_root).as_posix()
        parts = rel_to_config.split("/")
        if len(parts) <= max(kind_segment, default_code_segment):
            continue
        package = parts[0]
        kind = parts[kind_segment]
        code_index = int(code_segment_by_kind.get(kind, default_code_segment))
        if len(parts) <= code_index:
            continue
        code_name = parts[code_index]
        try:
            config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to load config %s: %s", config_path, exc)
            config = {}
        rel_path = config_path.relative_to(root).as_posix()
        rel_dir = config_path.parent.relative_to(root).as_posix()
        index.add(
            ConfigNode(
                package=package,
                kind=kind,
                code_name=code_name,
                rel_path=rel_path,
                rel_dir=rel_dir,
                abs_dir=config_path.parent,
                config=config,
            )
        )
    return index


def _add_constructor_links(
    index: ConfigIndex,
    node: ConfigNode,
    related: dict[str, RelatedFile],
    unresolved: list[str],
    profile: dict | None = None,
) -> None:
    profile = profile or default_adinsure_profile_json()
    config = node.config if isinstance(node.config, dict) else {}
    for rule in profile.get("rules", []):
        if not _rule_applies(rule, node):
            continue
        rule_type = rule.get("type")
        if rule_type == "json_field_link":
            for target_name in _extract_names(_get_path_value(config, rule.get("field", ""))):
                _add_linked_node(
                    index,
                    related,
                    unresolved,
                    str(rule.get("target_kind", "")),
                    target_name,
                    _format_relation(rule.get("relation", "{kind}/{code_name} link"), node, target_name),
                    node.rel_path,
                    profile,
                    required=bool(rule.get("required")),
                )
        elif rule_type == "json_field_link_any":
            for target_name in _extract_names(_get_path_value(config, rule.get("field", ""))):
                target_node = index.find_any(target_name, [str(kind) for kind in rule.get("target_kinds", [])])
                if target_node:
                    _add_node_context(index, target_node, related, unresolved, _format_relation(rule.get("relation", "{kind}/{code_name} link"), node, target_name), profile)
                elif rule.get("required"):
                    unresolved.append(f"{node.rel_path}: {rule.get('field')} {target_name} not found")
        elif rule_type == "reverse_json_field_link":
            for target in index.nodes:
                if target.kind not in rule.get("target_kinds", []):
                    continue
                if node.code_name in _extract_names(_get_path_value(target.config, rule.get("field", ""))):
                    _add_node_context(index, target, related, unresolved, _format_relation(rule.get("relation", "reverse link for {code_name}"), node), profile)
        elif rule_type == "sink_flow":
            _add_sink_flow_context(index, node, related, unresolved, rule, profile)
        elif rule_type == "component_links":
            for component_name in _extract_names(_get_path_value(config, rule.get("field", "components"))):
                _add_linked_node(index, related, unresolved, "component", component_name, f"{node.kind}/{node.code_name} component", node.rel_path, profile)
        elif rule_type == "component_owners":
            for owner in index.nodes:
                if owner.kind not in rule.get("owner_kinds", []):
                    continue
                if node.code_name in _extract_names(_get_path_value(owner.config, rule.get("field", "components"))):
                    _add_node_context(index, owner, related, unresolved, f"owner using component {node.code_name}", profile)
        elif rule_type == "named_directory_link":
            for name in _extract_names(_get_path_value(config, rule.get("field", ""))):
                _add_existing_dir(index.root, node.abs_dir / str(rule.get("directory", "")) / name, related, _format_relation(rule.get("relation", "{name} directory for {code_name}"), node, name), profile)
        elif rule_type == "template_files":
            templates = _get_path_value(config, rule.get("field", ""))
            if isinstance(templates, dict):
                for template_path in templates.values():
                    if isinstance(template_path, str):
                        _add_existing_file(index.root, node.abs_dir / template_path, related, _format_relation(rule.get("relation", "template for {code_name}"), node, template_path), profile)

def _add_linked_node(
    index: ConfigIndex,
    related: dict[str, RelatedFile],
    unresolved: list[str],
    kind: str,
    code_name: str | None,
    relation: str,
    source_path: str,
    profile: dict | None = None,
    required: bool = True,
) -> None:
    if not code_name:
        return
    linked = index.find(kind, str(code_name))
    if linked:
        _add_node_context(index, linked, related, unresolved, relation, profile)
    elif required:
        unresolved.append(f"{source_path}: {kind} {code_name} not found")


def _add_node_context(
    index: ConfigIndex,
    node: ConfigNode,
    related: dict[str, RelatedFile],
    unresolved: list[str],
    relation: str,
    profile: dict | None = None,
) -> None:
    profile = profile or default_adinsure_profile_json()
    preferred_names = profile.get("preferred_files", [])
    for name in preferred_names:
        _add_existing_file(index.root, node.abs_dir / name, related, relation, profile)


def _add_existing_dir(
    root: Path,
    directory: Path,
    related: dict[str, RelatedFile],
    relation: str,
    profile: dict | None = None,
) -> None:
    if not directory.exists() or not directory.is_dir():
        return
    for path in sorted(directory.rglob("*")):
        if path.is_file() and _is_text_context_file(path, profile):
            _add_existing_file(root, path, related, relation, profile)


def _add_existing_file(
    root: Path,
    path: Path,
    related: dict[str, RelatedFile],
    relation: str,
    profile: dict | None = None,
) -> None:
    if not path.exists() or not path.is_file() or not _is_text_context_file(path, profile):
        return
    rel_path = path.relative_to(root).as_posix()
    if rel_path in related:
        return
    try:
        content = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return
    except OSError:
        return
    if len(content) > RELATED_FILE_MAX_CHARS:
        content = content[:RELATED_FILE_MAX_CHARS].rstrip() + "\n... [related context truncated]"
    related[rel_path] = RelatedFile(rel_path, relation, content)


def _add_js_import_context(
    root: Path,
    rel_path: str,
    related: dict[str, RelatedFile],
    unresolved: list[str],
    profile: dict | None = None,
) -> None:
    profile = profile or default_adinsure_profile_json()
    js_import = profile.get("js_import", {})
    if not js_import.get("enabled", True):
        return
    if not rel_path.endswith(".js"):
        return
    source_path = root / rel_path
    if not source_path.exists() or not source_path.is_file():
        return
    try:
        content = source_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return
    regex = js_import.get("regex", r"@config-(?:rgsl|system)/[A-Za-z0-9_-]+/[A-Za-z0-9_./-]+")
    path_prefix = js_import.get("path_prefix", "configuration")
    extension = js_import.get("extension", ".js")
    for import_path in re.findall(regex, content):
        candidate = root / path_prefix / (import_path + extension)
        if candidate.exists():
            _add_existing_file(root, candidate, related, f"JS import used by {rel_path}", profile)
        else:
            unresolved.append(f"{rel_path}: import {import_path} not found in local project")


def _iter_sinks(config: dict) -> Iterable[dict]:
    for section in ("sinks", "completionSinks", "errorSinks"):
        for item in config.get(section) or []:
            if isinstance(item, dict):
                yield item


def _add_sink_flow_context(
    index: ConfigIndex,
    node: ConfigNode,
    related: dict[str, RelatedFile],
    unresolved: list[str],
    rule: dict,
    profile: dict,
) -> None:
    config = node.config if isinstance(node.config, dict) else {}
    main_data_source = _get_path_value(config, rule.get("main_data_source_field", "mainDataSource"))
    _add_linked_node(index, related, unresolved, "dataSource", main_data_source, f"{node.kind}/{node.code_name} mainDataSource", node.rel_path, profile, required=False)
    if main_data_source:
        _add_existing_dir(index.root, node.abs_dir / str(rule.get("source_mapping_dir", "sourceMappings")) / str(main_data_source), related, f"sourceMapping for {node.code_name}", profile)

    for section in rule.get("sink_sections", ["sinks", "completionSinks", "errorSinks"]):
        for sink in config.get(section) or []:
            if not isinstance(sink, dict):
                continue
            sink_name = sink.get("name")
            if sink_name:
                mapping_dir = node.abs_dir / str(rule.get("sink_mapping_dir", "sinkMappings")) / str(sink_name)
                if mapping_dir.exists():
                    _add_existing_dir(index.root, mapping_dir, related, f"sinkMapping {sink_name} for {node.code_name}", profile)
                else:
                    unresolved.append(f"{node.rel_path}: sinkMapping {sink_name} not found")
            _add_linked_node(index, related, unresolved, "sinkGroup", sink.get("ref"), f"ref sinkGroup for {node.code_name}", node.rel_path, profile, required=False)
            fetch_config = _get_path_value(sink, "fetch.configuration")
            if isinstance(fetch_config, dict):
                _add_linked_node(index, related, unresolved, "dataSource", fetch_config.get("name"), f"fetch dataSource for {node.code_name}", node.rel_path, profile, required=False)
            for key, kinds in (
                ("document", ("document",)),
                ("masterEntity", ("masterEntity",)),
                ("notification", ("notification",)),
            ):
                target = sink.get(key) if isinstance(sink.get(key), dict) else {}
                target_config = target.get("configuration") if isinstance(target.get("configuration"), dict) else {}
                target_name = target_config.get("name") or target.get("name")
                target_node = index.find_any(target_name, kinds)
                if target_node:
                    _add_node_context(index, target_node, related, unresolved, f"{key} sink target for {node.code_name}", profile)
            transition_name = _get_path_value(sink, "documentTransition.transition.configurationName")
            transition_node = index.find_any(transition_name, ("document", "masterEntity"))
            if transition_node:
                _add_node_context(index, transition_node, related, unresolved, f"documentTransition target for {node.code_name}", profile)


def _rule_applies(rule: dict, node: ConfigNode) -> bool:
    source_kinds = rule.get("source_kinds") or []
    return not source_kinds or node.kind in source_kinds


def _get_path_value(data, path: str):
    current = data
    for part in (path or "").split("."):
        if not part:
            continue
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _format_relation(template: str, node: ConfigNode, name: str | None = None) -> str:
    return str(template or "{kind}/{code_name} link").format(
        package=node.package,
        kind=node.kind,
        code_name=node.code_name,
        name=name or "",
    )


def _extract_names(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        names: list[str] = []
        for item in value:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                for key in ("name", "component", "codeName", "configurationCodeName", "dataSource"):
                    if item.get(key):
                        names.append(str(item[key]))
                        break
        return names
    if isinstance(value, dict):
        for key in ("name", "component", "codeName", "configurationCodeName", "dataSource"):
            if value.get(key):
                return [str(value[key])]
    return []


def _normalize_rel_path(path: str) -> str:
    path = (path or "").strip().replace("\\", "/")
    path = re.sub(r"^([ab]/)", "", path)
    return path.strip("/")


def _is_text_context_file(path: Path, profile: dict | None = None) -> bool:
    profile = profile or default_adinsure_profile_json()
    return path.suffix.lower() in set(profile.get("text_extensions", [".json", ".js", ".handlebars", ".csv", ".html", ".txt", ".md"]))


def _dedupe(items: Iterable[str]) -> list[str]:
    result: list[str] = []
    for item in items:
        if item and item not in result:
            result.append(item)
    return result
