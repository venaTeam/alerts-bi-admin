"""The page's stylesheet and its one script.

Inlined rather than served as files: the pages are two of them, and a static-file mount
would be more machinery than the whole UI.
"""

from __future__ import annotations

__all__ = ["PAGE_CSS", "SUBMIT_SCRIPT"]

PAGE_CSS = """
/* Every colour is a token with an explicit value in both schemes. `transparent` and
   `inherit` are avoided on form controls on purpose: a native <select> popup is painted by
   the browser on its own surface, so a transparent background there put white option text
   on a light popup and made every team invisible in dark mode. */
:root {
  color-scheme: light dark;
  --bg: #ffffff;
  --fg: #1f2328;
  --field-bg: #ffffff;
  --line: #d0d7de;
  --muted: #57606a;
  --error: #b3261e;
  --link: #0969da;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117;
    --fg: #e6edf3;
    --field-bg: #161b22;
    --line: #30363d;
    --muted: #9198a1;
    --error: #ff8080;
    --link: #4493f8;
  }
}
body { font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
  margin: 2.5rem auto; max-width: 46rem; padding: 0 1.25rem; line-height: 1.5;
  background: var(--bg); color: var(--fg); }
h1 { font-size: 1.3rem; margin-bottom: .25rem; }
p.sub { color: var(--muted); margin-top: 0; }
a { color: var(--link); }
form { border: 1px solid var(--line); border-radius: 8px; padding: 1rem 1.25rem; margin: 1.5rem 0; }
label { display: block; font-size: .85rem; margin: .75rem 0 .2rem; }
select, input, button { font: inherit; border: 1px solid var(--line); border-radius: 6px;
  background: var(--field-bg); color: var(--fg); }
select, input { padding: .4rem .5rem; width: 100%; box-sizing: border-box; }
option { background: var(--field-bg); color: var(--fg); }
button { margin-top: 1.1rem; padding: .5rem 1.1rem; cursor: pointer; }
button[disabled] { opacity: .6; cursor: progress; }
code { font-size: .85em; }
table { border-collapse: collapse; width: 100%; font-size: .86rem; margin-top: .5rem; }
th, td { text-align: left; padding: .35rem .5rem; border-bottom: 1px solid var(--line); }
.note { color: var(--muted); font-size: .85rem; }
.failure { color: var(--error); font-size: .9rem; margin-top: .75rem; }
.result { color: var(--muted); font-size: .9rem; margin-top: .75rem; }
"""

#: The form posts JSON so the request body matches the documented contract exactly, rather
#: than a second form-encoded shape the endpoint would also have to accept. It also gives a
#: run some feedback: a plain form submit shows nothing for the minutes a run can take.
#:
#: The finished scorecard is downloaded rather than written over this page. Replacing the
#: page destroyed the form, so starting a second run meant navigating back; a download
#: leaves the page intact and puts the report where the reader can keep it. The run also
#: has a real URL, so the result line offers that too - a tab that can be reloaded and
#: bookmarked, which a blob cannot.
SUBMIT_SCRIPT = """
document.querySelector('form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.target;
  const button = form.querySelector('button');
  const failure = document.getElementById('failure');
  const result = document.getElementById('result');
  failure.textContent = '';
  result.textContent = '';
  button.disabled = true;
  button.textContent = 'Running\\u2026';
  const data = Object.fromEntries(new FormData(form).entries());
  if (!data.run_at) delete data.run_at;
  try {
    const response = await fetch('/runs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/html' },
      body: JSON.stringify(data),
    });
    const body = await response.text();
    if (!response.ok) {
      // The server renders a readable error page. Parse it for its text rather than
      // injecting its markup, and keep the form usable so the run can be corrected.
      const parsed = new DOMParser().parseFromString(body, 'text/html');
      const detail = parsed.querySelector('p');
      const message = (detail ? detail.textContent : '').trim();
      failure.textContent = response.status + ' — ' + (message || 'the run was refused');
    } else {
      const runId = response.headers.get('X-Alerts-BI-Run-Id') || '';
      const team = response.headers.get('X-Alerts-BI-Team') || 'run';
      const name = 'scorecard-' + team + (runId ? '-' + runId.slice(0, 8) : '') + '.html';
      const url = URL.createObjectURL(new Blob([body], { type: 'text/html' }));
      const download = document.createElement('a');
      download.href = url;
      download.download = name;
      document.body.appendChild(download);
      download.click();
      download.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60000);

      result.textContent = 'Downloaded ' + name + ' \\u2014 ';
      const open = document.createElement('a');
      open.href = '/runs/' + runId;
      open.target = '_blank';
      open.rel = 'noopener';
      open.textContent = 'open it in a new tab';
      result.appendChild(open);
    }
  } catch (error) {
    // A dead server must say so. Leaving the button stuck on 'Running' made an
    // unreachable service look like a slow run.
    failure.textContent =
      'Could not reach the service: ' + error + '. Is it still running?';
  } finally {
    button.disabled = false;
    button.textContent = 'Run';
  }
});
"""
