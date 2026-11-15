"""Build reviewable JSON, CSV, and Markdown harvest reports."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .db import Database
from .oai_errors import ReportError
from .util import atomic_write_text, ensure_directory, iso_z, sha256_bytes


@dataclass(frozen=True)
class ReportOutput:
    run_id: int
    files: dict[str, Path]
    integrity: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "files": {key: str(value) for key, value in self.files.items()},
            "integrity": self.integrity,
        }


def _verify_evidence(run: dict[str, Any], raw_responses: list[dict[str, Any]]) -> dict[str, Any]:
    root = Path(str(run["archive_root"])).expanduser().resolve()
    missing: list[str] = []
    mismatched: list[dict[str, str]] = []
    size_mismatched: list[dict[str, Any]] = []
    checked = 0
    for response in raw_responses:
        checked += 1
        relative = str(response["archive_path"])
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            missing.append(relative)
            continue
        if not candidate.is_file():
            missing.append(relative)
            continue
        content = candidate.read_bytes()
        actual = sha256_bytes(content)
        if actual != response["sha256"]:
            mismatched.append(
                {
                    "path": relative,
                    "expected": str(response["sha256"]),
                    "actual": actual,
                }
            )
        if len(content) != int(response["size_bytes"]):
            size_mismatched.append(
                {
                    "path": relative,
                    "expected": int(response["size_bytes"]),
                    "actual": len(content),
                }
            )
    ok = not missing and not mismatched and not size_mismatched
    return {
        "status": "ok" if ok else "failed",
        "checked_files": checked,
        "missing": missing,
        "sha256_mismatches": mismatched,
        "size_mismatches": size_mismatched,
    }


def build_report(db: Database, run_id: int) -> dict[str, Any]:
    """Assemble a report with two clearly separated read models.

    ``records`` is the *complete snapshot* reconstructed at this run's frozen
    watermark: the effective version of every identifier known when the run
    ended, including tombstones.  ``changes`` is the *single-run change list*:
    only identifiers whose effective state changed during this run.  Unchanged
    records appear in ``records`` but never in ``changes``.
    """
    run = db.get_run(run_id)
    if run is None:
        raise ReportError(f"收割运行不存在: {run_id}")
    raw_responses = db.raw_responses_for_run(run_id)
    snapshot = db.snapshot_for_run(run_id)
    assert snapshot is not None  # the run exists, checked above
    changes = db.changes_for_run(run_id)
    errors = db.errors_for_run(run_id)
    events = db.events_for_run(run_id)
    integrity = _verify_evidence(run, raw_responses)
    return {
        "schema_version": 2,
        "generated_at": iso_z(),
        "run": run,
        "view": {
            "snapshot_status": snapshot.status,
            "complete": snapshot.complete,
            "watermark": snapshot.watermark,
            "frozen_at": snapshot.frozen_at,
            "snapshot_record_count": snapshot.record_count,
            "snapshot_deleted_count": snapshot.deleted_count,
            "changed_record_count": len(changes),
        },
        "records": list(snapshot.records),
        "changes": changes,
        "raw_responses": raw_responses,
        "errors": errors,
        "events": events,
        "integrity": integrity,
    }


def _csv_text(
    records: Iterable[dict[str, Any]],
    *,
    include_outcome: bool = False,
    include_version: bool = False,
) -> str:
    columns = [
        "identifier",
        "datestamp",
        "deleted",
        "title",
        "creators",
        "subjects",
        "publisher",
        "date",
        "resource_type",
        "language",
        "rights",
        "record_hash",
    ]
    if include_outcome:
        columns.insert(1, "outcome")
    if include_version:
        columns.extend(["version_run_id", "version_seq", "changed_in_run"])
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for record in records:
        row = dict(record)
        for key in ("creators", "subjects"):
            row[key] = "; ".join(row.get(key) or [])
        row["deleted"] = "true" if row.get("deleted") else "false"
        if include_version:
            row["changed_in_run"] = "true" if row.get("changed_in_run") else "false"
        writer.writerow(row)
    return buffer.getvalue()


def _md(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _markdown(report: dict[str, Any]) -> str:
    run = report["run"]
    view = report["view"]
    counts = [
        ("页数", run["page_count"]),
        ("本运行观察记录数", run["record_count"]),
        ("新建", run["created_count"]),
        ("更新", run["updated_count"]),
        ("未变", run["unchanged_count"]),
        ("删除", run["deleted_count"]),
        ("错误", run["error_count"]),
    ]
    if view["complete"]:
        snapshot_line = "完整快照（运行已完成）"
    elif view["snapshot_status"] == "partial":
        snapshot_line = "部分快照（运行中断或失败，不构成完整仓库状态）"
    elif view["snapshot_status"] == "incomplete":
        snapshot_line = "历史不完整（旧数据库缺少版本历史，无法重建该运行）"
    else:
        snapshot_line = "无快照（运行尚未结束或早于版本历史）"
    lines = [
        f"# 元仓收割报告 · 运行 #{run['id']}",
        "",
        f"- 状态：`{run['status']}`",
        f"- 快照视图：{snapshot_line}",
        f"- 快照水位：`{view['watermark'] if view['watermark'] is not None else '(无)'}`",
        f"- 快照冻结时间：`{view['frozen_at'] or '(未冻结)'}`",
        f"- 完整快照记录数：{view['snapshot_record_count']}"
        f"（其中墓碑 {view['snapshot_deleted_count']}）",
        f"- 本次变更记录数：{view['changed_record_count']}",
        f"- 仓储：`{run['repository_url']}`",
        f"- 元数据格式：`{run['metadata_prefix']}`",
        f"- Set：`{run['set_spec'] or '(全部)'}`",
        f"- 时间窗：`{run['from_date'] or '(无)'}` → `{run['until_date'] or '(无)'}`",
        f"- 开始：`{run['started_at']}`",
        f"- 结束：`{run['finished_at'] or '(未结束)'}`",
        f"- 报告生成：`{report['generated_at']}`",
        "",
        "## 收割统计",
        "",
        "| 指标 | 数量 |",
        "| --- | ---: |",
        *[f"| {label} | {value} |" for label, value in counts],
        "",
        "## 原始响应完整性",
        "",
        f"- 校验结果：`{report['integrity']['status']}`",
        f"- 检查文件：{report['integrity']['checked_files']}",
        f"- 缺失文件：{len(report['integrity']['missing'])}",
        f"- SHA-256 不符：{len(report['integrity']['sha256_mismatches'])}",
        "",
        "| 页 | HTTP | 字节 | SHA-256 | 归档路径 |",
        "| ---: | ---: | ---: | --- | --- |",
    ]
    for item in report["raw_responses"]:
        lines.append(
            f"| {item['page_number']} | {item['http_status']} | {item['size_bytes']} | "
            f"`{item['sha256'][:16]}…` | `{_md(item['archive_path'])}` |"
        )
    lines.extend(["", "## 错误", ""])
    if report["errors"]:
        lines.extend(
            [
                "| 时间 | 范围 | 标识 | 代码 | 消息 |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for item in report["errors"]:
            lines.append(
                f"| {item['occurred_at']} | {item['scope']} | {_md(item['identifier'] or '')} | "
                f"{_md(item['code'] or '')} | {_md(item['message'])} |"
            )
    else:
        lines.append("无。")
    lines.extend(
        [
            "",
            "## 本次变更清单",
            "",
            "仅列出本运行实际改变的有效状态；未变记录不会出现在这里。",
            "",
            "| OAI 标识 | 结果 | 数据时间 | 删除 | 标题 | 内容哈希 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    if report["changes"]:
        for record in report["changes"]:
            lines.append(
                f"| `{_md(record['identifier'])}` | {record.get('outcome', '')} | "
                f"{record['datestamp']} | {'是' if record['deleted'] else '否'} | "
                f"{_md(record['title'] or '(无题名)')} | "
                f"`{record['record_hash'][:16]}…` |"
            )
    else:
        lines.append("| — | 无变更 | — | — | — | — |")
    lines.extend(
        [
            "",
            f"## 完整快照（水位 `{view['watermark']}`）",
            "",
            "每个标识在该运行结束时的有效版本；未变记录继承最近一次更早版本。",
            "",
            "| OAI 标识 | 数据时间 | 删除 | 标题 | 内容哈希 | 版本运行 |",
            "| --- | --- | --- | --- | --- | ---: |",
        ]
    )
    if report["records"]:
        for record in report["records"]:
            marker = "*" if record.get("changed_in_run") else ""
            lines.append(
                f"| `{_md(record['identifier'])}{marker}` | {record['datestamp']} | "
                f"{'是' if record['deleted'] else '否'} | "
                f"{_md(record['title'] or '(无题名)')} | "
                f"`{record['record_hash'][:16]}…` | {record['version_run_id']} |"
            )
    elif view["complete"]:
        lines.append("| — | — | — | （本次快照为空） | — | — |")
    else:
        lines.append("| — | — | — | （该运行没有可重建的完整快照） | — | — |")
    lines.extend(
        [
            "",
            "> `*` 表示该记录的有效状态在本运行中发生变化；其余记录继承自更早版本。",
            "> 本报告由本地归档的原始 OAI-PMH XML 支撑；SHA-256 校验失败时不应采信本次收割结果。",
            "",
        ]
    )
    return "\n".join(lines)


def write_reports(
    db: Database,
    run_id: int,
    output_dir: Path,
    formats: Iterable[str] = ("json", "csv", "markdown"),
) -> ReportOutput:
    report = build_report(db, run_id)
    root = ensure_directory(output_dir.expanduser().resolve())
    stem = f"harvest-run-{run_id:06d}"
    requested = {item.lower() for item in formats}
    if "md" in requested:
        requested.add("markdown")
    unknown = requested - {"json", "csv", "markdown"}
    if unknown:
        raise ReportError(f"未知报告格式: {', '.join(sorted(unknown))}")
    files: dict[str, Path] = {}
    if "json" in requested:
        path = root / f"{stem}.json"
        atomic_write_text(
            path, json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n"
        )
        files["json"] = path
    if "csv" in requested:
        # The CSV mirrors the complete snapshot; the change list gets its own
        # file so consumers cannot mistake one for the other.
        path = root / f"{stem}.csv"
        atomic_write_text(path, _csv_text(report["records"], include_version=True))
        files["csv"] = path
        changes_path = root / f"{stem}-changes.csv"
        atomic_write_text(
            changes_path, _csv_text(report["changes"], include_outcome=True)
        )
        files["changes_csv"] = changes_path
    if "markdown" in requested:
        path = root / f"{stem}.md"
        atomic_write_text(path, _markdown(report))
        files["markdown"] = path
    return ReportOutput(run_id=run_id, files=files, integrity=report["integrity"])
