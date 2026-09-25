"""Deterministic output formatting with an explicit token budget."""

import json
from collections.abc import Sequence
from typing import Any


def render(
    rows: Sequence[dict[str, Any]],
    *,
    output_format: str,
    max_tokens: int | None = None,
    snippet_chars: int | None = 400,
    format_version: int = 1,
) -> str:
    total = len(rows)
    prepared = [_truncate_text(dict(row), snippet_chars) for row in rows]
    kept = list(prepared)
    while kept and max_tokens is not None:
        candidate = _render_rows(kept, output_format)
        meta = _meta(total - len(kept), len(kept), total, format_version)
        with_meta = _append_meta(candidate, meta, output_format)
        if _tokens(with_meta) <= max_tokens:
            break
        kept.pop()
    truncated = total - len(kept)
    result = _render_rows(kept, output_format)
    if truncated:
        meta = _meta(truncated, len(kept), total, format_version)
        result = _append_meta(result, meta, output_format)
    return result


def _truncate_text(row: dict[str, Any], limit: int | None) -> dict[str, Any]:
    text = row.get("text")
    if limit is not None and isinstance(text, str) and len(text) > limit:
        row["text"] = text[: max(0, limit - 1)] + "…"
    return row


def _render_rows(rows: Sequence[dict[str, Any]], output_format: str) -> str:
    if output_format == "count":
        return f"{len(rows)}\n"
    if output_format == "jsonl":
        return "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows
        )
    if output_format == "md":
        lines: list[str] = []
        current_peer: str | None = None
        for row in rows:
            peer = str(row.get("peer", "results"))
            if peer != current_peer:
                lines.append(f"## {peer}")
                current_peer = peer
            tags = row.get("tags") or []
            tag_text = f" [{', '.join(tags)}]" if tags else ""
            text = str(row.get("text", "")).replace("\n", " ")
            sender = row.get("from") or "unknown"
            urls = " ".join(link["url"] for link in row.get("links") or [])
            link_text = f" — links: {urls}" if urls else ""
            lines.append(f"- `{row.get('id', '')}` {sender}{tag_text} — {text}{link_text}")
        return "\n".join(lines) + ("\n" if lines else "")
    if output_format == "table":
        if not rows:
            return ""
        keys = list(rows[0])
        values = [[_cell(row.get(key)) for key in keys] for row in rows]
        widths = [
            max(len(key), *(len(value[index]) for value in values))
            for index, key in enumerate(keys)
        ]
        lines = [
            "  ".join(key.ljust(widths[index]) for index, key in enumerate(keys)),
            "  ".join("-" * width for width in widths),
        ]
        lines.extend(
            "  ".join(value[index].ljust(widths[index]) for index in range(len(keys)))
            for value in values
        )
        return "\n".join(lines) + "\n"
    raise ValueError(f"unknown format: {output_format}")


def _append_meta(result: str, meta: dict[str, Any], output_format: str) -> str:
    if output_format == "jsonl":
        return result + json.dumps({"_meta": meta}, separators=(",", ":")) + "\n"
    if output_format == "md":
        return result + (
            f"_truncated: {meta['truncated']}; returned: {meta['returned']}; "
            f"total: {meta['total']}_\n"
        )
    if output_format == "table":
        return result + (
            f"[truncated={meta['truncated']} returned={meta['returned']} total={meta['total']}]\n"
        )
    return result


def _meta(truncated: int, returned: int, total: int, version: int) -> dict[str, Any]:
    return {
        "truncated": truncated,
        "returned": returned,
        "total": total,
        "format_version": version,
    }


def _tokens(text: str) -> int:
    return int(len(text) / 3.5 + 0.999)


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ",".join(
            str(item["url"]) if isinstance(item, dict) and "url" in item else str(item)
            for item in value
        )
    return str(value).replace("\n", " ")
