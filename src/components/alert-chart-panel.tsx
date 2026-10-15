import { useQuery } from "@tanstack/react-query";
import { Download } from "lucide-react";

import { Hint } from "@/components/hint";
import { getAlertChartImage } from "@/lib/alert-charts/view.functions";
import {
  alertFacts,
  chartViewModel,
  type AlertForChart,
  type ChartMeta,
} from "@/lib/alert-charts/hosted";
import { useNow } from "@/lib/use-now";
import { sourceLabel } from "@/lib/labels";
import { cn } from "@/lib/utils";

const local = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "—");
const price = (value: number | null) => (value === null ? "—" : `${value.toLocaleString()} USDT`);

/**
 * The setup chart of one alert, shown in the expanded row. The signed URL is requested only while this panel is
 * mounted (the row was expanded) and only when the chart is stored; it is not polled. The image box reserves its
 * height before the picture loads, so opening a row never shifts the table.
 */
export function AlertChartPanel({ alert, meta, id }: { alert: AlertForChart; meta: ChartMeta | undefined; id: string }) {
  const now = useNow(60_000);
  const model = chartViewModel(alert, meta, now);
  const facts = alertFacts(alert);
  const image = useQuery({
    queryKey: ["alert-chart-image", alert.id, meta?.rendered_at ?? null],
    queryFn: () => getAlertChartImage({ data: { alertId: alert.id } }),
    enabled: model.loadImage,
    staleTime: 5 * 60_000, // signed URLs live 10 minutes
    retry: 1,
  });
  const signed = image.data?.ok ? image.data : null;
  const missing = model.loadImage && (image.isError || (image.data && !image.data.ok));

  return (
    <div id={id} className="grid gap-4 p-4 lg:grid-cols-[minmax(0,1fr)_16rem]" role="region" aria-label={`Setup chart for ${alert.symbol}`}>
      <div>
        {model.loadImage ? (
          <div className="relative aspect-[11/8] w-full overflow-hidden rounded-md border border-border bg-muted/30">
            {signed ? (
              <img
                src={signed.viewUrl}
                alt={`Setup chart of the ${alert.symbol} alert: candles, EMA 20/50, volume, RSI, baseline and alert price`}
                className="absolute inset-0 size-full object-contain"
              />
            ) : (
              <p className="absolute inset-0 grid place-items-center p-4 text-center text-sm text-muted-foreground" role="status">
                {missing
                  ? "The chart file could not be loaded. It may have been removed; try reopening the row."
                  : "Loading chart…"}
              </p>
            )}
          </div>
        ) : (
          <div className="grid min-h-32 place-items-center rounded-md border border-dashed border-border p-4 text-center" role="status">
            <div>
              <p className="text-sm font-medium">{model.title}</p>
              <p className="mt-1 text-xs text-muted-foreground">{model.body}</p>
            </div>
          </div>
        )}
        {signed ? (
          <a
            href={signed.downloadUrl}
            download
            className="mt-2 inline-flex items-center gap-1 text-xs text-primary underline-offset-4 hover:underline"
          >
            <Download className="size-3.5" aria-hidden />
            Download PNG
          </a>
        ) : null}
      </div>

      <dl className="grid content-start gap-2 text-xs">
        <Fact label="Fired (UTC)">{facts.firedAtUtc}</Fact>
        <Fact label="Fired (your time)">{local(facts.firedAtIso)}</Fact>
        <Fact
          label={<Hint text="The starting price your alert measures from. It is saved, and moves to the alert price after each alert.">Baseline</Hint>}
        >
          {price(facts.baselinePrice)}
          {facts.baselineAtIso ? <span className="block text-muted-foreground">saved {local(facts.baselineAtIso)}</span> : null}
        </Fact>
        <Fact label="Alert price">{price(facts.alertPrice)}</Fact>
        <Fact label="Change">
          <span className={cn(facts.changePct >= 0 ? "text-bull" : "text-bear")}>
            {facts.changePct >= 0 ? "+" : ""}{facts.changePct.toFixed(2)}%
          </span>
        </Fact>
        <Fact label="Threshold">{facts.thresholdPct}%</Fact>
        <Fact label="Rule">
          {facts.rule}
          <span className="block text-muted-foreground">version {facts.ruleVersion}</span>
        </Fact>
        <Fact label="Data source">{sourceLabel(facts.dataSource)}</Fact>
      </dl>
    </div>
  );
}

function Fact({ label, children }: { label: React.ReactNode; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="num font-medium break-words">{children}</dd>
    </div>
  );
}
