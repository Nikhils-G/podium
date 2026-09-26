from podium.seed import SeedSummary


def format_summary(summary: SeedSummary, password: str) -> str:
    lines: list[str] = []
    if summary.report is not None:
        counts = ", ".join(f"{v} {k}" for k, v in summary.report.counts.items()) or "nothing new"
        lines.append(f"fixtures imported into event '{summary.report.event_slug}': {counts}")
        for dup, original in summary.report.duplicates:
            lines.append(f"  flagged duplicate submission: {dup} duplicates {original}")
        for warning in summary.report.warnings:
            lines.append(f"  warning: {warning}")
    if summary.logins:
        lines.append("seeded. test logins:")
        width = max(len(login.role) for login in summary.logins)
        for login in summary.logins:
            note = f"   ({login.email}{', ' + login.note if login.note else ''})"
            lines.append(f"  {login.role.ljust(width)}  {login.cookie}{note}")
        lines.append(f"  password for every demo and fixture user: {password}")
    return "\n".join(lines)
