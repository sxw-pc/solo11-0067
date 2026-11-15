"""Command-line entry points for mock, harvest, report, status, and demo."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

from .config import HarvestConfig, load_harvest_config, load_mock_config
from .db import Database
from .demo import run_demo
from .harvester import Harvester
from .mock_repository import create_app
from .oai_errors import MetaHarvestError, SetCatalogError
from .report import write_reports
from .set_catalog import SetCatalog, resolve_catalog_client
from .util import parse_datestamp

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _default_config(filename: str) -> Path:
    candidates = (PROJECT_ROOT / "configs" / filename, Path.cwd() / "configs" / filename)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[1]


DEFAULT_HARVEST_CONFIG = _default_config("harvest_demo.yml")
DEFAULT_MOCK_CONFIG = _default_config("mock_repo.yml")
DEFAULT_WORKDIR = Path.cwd() / "build" / "demo"


def _emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="metaharvest",
        description="元仓收割台：纯本地 OAI-PMH 元数据收割与 mock 仓储",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    mock = sub.add_parser("mock", help="启动本机 mock OAI-PMH 仓储")
    mock.add_argument("--config", type=Path, default=DEFAULT_MOCK_CONFIG)
    mock.set_defaults(handler=_cmd_mock)

    harvest = sub.add_parser("harvest", help="执行一次 OAI-PMH 收割")
    harvest.add_argument("--config", type=Path, default=DEFAULT_HARVEST_CONFIG)
    harvest.add_argument("--resume", type=int, metavar="RUN_ID", help="从台账中的断点续采")
    harvest.add_argument("--from", dest="from_date", help="OAI from 日期/时间")
    harvest.add_argument("--until", dest="until_date", help="OAI until 日期/时间")
    harvest.add_argument("--set", dest="set_spec", help="OAI setSpec")
    harvest.add_argument("--metadata-prefix", dest="metadata_prefix")
    harvest.add_argument("--max-pages", type=int, help="本次运行最多请求页数")
    harvest.add_argument(
        "--allow-unverified-set",
        dest="allow_unverified_set",
        action="store_true",
        help="兼容开关：跳过目录准入，直接按 --set 采集并在 run 中标记未验证",
    )
    harvest.add_argument("--set-name", help="冲突 set 的说明版本：setName")
    harvest.add_argument("--set-description", help="冲突 set 的说明版本：setDescription")
    harvest.add_argument("--set-version", help="冲突 set 的说明版本哈希")
    harvest.add_argument("--db", type=Path, help="覆盖 SQLite 台账路径")
    harvest.add_argument("--archive-dir", type=Path, help="覆盖原始 XML 归档目录")
    harvest.add_argument("--audit-log", type=Path, help="覆盖请求审计日志路径")
    harvest.set_defaults(handler=_cmd_harvest)

    catalog = sub.add_parser("catalog", help="维护仓储集合目录")
    catalog_sub = catalog.add_subparsers(dest="catalog_command", required=True)
    catalog_refresh = catalog_sub.add_parser(
        "refresh", help="分页抓取完整 ListSets 并发布新的目录代"
    )
    catalog_refresh.add_argument("--config", type=Path, default=DEFAULT_HARVEST_CONFIG)
    catalog_refresh.add_argument("--db", type=Path, help="覆盖 SQLite 台账路径")
    catalog_refresh.add_argument("--audit-log", type=Path, help="覆盖请求审计日志路径")
    catalog_refresh.add_argument(
        "--conflict-policy",
        choices=["strict", "active"],
        help="覆盖目录冲突策略：strict 整代不激活，active 隔离冲突 set",
    )
    catalog_refresh.set_defaults(handler=_cmd_catalog_refresh)

    catalog_show = catalog_sub.add_parser(
        "show", help="查看当前 active 目录代及其成员状态"
    )
    catalog_show.add_argument("--config", type=Path, default=DEFAULT_HARVEST_CONFIG)
    catalog_show.add_argument("--db", type=Path, help="覆盖 SQLite 台账路径")
    catalog_show.add_argument("--generation", type=int, help="查看指定代而非 active 代")
    catalog_show.add_argument(
        "--all-generations", action="store_true", help="附带该仓储的所有目录代"
    )
    catalog_show.set_defaults(handler=_cmd_catalog_show)

    report = sub.add_parser("report", help="从台账和归档生成可复核报告")
    report.add_argument("--config", type=Path, default=DEFAULT_HARVEST_CONFIG)
    report.add_argument("--db", type=Path)
    report.add_argument("--run-id", type=int)
    report.add_argument("--output-dir", type=Path)
    report.add_argument(
        "--format",
        dest="formats",
        nargs="+",
        default=["all"],
        choices=["all", "json", "csv", "markdown", "md"],
    )
    report.set_defaults(handler=_cmd_report)

    status = sub.add_parser("status", help="查看收割运行台账")
    status.add_argument("--config", type=Path, default=DEFAULT_HARVEST_CONFIG)
    status.add_argument("--db", type=Path)
    status.add_argument("--limit", type=int, default=20)
    status.set_defaults(handler=_cmd_status)

    demo = sub.add_parser("demo", help="一键运行 mock 启动、两轮增量收割与报告")
    demo.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR)
    demo.add_argument("--mock-config", type=Path, default=DEFAULT_MOCK_CONFIG)
    demo.add_argument("--harvest-config", type=Path, default=DEFAULT_HARVEST_CONFIG)
    demo.set_defaults(handler=_cmd_demo)
    return parser


def _cmd_mock(args: argparse.Namespace) -> int:
    config = load_mock_config(args.config)
    app = create_app(config)
    print(f"{config.repository.name} 监听 {config.repository.base_url}（仅本机）")
    app.run(
        host=config.host,
        port=config.port,
        debug=False,
        use_reloader=False,
        threaded=True,
    )
    return 0


def _storage_overrides(
    config: HarvestConfig, args: argparse.Namespace
) -> HarvestConfig:
    storage = config.storage
    if args.db:
        storage = replace(storage, db_path=args.db.expanduser().resolve())
    if getattr(args, "archive_dir", None):
        storage = replace(
            storage, archive_dir=args.archive_dir.expanduser().resolve()
        )
    if getattr(args, "audit_log", None):
        storage = replace(storage, audit_log=args.audit_log.expanduser().resolve())
    return replace(config, storage=storage)


def _cmd_harvest(args: argparse.Namespace) -> int:
    config = _storage_overrides(load_harvest_config(args.config), args)
    if args.resume is not None:
        if any(
            value is not None
            for value in (
                args.from_date,
                args.until_date,
                args.set_spec,
                args.metadata_prefix,
                args.set_name,
                args.set_description,
                args.set_version,
            )
        ) or args.allow_unverified_set:
            raise MetaHarvestError("续采时不得改变仓储筛选参数")
        result = Harvester(config).resume(args.resume, max_pages=args.max_pages)
    else:
        repository = replace(
            config.repository,
            from_date=args.from_date or config.repository.from_date,
            until_date=args.until_date or config.repository.until_date,
            set_spec=args.set_spec if args.set_spec is not None else config.repository.set_spec,
            metadata_prefix=args.metadata_prefix or config.repository.metadata_prefix,
        )
        if repository.from_date:
            parse_datestamp(repository.from_date)
        if repository.until_date:
            parse_datestamp(repository.until_date)
        if (
            repository.from_date
            and repository.until_date
            and parse_datestamp(repository.from_date)
            > parse_datestamp(repository.until_date)
        ):
            raise MetaHarvestError("--from 不得晚于 --until")
        config = replace(config, repository=repository)
        harvester = Harvester(config)
        admission = harvester.admit_set(
            allow_unverified=args.allow_unverified_set
            or not config.verify_sets,
            pinned_set_name=args.set_name,
            pinned_set_description=args.set_description,
            pinned_set_version=args.set_version,
        )
        result = harvester.start(max_pages=args.max_pages, admission=admission)
    payload = result.as_dict()
    payload["db_path"] = str(config.storage.db_path)
    payload["archive_dir"] = str(config.storage.archive_dir)
    if result.status == "interrupted":
        payload["resume_hint"] = (
            f"python -m metaharvest harvest --config {args.config} "
            f"--resume {result.run_id} --db {config.storage.db_path}"
        )
    _emit(payload)
    return 0 if result.status in {"completed", "interrupted"} else 1


def _cmd_catalog_refresh(args: argparse.Namespace) -> int:
    config = _storage_overrides(load_harvest_config(args.config), args)
    policy = args.conflict_policy or config.set_catalog_conflict_policy
    db = Database(config.storage.db_path)
    catalog = SetCatalog(
        db, resolve_catalog_client(config), conflict_policy=policy
    )
    try:
        result = catalog.refresh()
    except SetCatalogError as exc:
        raise MetaHarvestError(str(exc)) from exc
    payload = result.as_dict()
    payload["db_path"] = str(config.storage.db_path)
    if result.admitted:
        active = catalog.active_generation(config.repository.base_url)
        payload["active_generation"] = active.as_dict() if active else None
    _emit(payload)
    return 0 if result.admitted else 1


def _cmd_catalog_show(args: argparse.Namespace) -> int:
    config = _storage_overrides(load_harvest_config(args.config), args)
    db = Database(config.storage.db_path)
    catalog = SetCatalog(
        db,
        resolve_catalog_client(config),
        conflict_policy=config.set_catalog_conflict_policy,
    )
    repository_url = config.repository.base_url
    if args.generation is not None:
        generation = next(
            (
                item
                for item in catalog.generations(repository_url, limit=1000)
                if item.generation == args.generation
            ),
            None,
        )
        if generation is None:
            raise MetaHarvestError(f"目录代不存在: {args.generation}")
    else:
        generation = catalog.active_generation(repository_url)
    payload: dict[str, object] = {
        "repository_url": repository_url,
        "db_path": str(config.storage.db_path),
        "generation": generation.as_dict() if generation else None,
        "members": (
            [member.as_dict() for member in catalog.members(generation.id)]
            if generation
            else []
        ),
    }
    if generation is not None:
        payload["pages"] = catalog.pages(generation.id)
    if args.all_generations:
        payload["generations"] = [
            item.as_dict() for item in catalog.generations(repository_url, limit=100)
        ]
    _emit(payload)
    return 0 if generation is not None else 1


def _cmd_report(args: argparse.Namespace) -> int:
    config = _storage_overrides(load_harvest_config(args.config), args)
    db = Database(config.storage.db_path)
    run = db.get_run(args.run_id) if args.run_id is not None else db.latest_run()
    if args.run_id is not None and run is None:
        raise MetaHarvestError(f"收割运行不存在: {args.run_id}")
    if run is None:
        raise MetaHarvestError("台账中还没有收割运行")
    formats = (
        ("json", "csv", "markdown")
        if "all" in args.formats
        else tuple(args.formats)
    )
    output = write_reports(
        db,
        int(run["id"]),
        args.output_dir or config.storage.report_dir,
        formats=formats,
    )
    _emit(output.as_dict())
    return 0 if output.integrity["status"] == "ok" else 1


def _cmd_status(args: argparse.Namespace) -> int:
    config = _storage_overrides(load_harvest_config(args.config), args)
    runs = Database(config.storage.db_path).list_runs(args.limit)
    _emit({"db_path": str(config.storage.db_path), "run_count": len(runs), "runs": runs})
    return 0


def _cmd_demo(args: argparse.Namespace) -> int:
    summary = run_demo(
        workdir=args.workdir,
        mock_config_path=args.mock_config,
        harvest_config_path=args.harvest_config,
    )
    _emit(summary.as_dict())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except KeyboardInterrupt:
        print("已中断。", file=sys.stderr)
        return 130
    except (MetaHarvestError, OSError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


def main_cli() -> int:
    return main()


if __name__ == "__main__":
    sys.exit(main())
