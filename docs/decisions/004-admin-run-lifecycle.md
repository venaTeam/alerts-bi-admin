# Admin run lifecycle — accepted 2026-10-13

The product owner requested Monday-to-Monday runs, full run deletion, and removal of the
admin publication gap option. See design section 7.12 for the authoritative scope,
transaction, audit-history exception, shared-cache boundary and scheduler behavior.

Admin owns manual entry-point validation, the delete POST, generated report cleanup and
new migration 009. Portal continues to read the same SQL views; no view or shared-library
contract changes. Apply 009 with the existing guarded database setup before using deletion.
Merge design and admin together; deploy only with separate authorization. Rolling the app
back leaves the additive trigger revision installed. A deleted run cannot be restored
without an external backup or rerun, and regenerated analysis does not restore decisions.
