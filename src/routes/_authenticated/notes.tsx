import { createFileRoute } from "@tanstack/react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Trash2 } from "lucide-react";
import { toast } from "sonner";

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
    onError: (e: Error) => toast.error(e.message),
  });

  const remove = useMutation({
    mutationFn: deleteNote,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["notes"] }),
  });

  return (
    <AppShell>
      <h1 className="text-2xl font-semibold">Notes</h1>
      <p className="text-sm text-muted-foreground">
        Analysis you want to keep. Stored with your account, never in the browser.
      </p>

      <div className="mt-5 grid gap-4 lg:grid-cols-[380px_1fr]">
        <form
          className="panel space-y-3 p-5"
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate();
          }}
        >
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
            Save note
          </Button>
        </form>

        <div className="space-y-3">
          {(notes.data ?? []).map((n) => (
            <article key={n.id} className="panel p-5">
              <header className="flex items-start gap-3">
                <div>
                  <h2 className="font-semibold">{n.title}</h2>
                  <p className="num text-xs text-muted-foreground">
                    {n.symbol ? `${n.symbol} · ` : ""}
                    {new Date(n.updated_at).toLocaleString()}
                  </p>
                </div>
                <Button
                  variant="ghost"
                  size="icon"
                  className="ml-auto"
                  aria-label="Delete note"
                  onClick={() => remove.mutate(n.id)}
                >
                  <Trash2 className="size-4" aria-hidden />
                </Button>
              </header>
              {n.body ? (
                <p className="mt-3 whitespace-pre-wrap text-sm text-muted-foreground">{n.body}</p>
              ) : null}
            </article>
          ))}
          {notes.data?.length === 0 ? (
            <p className="panel p-6 text-sm text-muted-foreground">No notes yet.</p>
          ) : null}
        </div>
      </div>
    </AppShell>
  );
}
