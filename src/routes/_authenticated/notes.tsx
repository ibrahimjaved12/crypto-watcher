import { createFileRoute } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { NotebookPen, Trash2 } from "lucide-react";
import { toast } from "sonner";

import { AppShell } from "@/components/app-shell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { EmptyState, PageHeader, TimeAgo } from "@/components/plain";
import { relativeTime } from "@/lib/labels";
import { createNote, deleteNote, fetchNotes, fetchWatchlist } from "@/lib/db";

export const Route = createFileRoute("/_authenticated/notes")({
  head: () => ({
    meta: [
      { title: "Notes — Crypto Watch" },
      {
        name: "description",
        content: "Keep your market analysis and observations attached to the pairs you follow.",
      },
      { property: "og:title", content: "Notes — Crypto Watch" },
      { property: "og:description", content: "Your private crypto analysis notes." },
      { property: "og:type", content: "website" },
      { name: "twitter:card", content: "summary_large_image" },
    ],
  }),
  component: NotesPage,
});

function NotesPage() {
  const queryClient = useQueryClient();
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [symbol, setSymbol] = useState("");

  const notes = useQuery({ queryKey: ["notes"], queryFn: fetchNotes });
  const watchlist = useQuery({ queryKey: ["watchlist"], queryFn: fetchWatchlist });

  const save = useMutation({
    mutationFn: () => createNote({ title, body, symbol: symbol || null }),
    onSuccess: () => {
      setTitle("");
      setBody("");
      setSymbol("");
      queryClient.invalidateQueries({ queryKey: ["notes"] });
    },
    onError: (e: Error) => toast.error(e.message),
  });

  const remove = useMutation({
    mutationFn: deleteNote,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["notes"] }),
  });

  return (
    <AppShell>
      <PageHeader
        eyebrow="Logbook"
        title="Notes"
        subtitle="Your own trade ideas and observations, optionally tied to a pair. Stored with your account, never in the browser."
      />

      <div className="mt-5 grid gap-4 lg:grid-cols-[minmax(0,380px)_1fr]">
        <form
          className="panel space-y-3 p-5"
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate();
          }}
        >
          <h2 className="text-base font-semibold">New note</h2>
          <div className="space-y-2">
            <Label htmlFor="note-title">Title</Label>
            <Input
              id="note-title"
              required
              value={title}
              onChange={(e) => setTitle(e.target.value)}
            />
          </div>
          <div className="space-y-2">
            <Label htmlFor="note-symbol">Pair (optional)</Label>
            <Input
              id="note-symbol"
              list="watchlist-symbols"
              placeholder="BTCUSDT"
              value={symbol}
              onChange={(e) => setSymbol(e.target.value.toUpperCase())}
            />
            <datalist id="watchlist-symbols">
              {(watchlist.data ?? []).map((w) => (
                <option key={w.id} value={w.symbol} />
              ))}
            </datalist>
          </div>
          <div className="space-y-2">
            <Label htmlFor="note-body">Note</Label>
            <Textarea
              id="note-body"
              rows={6}
              value={body}
              onChange={(e) => setBody(e.target.value)}
            />
          </div>
          <Button type="submit" className="w-full" disabled={save.isPending}>
            {save.isPending ? "Saving…" : "Save note"}
          </Button>
        </form>

        <div className="space-y-3">
          {(notes.data ?? []).map((n) => (
            <article key={n.id} className="panel p-5">
              <header className="flex items-start gap-3">
                <div>
                  <h2 className="font-semibold">{n.title}</h2>
                  <p className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                    {n.symbol ? (
                      <span className="num rounded-full border border-primary/40 bg-primary/10 px-2 py-0.5 text-primary">
                        {n.symbol.replace(/USDT$/, "")}
                      </span>
                    ) : null}
                    <TimeAgo at={n.updated_at} text={`Edited ${relativeTime(n.updated_at)}`} />
                  </p>
                </div>
                <Button
                  variant="ghost"
                  size="icon"
                  className="ml-auto"
                  title="Delete note"
                  aria-label="Delete note"
                  onClick={() => remove.mutate(n.id)}
                >
                  <Trash2 className="size-4" aria-hidden />
                </Button>
              </header>
              {n.body ? (
                <p className="mt-3 whitespace-pre-wrap break-words text-sm text-muted-foreground">{n.body}</p>
              ) : null}
            </article>
          ))}
          {notes.isPending ? (
            <p className="text-sm text-muted-foreground">Loading notes…</p>
          ) : null}
          {notes.data?.length === 0 ? (
            <EmptyState
              icon={NotebookPen}
              title="No notes yet"
              body="Write your first note on the left: a trade idea, a level to watch, or why you skipped a setup. It appears here straight away."
            />
          ) : null}
        </div>
      </div>
    </AppShell>
  );
}
