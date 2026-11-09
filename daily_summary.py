"""
Daily Summary Engine for CodeRabbit Auto-Reviewer.
Generates comprehensive daily digests of open PRs, recently merged PRs,
attention-required items, review activity, and per-repository health.
Provides Markdown and HTML formatting for sharing and downloads.
"""

import html
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional

logger = logging.getLogger("daily_summary")


def parse_iso_datetime(dt_str: Optional[str]) -> Optional[datetime]:
    """Safely parses an ISO datetime string into a timezone-aware UTC datetime."""
    if not dt_str or not isinstance(dt_str, str):
        return None
    try:
        clean = dt_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def format_relative_time(dt_str: Optional[str], now: Optional[datetime] = None) -> str:
    """Formats an ISO timestamp into a human-friendly relative string."""
    dt = parse_iso_datetime(dt_str)
    if not dt:
        return "Unknown"
    now = now or datetime.now(timezone.utc)
    diff = int((now - dt).total_seconds())

    if diff < 0:
        return "Just now"
    if diff < 60:
        return f"{diff}s ago"
    if diff < 3600:
        return f"{diff // 60}m ago"
    if diff < 86400:
        hours = diff // 3600
        return f"{hours}h ago"
    days = diff // 86400
    if days == 1:
        return "Yesterday"
    if days < 30:
        return f"{days}d ago"
    return dt.strftime("%Y-%m-%d")


