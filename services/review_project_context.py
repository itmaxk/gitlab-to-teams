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


@dataclass(frozen=True)
class ReviewProjectSettings:
    project_root: str = ""
    config_path: str = DEFAULT_CONFIG_PATH
    sql_target: str = DEFAULT_SQL_TARGET
    graph_context_enabled: bool = True
    graph_context_max_files: int = DEFAULT_MAX_RELATED_FILES


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
            "## AdInsure constructor graph context",
            f"- SQL target: {self.settings.sql_target}",
            "- Treat this project as a configuration constructor: changed files can break linked configuration nodes.",
            "- Validate links between dataSource, dataProvider, etlService, route, integrationService, sinkGroup, document, component, view, printoutRelation, notification, and JS package imports.",
        ]
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
               review_graph_context_enabled, review_graph_context_max_files
        FROM review_settings
        WHERE id = 1
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

    return ReviewProjectSettings(
        project_root=(row["review_project_root"] or "").strip(),
        config_path=(row["review_project_config_path"] or DEFAULT_CONFIG_PATH).strip(),
        sql_target=(row["review_sql_target"] or DEFAULT_SQL_TARGET).strip(),
        graph_context_enabled=bool(row["review_graph_context_enabled"]),
        graph_context_max_files=max_files,
    )


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

    index = _build_config_index(root, config_root)
    related: dict[str, RelatedFile] = {}
    unresolved: list[str] = []
    notes: list[str] = []

    for raw_path in changed_paths:
        rel_path = _normalize_rel_path(raw_path)
        node = index.find_owner(rel_path)
        if node:
            notes.append(f"{rel_path} belongs to {node.kind}/{node.code_name}")
            _add_node_context(index, node, related, unresolved, f"owner of changed file {rel_path}")
            _add_constructor_links(index, node, related, unresolved)
        _add_js_import_context(root, rel_path, related, unresolved)

    return ProjectGraphContext(
        settings=settings,
        related_files=list(related.values())[:settings.graph_context_max_files],
        unresolved=_dedupe(unresolved),
        notes=_dedupe(notes),
    )


def _build_config_index(root: Path, config_root: Path) -> ConfigIndex:
    index = ConfigIndex(root, config_root)
    for config_path in config_root.rglob("configuration.json"):
        rel_to_config = config_path.relative_to(config_root).as_posix()
        parts = rel_to_config.split("/")
        if len(parts) < 3:
            continue
        package = parts[0]
        kind = parts[1]
        code_index = 3 if kind in {"dataProvider", "route"} and len(parts) >= 4 else 2
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
) -> None:
    config = node.config if isinstance(node.config, dict) else {}

    if node.kind == "dataSource":
        provider = config.get("dataProvider") or {}
        provider_name = provider.get("codeName")
        provider_node = index.find("dataProvider", provider_name)
        if provider_node:
            _add_node_context(index, provider_node, related, unresolved, f"dataProvider for {node.code_name}")
        elif provider_name:
            unresolved.append(f"{node.rel_path}: dataProvider {provider_name} not found")

    if node.kind == "dataProvider":
        for data_source in index.nodes:
            if data_source.kind != "dataSource":
                continue
            provider = data_source.config.get("dataProvider") if isinstance(data_source.config, dict) else None
            if isinstance(provider, dict) and provider.get("codeName") == node.code_name:
                _add_node_context(index, data_source, related, unresolved, f"dataSource using {node.code_name}")

    if node.kind in {"view", "dataExport"}:
        _add_linked_node(
            index,
            related,
            unresolved,
            "dataSource",
            config.get("dataSource"),
            f"{node.kind}/{node.code_name} dataSource",
            node.rel_path,
        )
        for data_source_name in _extract_names(config.get("additionalDataSources")):
            _add_linked_node(
                index,
                related,
                unresolved,
                "dataSource",
                data_source_name,
                f"{node.kind}/{node.code_name} additionalDataSource",
                node.rel_path,
            )

    if node.kind in {"etlService", "route", "integrationService", "sinkGroup"}:
        _add_linked_node(
            index,
            related,
            unresolved,
            "dataSource",
            config.get("mainDataSource"),
            f"{node.kind}/{node.code_name} mainDataSource",
            node.rel_path,
        )
        if config.get("mainDataSource"):
            _add_existing_dir(
                index.root,
                node.abs_dir / "sourceMappings" / str(config.get("mainDataSource")),
                related,
                f"sourceMapping for {node.code_name}",
            )
        for sink in _iter_sinks(config):
            sink_name = sink.get("name")
            if sink_name:
                mapping_dir = node.abs_dir / "sinkMappings" / str(sink_name)
                if mapping_dir.exists():
                    _add_existing_dir(index.root, mapping_dir, related, f"sinkMapping {sink_name} for {node.code_name}")
                else:
                    unresolved.append(f"{node.rel_path}: sinkMapping {sink_name} not found")
            _add_linked_node(
                index,
                related,
                unresolved,
                "sinkGroup",
                sink.get("ref"),
                f"ref sinkGroup for {node.code_name}",
                node.rel_path,
            )
            fetch = sink.get("fetch") if isinstance(sink.get("fetch"), dict) else {}
            fetch_config = fetch.get("configuration") if isinstance(fetch.get("configuration"), dict) else {}
            _add_linked_node(
                index,
                related,
                unresolved,
                "dataSource",
                fetch_config.get("name"),
                f"fetch dataSource for {node.code_name}",
                node.rel_path,
            )
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
                    _add_node_context(index, target_node, related, unresolved, f"{key} sink target for {node.code_name}")
            transition = sink.get("documentTransition") if isinstance(sink.get("documentTransition"), dict) else {}
            transition_config = transition.get("transition") if isinstance(transition.get("transition"), dict) else {}
            transition_document = transition_config.get("configurationName")
            transition_node = index.find_any(transition_document, ("document", "masterEntity"))
            if transition_node:
                _add_node_context(index, transition_node, related, unresolved, f"documentTransition target for {node.code_name}")

    if node.kind in {"document", "masterEntity", "view", "component"}:
        for component_name in _extract_names(config.get("components")):
            _add_linked_node(
                index,
                related,
                unresolved,
                "component",
                component_name,
                f"{node.kind}/{node.code_name} component",
                node.rel_path,
            )

    if node.kind == "component":
        for owner in index.nodes:
            if owner.kind not in {"document", "masterEntity", "view"}:
                continue
            if node.code_name in _extract_names(owner.config.get("components")):
                _add_node_context(index, owner, related, unresolved, f"owner using component {node.code_name}")

    if node.kind == "printoutRelation":
        _add_linked_node(
            index,
            related,
            unresolved,
            "printout",
            config.get("targetPrintout"),
            f"targetPrintout for {node.code_name}",
            node.rel_path,
        )
        source_node = index.find_any(config.get("sourceConfigurationName"), ("document", "masterEntity"))
        if source_node:
            _add_node_context(index, source_node, related, unresolved, f"source configuration for {node.code_name}")
        elif config.get("sourceConfigurationName"):
            unresolved.append(f"{node.rel_path}: sourceConfigurationName {config.get('sourceConfigurationName')} not found")
        for data_source_name in _extract_names(config.get("additionalDataSources")):
            _add_linked_node(
                index,
                related,
                unresolved,
                "dataSource",
                data_source_name,
                f"printoutRelation additionalDataSource for {node.code_name}",
                node.rel_path,
            )
            _add_existing_dir(
                index.root,
                node.abs_dir / "sourceMappings" / data_source_name,
                related,
                f"printout sourceMapping {data_source_name} for {node.code_name}",
            )

    if node.kind == "notification":
        channel = config.get("channel") if isinstance(config.get("channel"), dict) else {}
        templates = channel.get("templates") if isinstance(channel.get("templates"), dict) else {}
        for template_path in templates.values():
            if isinstance(template_path, str):
                _add_existing_file(
                    index.root,
                    node.abs_dir / template_path,
                    related,
                    f"notification template for {node.code_name}",
                )


