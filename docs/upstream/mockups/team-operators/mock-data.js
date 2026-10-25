// Sample teams for the team operators mock. Values follow contracts/teams.schema.json:
// v1_operators is a list (may be empty, case variants listed explicitly), v2_operator is one
// value or null before migration.
window.MOCK_TEAMS = [
  {
    key: "v1",
    label: "v1 only, registry changed since the run",
    name: "Legacy Batch Jobs",
    phase: "phase_1",
    week: "12 Oct – 19 Oct 2026",
    registry: "2026-10-01.1",
    effective: "1 Oct 2026",
    v1: ["batch-team", "BATCH_JOBS", "batch_svc"],
    v2: null,
    // The selected week's run froze an older registry version.
    run: { registry: "2026-09-15.2", v1: ["batch-team", "BATCH_JOBS"], v2: null },
    shared: [],
  },
  {
    key: "v2",
    label: "v2 only",
    name: "Payments Core",
    phase: "phase_2",
    week: "12 Oct – 19 Oct 2026",
    registry: "2026-10-01.1",
    effective: "1 Oct 2026",
    v1: [],
    v2: "payments-core",
    run: null,
    shared: [],
  },
  {
    key: "many",
    label: "Migrating, many operators",
    name: "Checkout Platform",
    phase: "phase_1",
    week: "12 Oct – 19 Oct 2026",
    registry: "2026-10-01.1",
    effective: "1 Oct 2026",
    v1: [
      "checkout", "Checkout", "CHECKOUT", "checkout-api", "checkout_api", "cart-svc",
      "cart_service", "basket", "promo-engine", "promo_engine_legacy", "platform-ops", "pay-gw-bridge",
    ],
    v2: "checkout-platform",
    run: null,
    shared: [{ operator: "platform-ops", teams: ["Fulfilment"] }],
  },
  {
    key: "none",
    label: "No operators yet",
    name: "Search Relevance",
    phase: "phase_0",
    week: "12 Oct – 19 Oct 2026",
    registry: "2026-10-01.1",
    effective: "1 Oct 2026",
    v1: [],
    v2: null,
    run: null,
    shared: [],
  },
];

window.mockEscape = (text) =>
  String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// The mock-only bar that switches between sample teams; not part of the proposed UI.
window.mountMockBar = (render) => {
  const bar = document.getElementById("mock-bar");
  const params = new URLSearchParams(location.search);
  let current = params.get("team") || window.MOCK_TEAMS[0].key;
  const draw = () => {
    bar.innerHTML =
      '<span class="mock-tag">Mock</span><span>Sample team:</span>' +
      window.MOCK_TEAMS.map(
        (t) =>
          `<button type="button" data-key="${t.key}"${t.key === current ? ' aria-pressed="true"' : ""}>${window.mockEscape(t.label)}</button>`,
      ).join("");
    render(window.MOCK_TEAMS.find((t) => t.key === current) || window.MOCK_TEAMS[0]);
  };
  bar.addEventListener("click", (event) => {
    const key = event.target.closest("button")?.dataset.key;
    if (!key) return;
    current = key;
    history.replaceState(null, "", `?team=${key}`);
    draw();
  });
  draw();
};
