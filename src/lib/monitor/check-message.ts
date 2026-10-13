import { summarizeReasons } from "../labels";

export type AlertCheckResult = {
  status: "success" | "partial" | "failed" | "skipped";
  alertsCreated: number;
  symbolsChecked: number;
  taChecks: number;
  taFailures: number;
  error: string | null;
};

export type AlertCheckMessage = {
  kind: "success" | "warning" | "error";
  title: string;
  description?: string;
};

export function describeAlertCheck(result: AlertCheckResult): AlertCheckMessage {
  const description = summarizeReasons(result.error).items.join("; ");
  const details = description ? { description } : {};

  switch (result.status) {
    case "failed":
      return {
        kind: "error",
        title: "Alert check failed. No analysis completed.",
        ...details,
      };
    case "partial":
      return {
        kind: "warning",
        title:
          result.taFailures > 0
            ? `${result.taFailures} of ${result.taChecks} technical-analysis checks failed. ${result.alertsCreated} new alert(s); 0 new alerts is not an all-clear.`
            : `Alert check partly completed. ${result.alertsCreated} new alert(s).`,
        ...details,
      };
    case "success":
      return {
        kind: "success",
        title:
          `Alert check completed: ${result.symbolsChecked} pairs checked, ${result.alertsCreated} new alert(s).` +
          (result.taChecks > 0 ? ` All ${result.taChecks} technical-analysis checks ran.` : ""),
      };
    case "skipped":
      return {
        kind: "warning",
        title: "Alert check skipped.",
        ...details,
      };
  }
}
