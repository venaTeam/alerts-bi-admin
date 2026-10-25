# Add change notes for the private-network import

Branch: feat/import-github-zips

## What changed

Adds `changes/`: every pull request now leaves a note that the private network's import agent
reads, because GitHub ZIPs carry no commit messages. `changes/notes.py new` drafts the note's
facts from the diff; the new CI job runs `changes/notes.py check` on pull requests.

## Deploy

- Migrations: none
- Settings: none
- Order: any
- Other steps: none

## Private network

- Dockerfile: no change
- CI: changed (.github/workflows/checks.yml): adds the change-note job. GitHub-only, do not port.
- Packages: none
