# Change notes

Every pull request to `main` adds one note in this folder, named
`<YYYY-MM-DD>-<branch description>.md`, for example `2026-10-22-case-sensitive-key-fields.md`.

This repository reaches the private network as a GitHub ZIP, without commit messages or pull
request descriptions. The agent that imports it there reads the notes added since the last
import to learn what changed and what the private side must do (alerts-bi-design
`docs/private-network-import.md`). Write for a reader who has not seen the pull request. Never
edit a merged note; a later pull request adds its own.

Create the note with `poetry run python changes/notes.py new` on the pull request's branch.
It writes the file below with the facts filled in from the diff with `main` (new migrations,
`.env.example` keys, Dockerfile and CI changes, `poetry.lock` package versions) and TODO for
what only the author knows. Replace every TODO, check the facts, and commit the note. CI runs
`python changes/notes.py check` on every pull request and fails when the note is missing, still
has a TODO, or contradicts the diff. The comparison goes one way: it fails when the diff has a
migration, setting, package, Dockerfile or CI change the note misses, not when a note says
more than the diff shows.

The format, if you write a note by hand (write "none" or "no change" instead of deleting a
line):

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
