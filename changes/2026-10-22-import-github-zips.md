# Add change notes for the private-network import

Branch: feat/import-github-zips

## What changed

Adds this `changes/` folder and the rule that every pull request leaves a note in it. The
private network imports this repository from GitHub ZIPs, which carry no commit messages; the
import agent reads these notes to learn what changed and how to deploy it
(alerts-bi-design `docs/private-network-import.md`).

## Deploy

- Migrations: none
- Settings: none
- Order: any
- Other steps: none

## Private network

- Dockerfile: no change
- CI: no change
- Packages: none
