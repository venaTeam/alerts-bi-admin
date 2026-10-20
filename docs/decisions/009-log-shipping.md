# Log shipping to Elasticsearch — accepted 2026-10-20

After the console log format (decision 008), the product owner asked on 2026-10-20 to also send
the logs to an Elasticsearch index. Offered the cluster's own log forwarding, a Fluent Bit
sidecar per pod, or one Fluent Bit in the namespace that the applications send to, they chose
the last: one collector instead of a sidecar in every pod, Job and CronJob. They asked for three
indices, runs, admin and portal, readable and searchable, with a run findable by its id, a
team's runs by team, and an alert by its application and key field. Asked where console actions
on a run belong, they chose admin. Asked how alerts should appear, they chose every model group
of every run, with the group's alerts inside its record. They left Elasticsearch configuration
(templates, retention, accounts) to whoever deploys it, and asked that the repository hold every
file and setting an agent needs to deploy the collector on OpenShift. See design section "Log
shipping to Elasticsearch" for the authoritative behavior.

Consequences accepted with the decision: the logs now carry the application, key field and model
outcome of each alert sent to the model, but never its document or the model's justification;
records are dropped, and the console says so, when the collector is unreachable; records buffered
on the collector are lost if its pod is deleted; and the JSON document of `LOG_FORMAT=json`
changes shape before any release used it.

Admin owns the collector Template, its configuration and runbook, shared 0.1.10 and operations
0.2.9; the portal vendors shared 0.1.10. Merge design, admin and portal together after the
console log format change; deploy only with separate authorization.
