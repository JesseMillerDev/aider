"""Terminal presentation and read-only context inspection; never assembles a prompt."""

import json
import re
from pathlib import Path

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from aider.utils import is_image_file
from aider.documents import is_docx, read_docx


def clean(value):
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", "", str(value))


def tokens(value):
    if value is None:
        return "unknown"
    return f"{value / 1000:.1f}k" if value >= 1000 else str(value)


def session_status(coder):
    """Cheap status: no source reads, tokenization, repository scan, or LSP requests."""
    pager = coder.context_pager
    nav = coder.agent_navigator
    graph = getattr(coder.repo_map, "semantic_graph", None)
    bridge = graph.bridge if graph else getattr(nav, "bridge", None)
    branch = "no git"
    if coder.repo:
        try:
            branch = coder.repo.repo.active_branch.name
        except (TypeError, ValueError):
            branch = "detached"
    return {
        "model": clean(coder.main_model.name),
        "reasoning": coder.main_model.get_reasoning_effort(),
        "root": clean(coder.root),
        "branch": clean(branch),
        "edit_format": coder.edit_format,
        "yolo": bool(getattr(coder.io, "yolo", False)),
        "l1_files": len(coder.abs_fnames),
        "l2_files": len(coder.l2_interface_fnames),
        "read_only_files": len(coder.abs_read_only_fnames),
        "input_tokens": coder.total_tokens_sent,
        "output_tokens": coder.total_tokens_received,
        "paging": "automatic" if pager and pager.automatic else "manual" if pager else "off",
        "paging_settings": ({"idle_responses": pager.l1_ttl_turns,
                             "max_l1_tokens": pager.max_l1_tokens,
                             "max_l1_files": pager.max_l1_files,
                             "max_l2_tokens": pager.max_l2_tokens} if pager else None),
        "pager_stats": dict(pager.stats) if pager else {},
        "navigation": dict(nav.stats) if nav else None,
        "recent_navigation": list(nav.events) if nav else [],
        "lsp": {
            "ranking_enabled": graph is not None,
            "budget_seconds": graph.timeout if graph else None,
            "bridge_stats": dict(bridge.stats) if bridge else {},
            "last_map": dict(graph.stats) if graph else {},
            "maps": graph.map_count if graph else 0,
            "last_map_seconds": graph.last_elapsed_seconds if graph else None,
            "languages_started": sorted(getattr(bridge, "servers", {})) if bridge else [],
        },
        "map_tokens": coder.repo_map.max_map_tokens if coder.repo_map else 0,
    }


def context_status(coder):
    """Estimate currently loaded source only; inspecting never advances pager state."""
    result = session_status(coder)
    pager = coder.context_pager
    result["files"] = []
    groups = [("L1", coder.abs_fnames), ("L2", coder.l2_interface_fnames),
              ("read-only", coder.abs_read_only_fnames)]
    for level, paths in groups:
        for path in sorted(paths):
            note = "read-only source" if level == "read-only" else "full source"
            count = None
            try:
                if is_image_file(path) or str(path).lower().endswith(".pdf"):
                    raise ValueError("Non-text context")
                if level == "L2":
                    source = pager.sources.get(path)
                    text = pager.render_l2_stub(path, source) if source is not None else None
                    note = "interface; implementation omitted"
                else:
                    text = (read_docx(path) if is_docx(path)
                            else Path(path).read_text(encoding=coder.io.encoding))
                    if is_docx(path):
                        note = "read-only DOCX text; images/layout excluded"
                    elif pager and level == "L1":
                        note = pager.file_status(path)
                count = coder.main_model.token_count(text) if text is not None else None
                if not isinstance(count, int) or isinstance(count, bool):
                    count = None
            except (OSError, UnicodeError, ValueError):
                note = "source unavailable or non-text"
            result["files"].append({"level": level, "path": clean(coder.get_rel_fname(path)),
                                    "tokens": count, "state": note})
    result["source_tokens"] = {
        level: (sum(row["tokens"] for row in result["files"] if row["level"] == level)
                if all(row["tokens"] is not None for row in result["files"]
                       if row["level"] == level) else None)
        for level, _ in groups
    }
    result["estimate_note"] = (
        "Local source/interface estimates only. Excludes history, map, instructions, images and "
        "message framing; not provider usage or measured savings. File states reflect the last "
        "pager observation. Inspecting does not advance, demote or reload files."
    )
    return result


