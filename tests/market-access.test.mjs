import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const read = (path) => readFile(new URL(path, import.meta.url), "utf8");

const middlewareOf = (source, name) =>
  source.match(new RegExp(`export const ${name} = createServerFn\\(\\{ method: "\\w+" \\}\\)\\s+\\.middleware\\(\\[([^\\]]*)\\]\\)`))?.[1];

const RULES = {
  "../src/lib/forward.functions.ts": {
    runForwardNow: 'requireMarket("crypto")',
    getForwardDashboard: 'requireMarket("crypto")',
    getForwardReport: 'requireMarket("crypto")',
    getForwardLog: 'requireMarket("crypto")',
    runForwardTrendNow: 'requireMarket("crypto")',
    getTrendDashboard: 'requireMarket("crypto")',
    getRunnerJob: "requireMarketAccess",
    getMachineCapabilities: "requireMarketAccess",
  },
  "../src/lib/analysis.functions.ts": { runPythonAnalysis: 'requireMarket("crypto")' },
  "../src/lib/monitor.functions.ts": { runMyMonitorCheck: 'requireMarket("crypto")' },
  "../src/lib/market-movement.functions.ts": {
    getMarketMovementCurrentState: 'requireMarket("crypto")',
    getSlowMovementState: 'requireMarket("crypto")',
  },
  "../src/lib/operational.functions.ts": { getOperationalState: 'requireMarket("crypto")' },
  "../src/lib/alert-charts/view.functions.ts": {
    getAlertChartImage: 'requireMarket("crypto")',
    deleteAlertWithChart: 'requireMarket("crypto")',
  },
  "../src/lib/forex-market.functions.ts": { getForexQuotes: 'requireMarket("forex")' },
  "../src/lib/runner/status.functions.ts": { getRunnerStatus: "requireMarketAccess" },
};

test("market-specific server functions require that market's label; shared ones any label", async () => {
  for (const [path, functions] of Object.entries(RULES)) {
    const source = await read(path);
    for (const [name, expected] of Object.entries(functions)) {
      assert.equal(middlewareOf(source, name), expected, `${name} in ${path}`);
    }
  }
});
