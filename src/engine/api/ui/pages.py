"""The three pages the surface renders itself.

The scorecard is not among them: it comes from :mod:`alerts_bi_operations.report.html`, rendered from
committed SQL rows, and this module never touches it. These are the index that starts a run
and the error page a browser gets when one is refused.

Every interpolated value goes through the report module's ``escape_html``, so the surface
escapes exactly the way the scorecard does.
"""

from __future__ import annotations

from alerts_bi_operations.report.html import escape_html

from ..schemas import TeamOut
from .assets import PAGE_CSS, SUBMIT_SCRIPT

__all__ = ["error_page", "index_page"]


def _page(title: str, body: str, script: str = "") -> str:
    tail = f"<script>{script}</script>" if script else ""
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape_html(title)}</title><style>{PAGE_CSS}</style></head>"
        f"<body>{body}{tail}</body></html>\n"
    )


def index_page(teams: list[TeamOut]) -> str:
    options = "\n".join(
        f'<option value="{escape_html(team.team_id)}">'
        f"{escape_html(team.display_name)} ({escape_html(team.team_id)})</option>"
        for team in teams
    )
    rows = "\n".join(
        f"<tr><td><code>{escape_html(team.team_id)}</code></td>"
        f"<td>{escape_html(team.display_name)}</td>"
        f"<td>{len(team.v1_operators)}</td>"
        f"<td>{escape_html(team.v2_operator or '—')}</td>"
        f"<td>{team.panels}</td></tr>"
        for team in teams
    )
    body = f"""
<h1>Alerts BI</h1>
<p class="sub">Start a run for one team. The response is that run's scorecard.</p>
<form method="post" action="/runs">
  <label for="team">Team</label>
  <select id="team" name="team" required>{options}</select>
  <label for="run_at">run_at (UTC, optional)</label>
  <input id="run_at" name="run_at" placeholder="2026-08-25T18:00:00Z">
  <label for="llm">Model</label>
  <select id="llm" name="llm">
    <option value="live">live — the on-prem model, if LLM_ENABLED</option>
    <option value="fake" selected>fake — deterministic client, for the mock</option>
    <option value="off">off — eligible identities become unassessed</option>
  </select>
  <button type="submit">Run</button>
  <p class="failure" id="failure" role="alert"></p>
  <p class="result" id="result" role="status"></p>
</form>
<p class="note">The finished scorecard downloads as a file, and the result line links to
it in a new tab; this page stays as it is, so the next run is one click away. A run reads
168 hours for one team and is serialized — a second request while one is running gets a
409. Against the mock dataset set <code>run_at</code> to
<code>2026-08-25T18:00:00Z</code>, which is the clock it was generated on.</p>
<h2>Registered teams</h2>
<table><thead><tr><th>team_id</th><th>display name</th><th>v1 operators</th>
<th>v2 operator</th><th>panels</th></tr></thead><tbody>{rows}</tbody></table>
<p class="note">Interactive API documentation: <a href="/docs">/docs</a> ·
<a href="/redoc">/redoc</a> · <a href="/openapi.json">openapi.json</a></p>
"""
    return _page("Alerts BI", body, SUBMIT_SCRIPT)


def error_page(code: int, detail: str) -> str:
    body = f'<h1>{code}</h1><p>{escape_html(detail)}</p><p class="note"><a href="/">Back</a></p>'
    return _page(str(code), body)
