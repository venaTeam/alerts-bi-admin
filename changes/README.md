# Change notes

Every pull request to `main` adds one note in this folder, named
`<YYYY-MM-DD>-<branch description>.md`, for example `2026-10-22-case-sensitive-key-fields.md`.

This repository reaches the private network as a GitHub ZIP, without commit messages or pull
request descriptions. The agent that imports it there reads the notes added since the last
import to learn what changed and what the private side must do (alerts-bi-design
`docs/private-network-import.md`). Write for a reader who has not seen the pull request. Never
edit a merged note; a later pull request adds its own.

Copy the template, replace every `<...>`, and write "none" or "no change" instead of deleting
a line:

````markdown
# <Pull request title>

Branch: <branch name>

## What changed

<One to three sentences: what behavior or structure changed, and why.>

## Deploy

- Migrations: <new migration ids, or "none">
- Settings: <environment keys added, removed or changed in meaning, or "none">
- Order: <deploy order across repositories, or "any">
- Other steps: <manual steps, or "none">

## Private network

- Dockerfile: <logic to port, for example "copy the new docs/ folder", or "no change">
- CI: <check added, changed or removed, or "no change">
- Packages: <new or changed packages to request in Artifactory, or "none">
````