def _add_linked_node(
    index: ConfigIndex,
    related: dict[str, RelatedFile],
    unresolved: list[str],
    kind: str,
    code_name: str | None,
    relation: str,
    source_path: str,
) -> None:
    if not code_name:
        return
    linked = index.find(kind, str(code_name))
    if linked:
        _add_node_context(index, linked, related, unresolved, relation)
    else:
        unresolved.append(f"{source_path}: {kind} {code_name} not found")


def _add_node_context(
    index: ConfigIndex,
    node: ConfigNode,
    related: dict[str, RelatedFile],
    unresolved: list[str],
    relation: str,
) -> None:
    preferred_names = [
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
    ]
    for name in preferred_names:
        _add_existing_file(index.root, node.abs_dir / name, related, relation)


def _add_existing_dir(
    root: Path,
    directory: Path,
    related: dict[str, RelatedFile],
    relation: str,
) -> None:
    if not directory.exists() or not directory.is_dir():
        return
    for path in sorted(directory.rglob("*")):
        if path.is_file() and _is_text_context_file(path):
            _add_existing_file(root, path, related, relation)


def _add_existing_file(
    root: Path,
    path: Path,
    related: dict[str, RelatedFile],
    relation: str,
) -> None:
    if not path.exists() or not path.is_file() or not _is_text_context_file(path):
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
) -> None:
    if not rel_path.endswith(".js"):
        return
    source_path = root / rel_path
    if not source_path.exists() or not source_path.is_file():
        return
    try:
        content = source_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return
    for import_path in re.findall(r"@config-(?:rgsl|system)/[A-Za-z0-9_-]+/[A-Za-z0-9_./-]+", content):
        candidate = root / "configuration" / (import_path + ".js")
        if candidate.exists():
            _add_existing_file(root, candidate, related, f"JS import used by {rel_path}")
        else:
            unresolved.append(f"{rel_path}: import {import_path} not found in local project")


def _iter_sinks(config: dict) -> Iterable[dict]:
    for section in ("sinks", "completionSinks", "errorSinks"):
        for item in config.get(section) or []:
            if isinstance(item, dict):
                yield item


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


def _is_text_context_file(path: Path) -> bool:
    return path.suffix.lower() in {
        ".json",
        ".js",
        ".handlebars",
        ".csv",
        ".html",
        ".txt",
        ".md",
    }


def _dedupe(items: Iterable[str]) -> list[str]:
    result: list[str] = []
    for item in items:
        if item and item not in result:
            result.append(item)
    return result
