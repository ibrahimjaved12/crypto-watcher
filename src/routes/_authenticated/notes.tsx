import { createFileRoute } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Trash2 } from "lucide-react";
import { toast } from "sonner";

import { EmptyState, ErrorNotice, PageHeader } from "@/components/presentation";
import { Badge } from "@/components/ui/badge";
import { issueSummary, pairLabel } from "@/lib/presentation/labels";
import { AppShell } from "@/components/app-shell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
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
    onError: (e: Error) => toast.error(issueSummary(e.message)),
  });

  const remove = useMutation({
    mutationFn: deleteNote,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["notes"] }),
  });

  return (
    <AppShell>
      <PageHeader
        title="Notes"
        description="Keep your observations, questions and market decisions in one place. Add a pair to give each note context."
      />
      <div className="mt-6 grid items-start gap-6 lg:grid-cols-[360px_minmax(0,1fr)]">
        <form
          className="panel space-y-4 p-5 sm:p-6"
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate();
          }}
        >
          <div>
            <p className="eyebrow mb-2">Capture an observation</p>
            <h2 className="text-xl font-semibold">New note</h2>
          </div>
          {save.error && <ErrorNotice error={save.error} />}
          <div className="space-y-2">
            <Label htmlFor="note-title">Title</Label>
            <Input
              id="note-title"
              placeholder="What caught your attention?"
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
              rows={7}
              placeholder="Record your reasoning, levels to watch, or what you want to revisit."
              value={body}
              onChange={(e) => setBody(e.target.value)}
            />
          </div>
          <Button type="submit" className="w-full" disabled={save.isPending}>
            {save.isPending ? "Saving note…" : "Save note"}
          </Button>
        </form>

        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-lg font-semibold">Your observations</h2>
            <span className="text-xs text-muted-foreground">{notes.data?.length ?? 0} notes</span>
          </div>
          {notes.isPending && (
            <p role="status" className="panel p-6 text-sm text-muted-foreground">
              Loading your notes…
            </p>
          )}
          {notes.error && <ErrorNotice title="Notes unavailable." error={notes.error} />}
          {remove.error && <ErrorNotice title="Note could not be deleted." error={remove.error} />}
          {(notes.data ?? []).map((n) => (
            <article key={n.id} className="panel p-5">
              <header className="flex items-start gap-3">
                <div>
                  <h2 className="break-words text-lg font-semibold">{n.title}</h2>
                  <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                    {n.symbol && <Badge variant="secondary">{pairLabel(n.symbol)}</Badge>}
                    <time dateTime={n.updated_at}>{new Date(n.updated_at).toLocaleString()}</time>
                  </div>
                </div>
                <Button
                  variant="ghost"
                  size="icon"
                  className="ml-auto"
                  aria-label={`Delete note: ${n.title}`}
                  disabled={remove.isPending}
                  onClick={() => remove.mutate(n.id)}
                >
                  <Trash2 className="size-4" aria-hidden />
                </Button>
              </header>
              {n.body ? (
                <p className="mt-4 whitespace-pre-wrap break-words text-sm leading-relaxed text-muted-foreground">
                  {n.body}
                </p>
              ) : null}
            </article>
          ))}
          {notes.data?.length === 0 ? (
            <EmptyState title="Make room for your own perspective">
              Write your first observation. A short note today can help you understand a decision
              later.
            </EmptyState>
          ) : null}
        </div>
      </div>
    </AppShell>
  );
}