def toolbar(status, width=100):
    """One bounded, terminal-themed line; paths and model names remain literal text."""
    if width < 2:
        return []
    model = status["model"].split("/")[-1]
    branch = Text(status["branch"])
    branch.truncate(max(8, min(24, width // 4)), overflow="ellipsis")
    context = f"L1 {status['l1_files']} / L2 {status['l2_files']}"
    if status["read_only_files"]:
        context += f" / RO {status['read_only_files']}"
    items = (["YOLO"] if status.get("yolo") else []) + [model, branch.plain, context]
    if width >= 130:
        items.append(f"LSP {status['lsp']['bridge_stats'].get('queries', 0)} RPC")
        nav = status["navigation"]
        if nav is not None:
            items.append(f"nav {nav.get('succeeded', 0)} ok / {nav.get('failed', 0)} failed")
    if width >= 90:
        items.append(f"in {tokens(status['input_tokens'])} / out {tokens(status['output_tokens'])}")
    if width >= 65:
        items.append("/status")
    text = Text("  ·  ".join(items))
    text.truncate(max(0, width - 2), overflow="ellipsis")
    return [("class:bottom-toolbar", " " + text.plain)]


def show_header(io, coder, announcements):
    status = session_status(coder)
    content = Text()
    if status["yolo"]:
        content.append("YOLO · confirmations approved automatically\n", style="bold yellow")
    model_line = next((line.split(": ", 1)[1] for line in announcements
                       if line.startswith(("Model: ", "Main model: "))), status["model"])
    content.append(clean(model_line) + "\n", style="bold")
    content.append(f"{Path(status['root']).name}  /  {status['branch']}", style="cyan")
    content.append(f"  ·  map {tokens(status['map_tokens'])}\n", style="dim")
    lsp = "on" if status["lsp"]["ranking_enabled"] else "off"
    nav = "on" if status["navigation"] is not None else "off"
    content.append(f"LSP {lsp}  ·  navigation {nav}  ·  paging {status['paging']}\n")
    content.append("/status inspect files & activity    /help commands", style="dim")
    version = announcements[0].removeprefix("Aider ") if announcements else ""
    io.console.print(Panel(content, title=Text("Better Aider", style="bold cyan"),
                           subtitle=Text(clean(version), style="dim"),
                           border_style="dim", width=min(io.console.width, 104), padding=(0, 2)))
    for line in announcements:
        io.tool_output(line, log_only=True)
        if not line.startswith(("Aider v", "Model: ", "Main model: ", "Git repo: ", "Repo-map: ")):
            io.console.print(Text(clean(line)))


def show_context(io, coder, as_json=False):
    status = context_status(coder)
    if as_json:
        data = json.dumps(status, indent=2, ensure_ascii=True)
        io.console.print(data, markup=False, highlight=False, soft_wrap=True)
        io.tool_output(data, log_only=True)
        return
    summary = (
        f"L1 {status['l1_files']} files / ~{tokens(status['source_tokens']['L1'])} tokens  ·  "
        f"L2 {status['l2_files']} files / ~{tokens(status['source_tokens']['L2'])} tokens  ·  "
        f"read-only {status['read_only_files']}"
    )
    lsp = status["lsp"]
    rpc = lsp["bridge_stats"]
    latest = lsp["last_map"]
    navigation = status["navigation"]
    settings = status["paging_settings"]
    policy = ""
    if settings:
        uncapped = all(settings[key] is None for key in
                       ("max_l1_tokens", "max_l1_files", "max_l2_tokens"))
        policy = f" (idle {settings['idle_responses']}; " + (
            "uncapped)" if uncapped else "custom caps; see /status json)"
        )
    activity = [
        f"Paging {status['paging']}{policy}: "
        f"{status['pager_stats'].get('demotions', 0)} demotions, "
        f"{status['pager_stats'].get('full_file_reloads', 0)} navigation L1 loads/L2 reloads. "
        f"L2 in {status['pager_stats'].get('completed_requests_with_l2', 0)}/"
        f"{status['pager_stats'].get('completed_model_requests', 0)} completed requests.",
        f"LSP ranking {'on' if lsp['ranking_enabled'] else 'off'} · "
        f"{rpc.get('queries', 0)} RPC · "
        f"{rpc.get('timeouts_or_disconnects', 0)} timeouts/disconnects · "
        f"{rpc.get('missing_servers', 0)} unavailable servers.",
        (f"Last map: {latest.get('resolved_sites', 0)} resolved sites, "
         f"{latest.get('unknown_sites', 0)} unknown, "
         f"{latest.get('budget_exhausted', 0)} budget exhaustion events · "
         f"{lsp['last_map_seconds']:.2f}s."
         if lsp['last_map_seconds'] is not None else "LSP map: not generated yet."),
        (f"Navigation: {navigation.get('succeeded', 0)} succeeded, "
         f"{navigation.get('failed', 0)} failed, {navigation.get('empty', 0)} empty; "
         f"{navigation.get('invalid_responses', 0)} malformed responses."
         if navigation is not None else "Navigation off."),
    ]
    files = Table(box=None, expand=True, padding=(0, 1))
    for name in ("Level", "File", "~tokens", "Observed state"):
        files.add_column(name, overflow="fold", style="dim" if name == "Observed state" else None)
    for row in status["files"]:
        files.add_row(row["level"], Text(row["path"]), tokens(row["tokens"]), row["state"])
    plain = [summary, *activity]
    plain.extend(f"{row['level']}  {row['path']}  ~{tokens(row['tokens'])}  {row['state']}"
                 for row in status["files"])
    for event in status["recent_navigation"]:
        plain.append(f"/{event['action']} [{event['status']}] {event['detail']}")
    plain.append(status["estimate_note"])
    if not io.pretty or not io.console.is_terminal:
        for line in plain:
            io.tool_output(line)
        return
    recent = [Text(clean(line)) for line in plain[1 + len(activity) + len(status["files"]):-1]]
    estimate = ("~tokens estimates source only, not full prompt usage or savings.\n"
                "States reflect the last pager observation. Inspection does not change context.")
    body = Group(Text(summary, style="bold cyan"), Text("\n".join(activity)), Text(""), files,
                 *recent, Text("\n" + estimate, style="dim"))
    io.console.print(Panel(body, title="Context & activity", border_style="dim",
                           width=min(io.console.width, 120), padding=(1, 1)))
    for line in plain:
        io.tool_output(line, log_only=True)
