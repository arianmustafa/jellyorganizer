"""Matching evidence saved with decisions, without another provider request."""

from jellyorganize.metadata.matcher import local_title_year, normalize


def explain(proposal):
    item = proposal.item
    title, year = local_title_year(item)
    rows = list(proposal.alternatives)
    if proposal.candidate and proposal.candidate not in rows:
        rows.append(proposal.candidate)
    return {
        "parsed_title": item.hints.get("title"), "parsed_year": item.hints.get("year"),
        "folder_title": item.hints.get("folder_title") or item.hints.get("package_title"),
        "folder_year": item.hints.get("folder_year") or item.hints.get("package_year"),
        "effective_title": title, "effective_year": year,
        "release_group": item.hints.get("release_group"),
        "decision": proposal.reason,
        "candidates": [{"provider": row.provider, "id": row.provider_id,
                        "title": row.title, "year": row.year,
                        "title_matches": bool(title and normalize(title) == normalize(row.title)),
                        "year_matches": year is not None and year == row.year}
                       for row in rows],
    }


def lines(evidence):
    if not evidence:
        return []
    result = [f"Parsed: {evidence['parsed_title']!r} ({evidence['parsed_year'] or '?'})",
              f"Matching as: {evidence['effective_title']!r} ({evidence['effective_year'] or '?'})"]
    if evidence.get("release_group"):
        result.append(f"Parsed release group: {evidence['release_group']!r}")
    for row in evidence.get("candidates", []):
        comparison = "; ".join(["title matches" if row["title_matches"] else "title differs",
                                 "year matches" if row["year_matches"] else "year differs or is absent"])
        result.append(f"{row['title']} ({row['year'] or '?'}) [{row['provider']}:{row['id']}]: {comparison}")
    return result
