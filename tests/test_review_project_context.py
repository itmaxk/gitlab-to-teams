import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.review_project_context import ReviewProjectSettings, build_project_graph_context


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, data: dict) -> None:
    _write(path, json.dumps(data, ensure_ascii=False, indent=2))


def test_data_source_context_includes_data_provider_and_postgres_query(tmp_path):
    project_root = tmp_path / "impl"
    config_root = project_root / "configuration" / "@config-rgsl"
    _write_json(
        config_root / "acc-base" / "dataSource" / "AllocationDataSource" / "configuration.json",
        {
            "dataProvider": {
                "type": "DatabaseDataProvider",
                "codeName": "AllocationDataProvider",
                "version": "1",
            }
        },
    )
    _write(config_root / "acc-base" / "dataSource" / "AllocationDataSource" / "inputMapping.js", "module.exports = input => input;")
    _write_json(config_root / "acc-base" / "dataSource" / "AllocationDataSource" / "inputSchema.json", {"type": "object"})
    _write_json(config_root / "acc-base" / "dataProvider" / "database" / "AllocationDataProvider" / "configuration.json", {"version": "1"})
    _write(
        config_root / "acc-base" / "dataProvider" / "database" / "AllocationDataProvider" / "query.postgres.handlebars",
        "select * from acc_impl.allocation where allocation_id = @allocationId",
    )

    context = build_project_graph_context(
        [
            "configuration/@config-rgsl/acc-base/dataSource/AllocationDataSource/inputMapping.js",
        ],
        ReviewProjectSettings(project_root=str(project_root)),
    )

    paths = {item.path for item in context.related_files}
    assert "configuration/@config-rgsl/acc-base/dataSource/AllocationDataSource/inputMapping.js" in paths
    assert "configuration/@config-rgsl/acc-base/dataProvider/database/AllocationDataProvider/configuration.json" in paths
    assert "configuration/@config-rgsl/acc-base/dataProvider/database/AllocationDataProvider/query.postgres.handlebars" in paths
    assert context.unresolved == []


def test_etl_context_includes_main_data_source_sink_mapping_and_sink_group(tmp_path):
    project_root = tmp_path / "impl"
    config_root = project_root / "configuration" / "@config-rgsl"
    _write_json(
        config_root / "acc-base" / "etlService" / "PendingPaymentsEtlService" / "configuration.json",
        {
            "mainDataSource": "PendingPaymentDataSource",
            "sinks": [
                {"name": "PostPayment", "ref": "PendingPaymentsSinkGroup"},
                {"name": "FetchPolicy", "fetch": {"configuration": {"name": "PolicyDataSource"}}},
            ],
        },
    )
    _write(
        config_root / "acc-base" / "etlService" / "PendingPaymentsEtlService" / "sinkMappings" / "PostPayment" / "mapping.js",
        "module.exports = function mapping() {};",
    )
    _write(
        config_root / "acc-base" / "etlService" / "PendingPaymentsEtlService" / "sourceMappings" / "PendingPaymentDataSource" / "mapping.js",
        "module.exports = function sourceMapping() {};",
    )
    _write_json(config_root / "acc-base" / "dataSource" / "PendingPaymentDataSource" / "configuration.json", {})
    _write_json(config_root / "acc-base" / "dataSource" / "PolicyDataSource" / "configuration.json", {})
    _write_json(config_root / "acc-base" / "sinkGroup" / "PendingPaymentsSinkGroup" / "configuration.json", {"sinks": []})

    context = build_project_graph_context(
        [
            "configuration/@config-rgsl/acc-base/etlService/PendingPaymentsEtlService/configuration.json",
        ],
        ReviewProjectSettings(project_root=str(project_root), graph_context_max_files=20),
    )

    paths = {item.path for item in context.related_files}
    assert "configuration/@config-rgsl/acc-base/dataSource/PendingPaymentDataSource/configuration.json" in paths
    assert "configuration/@config-rgsl/acc-base/dataSource/PolicyDataSource/configuration.json" in paths
    assert "configuration/@config-rgsl/acc-base/sinkGroup/PendingPaymentsSinkGroup/configuration.json" in paths
    assert "configuration/@config-rgsl/acc-base/etlService/PendingPaymentsEtlService/sinkMappings/PostPayment/mapping.js" in paths
    assert "configuration/@config-rgsl/acc-base/etlService/PendingPaymentsEtlService/sourceMappings/PendingPaymentDataSource/mapping.js" in paths


def test_printout_relation_context_reports_missing_source_mapping(tmp_path):
    project_root = tmp_path / "impl"
    config_root = project_root / "configuration" / "@config-rgsl"
    _write_json(
        config_root / "contract" / "printoutRelation" / "ContractToPrintout" / "configuration.json",
        {
            "sourceConfigurationName": "Contract",
            "targetPrintout": "ContractPrintout",
            "additionalDataSources": ["ContractDataSource"],
        },
    )
    _write_json(config_root / "contract" / "document" / "Contract" / "configuration.json", {"states": []})
    _write_json(config_root / "contract" / "printout" / "ContractPrintout" / "configuration.json", {"version": "1"})
    _write_json(config_root / "contract" / "dataSource" / "ContractDataSource" / "configuration.json", {})

    context = build_project_graph_context(
        [
            "configuration/@config-rgsl/contract/printoutRelation/ContractToPrintout/configuration.json",
        ],
        ReviewProjectSettings(project_root=str(project_root), graph_context_max_files=20),
    )

    paths = {item.path for item in context.related_files}
    assert "configuration/@config-rgsl/contract/document/Contract/configuration.json" in paths
    assert "configuration/@config-rgsl/contract/printout/ContractPrintout/configuration.json" in paths
    assert "configuration/@config-rgsl/contract/dataSource/ContractDataSource/configuration.json" in paths