def format_friendly_datetime(dt_str: Optional[str]) -> str:
    """Formats an ISO timestamp into 'YYYY-MM-DD HH:MM UTC'."""
    dt = parse_iso_datetime(dt_str)
    if not dt:
        return "Unknown"
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def generate_daily_summary(
    annotated_status: Dict[str, Any],
    github_client: Any,
    state_manager: Any,
    hours: int = 24,
) -> Dict[str, Any]:
    """
    Generates a full daily summary data structure including:
    - High-level metrics
    - Open PRs breakdown (created, last updated, status)
    - Merged PRs on the day (created, merged_at, updated_at)
    - Needs attention triage
    - 24-hour automated review activity
    - Repository health breakdown
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    cutoff_iso = cutoff.isoformat()
    today_date = now.strftime("%Y-%m-%d")

    raw_prs = annotated_status.get("pull_requests", [])
    open_prs: List[Dict[str, Any]] = []

    for pr in raw_prs:
        if not isinstance(pr, dict):
            continue
        created_at = pr.get("created_at", "")
        updated_at = pr.get("updated_at", "")
        status_badge = pr.get("status_badge", "PENDING_REVIEW")
        status_label = pr.get("status_label", status_badge.replace("_", " ").title())
        has_conflict = bool(pr.get("has_conflict", False))
        has_check_error = bool(pr.get("has_check_error", False))
        ready_to_merge = bool(pr.get("ready_to_merge", False))

        open_prs.append({
            "pr_key": pr.get("pr_key", ""),
            "repo": pr.get("repo", ""),
            "number": pr.get("number", 0),
            "title": pr.get("title", ""),
            "author": pr.get("author", "unknown"),
            "html_url": pr.get("html_url", ""),
            "created_at": created_at,
            "created_at_friendly": format_friendly_datetime(created_at),
            "created_at_relative": format_relative_time(created_at, now),
            "updated_at": updated_at,
            "updated_at_friendly": format_friendly_datetime(updated_at),
            "updated_at_relative": format_relative_time(updated_at, now),
            "status_badge": status_badge,
            "status_label": status_label,
            "has_conflict": has_conflict,
            "mergeable_state": pr.get("mergeable_state", "unknown"),
            "has_check_error": has_check_error,
            "checks_status": pr.get("checks_status"),
            "failed_checks": pr.get("failed_checks", []),
            "ready_to_merge": ready_to_merge,
            "report_file": pr.get("report_file", ""),
            "is_own_pr": bool(pr.get("is_own_pr", False)),
        })

    # Discover PRs merged on the day / in the window
    merged_prs: List[Dict[str, Any]] = []
    repositories = annotated_status.get("repositories", [])
    if github_client and hasattr(github_client, "list_recently_merged_prs"):
        for repo_info in repositories:
            if not repo_info.get("enabled", True):
                continue
            full_name = repo_info.get("full_name", "")
            if not full_name:
                continue
            try:
                owner, repo_name = full_name.split("/", 1)
            except ValueError:
                continue

            try:
                repo_merged = github_client.list_recently_merged_prs(owner, repo_name, since_timestamp=cutoff_iso)
                for m_pr in repo_merged:
                    if not isinstance(m_pr, dict):
                        continue
                    num = m_pr.get("number", 0)
                    m_created = m_pr.get("created_at", "")
                    m_merged = m_pr.get("merged_at", "")
                    m_updated = m_pr.get("updated_at", "")
                    user_login = (m_pr.get("user") or {}).get("login", "unknown")
                    base_ref = (m_pr.get("base") or {}).get("ref", "main")

                    merged_prs.append({
                        "pr_key": f"{full_name}#{num}",
                        "repo": full_name,
                        "number": num,
                        "title": m_pr.get("title", ""),
                        "author": user_login,
                        "html_url": m_pr.get("html_url", ""),
                        "created_at": m_created,
                        "created_at_friendly": format_friendly_datetime(m_created),
                        "created_at_relative": format_relative_time(m_created, now),
                        "merged_at": m_merged,
                        "merged_at_friendly": format_friendly_datetime(m_merged),
                        "merged_at_relative": format_relative_time(m_merged, now),
                        "updated_at": m_updated,
                        "base_ref": base_ref,
                        "status": "MERGED",
                        "status_badge": "MERGED",
                        "status_label": "Merged",
                    })
            except Exception as e:
                logger.warning("Could not fetch merged PRs for %s: %s", full_name, e)

    # Sort open PRs and merged PRs
    open_prs.sort(key=lambda p: p.get("updated_at", ""), reverse=True)
    merged_prs.sort(key=lambda p: p.get("merged_at", ""), reverse=True)

    # Needs Attention Triage
    needs_attention: List[Dict[str, Any]] = []
    for pr in open_prs:
        reasons = []
        if pr.get("has_conflict"):
            reasons.append("Merge conflict with base branch")
        if pr.get("has_check_error"):
            failed = pr.get("failed_checks") or []
            failed_str = f" ({', '.join(failed[:3])})" if failed else ""
            reasons.append(f"CI checks failing{failed_str}")
        if pr.get("status_badge") in ("RATE_LIMITED", "ERROR"):
            reasons.append(f"Review issue: {pr.get('status_label')}")

        if reasons:
            item = dict(pr)
            item["reasons"] = reasons
            needs_attention.append(item)

    # Automated Review Activity in the last 24h
    recent_reviews: List[Dict[str, Any]] = []
    if state_manager and hasattr(state_manager, "get_all_recent_logs"):
        raw_logs = state_manager.get_all_recent_logs(hours=hours)
        for log in raw_logs:
            ts = log.get("timestamp", "")
            recent_reviews.append({
                "log_id": log.get("log_id", ""),
                "pr_key": log.get("pr_key", ""),
                "repo": log.get("repo", ""),
                "pr_number": log.get("pr_number", 0),
                "action": log.get("action", ""),
                "outcome": log.get("outcome", "REVIEWED"),
                "account_id": log.get("account_id", ""),
                "account_name": log.get("account_name", "Default"),
                "duration_seconds": log.get("duration_seconds", 0),
                "timestamp": ts,
                "timestamp_friendly": format_friendly_datetime(ts),
                "timestamp_relative": format_relative_time(ts, now),
                "report_file": log.get("report_file", ""),
                "summary": log.get("summary", ""),
            })

    # Per-repository breakdown
    repo_breakdown: List[Dict[str, Any]] = []
    for repo_info in repositories:
        full_name = repo_info.get("full_name", "")
        if not full_name:
            continue
        enabled = bool(repo_info.get("enabled", True))
        repo_open = [p for p in open_prs if p.get("repo") == full_name]
        repo_merged = [p for p in merged_prs if p.get("repo") == full_name]
        repo_attention = [p for p in needs_attention if p.get("repo") == full_name]
        repo_clean = [p for p in repo_open if p.get("ready_to_merge")]

        repo_breakdown.append({
            "repo": full_name,
            "enabled": enabled,
            "open_count": len(repo_open),
            "merged_count": len(repo_merged),
            "attention_count": len(repo_attention),
            "ready_to_merge_count": len(repo_clean),
        })

    # High-level Metrics
    metrics = {
        "total_open_prs": len(open_prs),
        "merged_today": len(merged_prs),
        "reviews_completed_24h": len(recent_reviews),
        "needs_attention_count": len(needs_attention),
        "ready_to_merge_count": sum(1 for p in open_prs if p.get("ready_to_merge")),
        "auto_approved_count": sum(1 for p in open_prs if p.get("status_badge") == "APPROVED_BY_YOU"),
        "comments_count": sum(1 for p in open_prs if p.get("status_badge") == "COMMENTS_POSTED"),
        "conflict_count": sum(1 for p in open_prs if p.get("has_conflict")),
        "check_error_count": sum(1 for p in open_prs if p.get("has_check_error")),
        "total_repositories": len([r for r in repositories if r.get("enabled", True)]),
    }

    return {
        "date": today_date,
        "generated_at": now.isoformat(),
        "generated_at_friendly": format_friendly_datetime(now.isoformat()),
        "window_hours": hours,
        "metrics": metrics,
        "open_prs": open_prs,
        "merged_prs": merged_prs,
        "needs_attention": needs_attention,
        "recent_reviews": recent_reviews,
        "repository_breakdown": repo_breakdown,
    }


def format_markdown_summary(summary_data: Dict[str, Any]) -> str:
    """Formats the daily summary into a clean GitHub-Flavored Markdown report."""
    date_str = summary_data.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    gen_at = summary_data.get("generated_at_friendly", "Just now")
    metrics = summary_data.get("metrics", {})
    open_prs = summary_data.get("open_prs", [])
    merged_prs = summary_data.get("merged_prs", [])
    attention = summary_data.get("needs_attention", [])
    reviews = summary_data.get("recent_reviews", [])
    repos = summary_data.get("repository_breakdown", [])

    lines = [
        f"# 🐰 CodeRabbit Daily PR Summary - {date_str}",
        f"*Generated at {gen_at} (covering past {summary_data.get('window_hours', 24)}h)*\n",
        "## 📊 Daily Overview",
        "",
        "| Metric | Count |",
        "| :--- | :---: |",
        f"| 📂 **Total Open PRs** | **{metrics.get('total_open_prs', 0)}** |",
        f"| 🟣 **Merged Today** | **{metrics.get('merged_today', 0)}** |",
        f"| ⚡ **Reviews Completed (24h)** | **{metrics.get('reviews_completed_24h', 0)}** |",
        f"| ⚠️ **Needs Attention** | **{metrics.get('needs_attention_count', 0)}** |",
        f"| ✅ **Ready to Merge** | **{metrics.get('ready_to_merge_count', 0)}** |",
        f"| 🟢 **Auto-Approved** | **{metrics.get('auto_approved_count', 0)}** |",
        f"| 💬 **Comments Posted** | **{metrics.get('comments_count', 0)}** |",
        "",
    ]

    # Needs Attention section
    if attention:
        lines.append(f"## ⚠️ Action Required ({len(attention)})\n")
        for item in attention:
            title = item.get("title", "")
            url = item.get("html_url") or "#"
            key = item.get("pr_key", "")
            author = item.get("author", "unknown")
            reasons = "; ".join(item.get("reasons", []))
            lines.append(f"- [**{key}**: {title}]({url}) by @{author} — ⚠️ *{reasons}*")
        lines.append("")

    # Open PRs Breakdown
    lines.append(f"## 📂 Open Pull Requests ({len(open_prs)})\n")
    if open_prs:
        lines.append("| PR | Repository | Author | Created | Last Updated | Status | CI / Merge |")
        lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
        for p in open_prs:
            title = p.get("title", "").replace("|", "\\|")
            url = p.get("html_url") or "#"
            num = p.get("number")
            key = f"[#{num}]({url})"
            repo = p.get("repo", "")
            author = f"@{p.get('author', 'unknown')}"
            created = f"{p.get('created_at_relative', '')} <br><sub>`{p.get('created_at_friendly', '')}`</sub>"
            updated = f"{p.get('updated_at_relative', '')} <br><sub>`{p.get('updated_at_friendly', '')}`</sub>"
            status = p.get("status_label", "Unknown")

            # Flags
            flags = []
            if p.get("has_conflict"):
                flags.append("❌ Conflict")
            elif p.get("ready_to_merge"):
                flags.append("✅ Clean")
            if p.get("has_check_error"):
                flags.append("❌ CI Failed")
            elif p.get("checks_status") == "PASSED":
                flags.append("✓ CI Pass")
            flag_str = ", ".join(flags) if flags else "-"

            lines.append(f"| {key}: {title} | `{repo}` | {author} | {created} | {updated} | `{status}` | {flag_str} |")
        lines.append("")
    else:
        lines.append("*No open pull requests across monitored repositories.*\n")

    # Merged PRs Breakdown
    lines.append(f"## 🟣 Merged Pull Requests Today ({len(merged_prs)})\n")
    if merged_prs:
        lines.append("| PR | Repository | Author | Created | Merged At | Branch |")
        lines.append("| :--- | :--- | :--- | :--- | :--- | :--- |")
        for m in merged_prs:
            title = m.get("title", "").replace("|", "\\|")
            url = m.get("html_url") or "#"
            num = m.get("number")
            key = f"[#{num}]({url})"
            repo = m.get("repo", "")
            author = f"@{m.get('author', 'unknown')}"
            created = f"{m.get('created_at_relative', '')} <br><sub>`{m.get('created_at_friendly', '')}`</sub>"
            merged = f"{m.get('merged_at_relative', '')} <br><sub>`{m.get('merged_at_friendly', '')}`</sub>"
            branch = f"`{m.get('base_ref', 'main')}`"
            lines.append(f"| {key}: {title} | `{repo}` | {author} | {created} | {merged} | {branch} |")
        lines.append("")
    else:
        lines.append("*No pull requests merged today in monitored repositories.*\n")

    # Automated Review Activity
    if reviews:
        lines.append(f"## ⚡ 24-Hour Review Activity ({len(reviews)})\n")
        lines.append("| Time | PR | Account | Outcome | Duration |")
        lines.append("| :--- | :--- | :--- | :--- | :---: |")
        for r in reviews[:20]:
            ts = r.get("timestamp_relative", "")
            pr_k = r.get("pr_key", "")
            acc = r.get("account_name", "Default")
            outcome = r.get("outcome", "REVIEWED")
            dur = f"{r.get('duration_seconds', 0)}s"
            lines.append(f"| {ts} | **{pr_k}** | {acc} | `{outcome}` | {dur} |")
        lines.append("")

    # Repository Breakdown
    if repos:
        lines.append("## 📁 Monitored Repositories\n")
        lines.append("| Repository | Open PRs | Merged Today | Needs Attention | Ready to Merge |")
        lines.append("| :--- | :---: | :---: | :---: | :---: |")
        for rep in repos:
            name = rep.get("repo", "")
            lines.append(
                f"| `{name}` | {rep.get('open_count', 0)} | {rep.get('merged_count', 0)} | {rep.get('attention_count', 0)} | {rep.get('ready_to_merge_count', 0)} |"
            )
        lines.append("")

    return "\n".join(lines)


def format_html_summary(summary_data: Dict[str, Any]) -> str:
    """Formats the daily summary into a standalone styled HTML report."""
    date_str = summary_data.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    gen_at = summary_data.get("generated_at_friendly", "Just now")
    metrics = summary_data.get("metrics", {})
    open_prs = summary_data.get("open_prs", [])
    merged_prs = summary_data.get("merged_prs", [])
    attention = summary_data.get("needs_attention", [])
    reviews = summary_data.get("recent_reviews", [])
    repos = summary_data.get("repository_breakdown", [])

    def esc(text: Any) -> str:
        return html.escape(str(text or ""))

    # Cards HTML
    cards_html = f"""
    <div class="stats-grid">
        <div class="stat-card">
            <div class="stat-val">{metrics.get('total_open_prs', 0)}</div>
            <div class="stat-lbl">📂 Open PRs</div>
        </div>
        <div class="stat-card">
            <div class="stat-val">{metrics.get('merged_today', 0)}</div>
            <div class="stat-lbl">🟣 Merged Today</div>
        </div>
        <div class="stat-card">
            <div class="stat-val">{metrics.get('reviews_completed_24h', 0)}</div>
            <div class="stat-lbl">⚡ Reviews (24h)</div>
        </div>
        <div class="stat-card {'stat-alert' if metrics.get('needs_attention_count', 0) > 0 else ''}">
            <div class="stat-val">{metrics.get('needs_attention_count', 0)}</div>
            <div class="stat-lbl">⚠️ Needs Attention</div>
        </div>
        <div class="stat-card">
            <div class="stat-val">{metrics.get('ready_to_merge_count', 0)}</div>
            <div class="stat-lbl">✅ Ready to Merge</div>
        </div>
        <div class="stat-card">
            <div class="stat-val">{metrics.get('auto_approved_count', 0)}</div>
            <div class="stat-lbl">🟢 Auto-Approved</div>
        </div>
    </div>
    """

    # Open PRs Rows
    open_rows = ""
    for p in open_prs:
        conflict_badge = '<span class="badge badge-red">Conflict</span>' if p.get('has_conflict') else ''
        check_badge = '<span class="badge badge-red">CI Fail</span>' if p.get('has_check_error') else ('<span class="badge badge-green">CI Pass</span>' if p.get('checks_status') == 'PASSED' else '')
        ready_badge = '<span class="badge badge-green">Ready</span>' if p.get('ready_to_merge') else ''

        open_rows += f"""
        <tr>
            <td>
                <a href="{esc(p.get('html_url'))}" target="_blank" class="pr-link"><strong>{esc(p.get('pr_key'))}</strong>: {esc(p.get('title'))}</a>
            </td>
            <td><code>{esc(p.get('repo'))}</code></td>
            <td>@{esc(p.get('author'))}</td>
            <td>
                <div>{esc(p.get('created_at_relative'))}</div>
                <div class="text-sub">{esc(p.get('created_at_friendly'))}</div>
            </td>
            <td>
                <div>{esc(p.get('updated_at_relative'))}</div>
                <div class="text-sub">{esc(p.get('updated_at_friendly'))}</div>
            </td>
            <td><span class="badge badge-info">{esc(p.get('status_label'))}</span></td>
            <td>{conflict_badge} {check_badge} {ready_badge}</td>
        </tr>
        """

    # Merged PRs Rows
    merged_rows = ""
    for m in merged_prs:
        merged_rows += f"""
        <tr>
            <td>
                <a href="{esc(m.get('html_url'))}" target="_blank" class="pr-link"><strong>{esc(m.get('pr_key'))}</strong>: {esc(m.get('title'))}</a>
            </td>
            <td><code>{esc(m.get('repo'))}</code></td>
            <td>@{esc(m.get('author'))}</td>
            <td>
                <div>{esc(m.get('created_at_relative'))}</div>
                <div class="text-sub">{esc(m.get('created_at_friendly'))}</div>
            </td>
            <td>
                <div>{esc(m.get('merged_at_relative'))}</div>
                <div class="text-sub">{esc(m.get('merged_at_friendly'))}</div>
            </td>
            <td><code>{esc(m.get('base_ref'))}</code></td>
        </tr>
        """

    # Attention items
    attention_html = ""
    if attention:
        items = "".join(f"""
        <div class="attention-item">
            <a href="{esc(a.get('html_url'))}" target="_blank"><strong>{esc(a.get('pr_key'))}</strong>: {esc(a.get('title'))}</a>
            <span class="text-sub">by @{esc(a.get('author'))}</span>
            <div class="attention-reasons">⚠️ {esc('; '.join(a.get('reasons', [])))}</div>
        </div>
        """ for a in attention)
        attention_html = f"""
        <div class="section-card">
            <h2 class="section-title">⚠️ Action Required ({len(attention)})</h2>
            {items}
        </div>
        """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Daily PR Summary - {esc(date_str)}</title>
    <style>
        :root {{
            --bg: #0f172a;
            --surface: #1e293b;
            --surface-hover: #334155;
            --border: #334155;
            --text: #f8fafc;
            --text-muted: #94a3b8;
            --primary: #6366f1;
            --success: #10b981;
            --danger: #ef4444;
            --warning: #f59e0b;
        }}
        body {{
            background: var(--bg);
            color: var(--text);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            margin: 0;
            padding: 2rem;
            line-height: 1.5;
        }}
        .container {{ max-width: 1100px; margin: 0 auto; }}
        header {{ margin-bottom: 2rem; border-bottom: 1px solid var(--border); padding-bottom: 1rem; }}
        h1 {{ margin: 0 0 0.5rem 0; font-size: 1.8rem; display: flex; align-items: center; gap: 0.5rem; }}
        .text-sub {{ font-size: 0.8rem; color: var(--text-muted); }}
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 1rem;
            margin-bottom: 2rem;
        }}
        .stat-card {{
            background: var(--surface);
            padding: 1.25rem;
            border-radius: 8px;
            border: 1px solid var(--border);
            text-align: center;
        }}
        .stat-alert {{ border-color: var(--danger); background: rgba(239, 68, 68, 0.08); }}
        .stat-val {{ font-size: 1.8rem; font-weight: bold; margin-bottom: 0.25rem; }}
        .stat-lbl {{ font-size: 0.85rem; color: var(--text-muted); font-weight: 500; }}
        .section-card {{
            background: var(--surface);
            border-radius: 8px;
            border: 1px solid var(--border);
            padding: 1.25rem;
            margin-bottom: 2rem;
        }}
        .section-title {{ margin-top: 0; font-size: 1.25rem; margin-bottom: 1rem; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; text-align: left; }}
        th, td {{ padding: 0.75rem; border-bottom: 1px solid var(--border); }}
        th {{ color: var(--text-muted); font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; }}
        a.pr-link {{ color: #a5b4fc; text-decoration: none; }}
        a.pr-link:hover {{ text-decoration: underline; }}
        .badge {{
            display: inline-block;
            padding: 0.2rem 0.5rem;
            border-radius: 4px;
            font-size: 0.75rem;
            font-weight: 600;
        }}
        .badge-info {{ background: rgba(99, 102, 241, 0.2); color: #c7d2fe; }}
        .badge-green {{ background: rgba(16, 185, 129, 0.2); color: #6ee7b7; }}
        .badge-red {{ background: rgba(239, 68, 68, 0.2); color: #fca5a5; }}
        .attention-item {{ padding: 0.75rem 0; border-bottom: 1px solid var(--border); }}
        .attention-item:last-child {{ border-bottom: none; }}
        .attention-reasons {{ color: var(--danger); font-size: 0.85rem; margin-top: 0.25rem; }}
        code {{ background: rgba(255, 255, 255, 0.1); padding: 0.15rem 0.3rem; border-radius: 3px; font-size: 0.85rem; }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🐰 CodeRabbit Daily PR Summary</h1>
            <div class="text-sub">Date: {esc(date_str)} | Generated at {esc(gen_at)} | Past {summary_data.get('window_hours', 24)}h</div>
        </header>

        {cards_html}
        {attention_html}

        <div class="section-card">
            <h2 class="section-title">📂 Open Pull Requests ({len(open_prs)})</h2>
            {"<table><thead><tr><th>PR</th><th>Repository</th><th>Author</th><th>Created</th><th>Last Updated</th><th>Status</th><th>Health</th></tr></thead><tbody>" + open_rows + "</tbody></table>" if open_prs else "<p class='text-sub'>No open PRs found.</p>"}
        </div>

        <div class="section-card">
            <h2 class="section-title">🟣 Merged Pull Requests Today ({len(merged_prs)})</h2>
            {"<table><thead><tr><th>PR</th><th>Repository</th><th>Author</th><th>Created</th><th>Merged At</th><th>Base Branch</th></tr></thead><tbody>" + merged_rows + "</tbody></table>" if merged_prs else "<p class='text-sub'>No pull requests merged today.</p>"}
        </div>
    </div>
</body>
</html>
    """
