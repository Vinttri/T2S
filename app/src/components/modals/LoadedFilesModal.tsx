import { useEffect, useState, useCallback } from "react";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { BookOpen, FileText, Download, Trash2, Loader2 } from "lucide-react";
import { DatabaseService } from "@/services/database";
import { useToast } from "@/components/ui/use-toast";

interface LoadedFilesModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  graphId?: string;
  graphName?: string;
  onChanged?: () => void; // called after a delete so the toolbar lamps refresh
}

interface LoadedState {
  knowledge: { present: boolean; chars: number };
  documents: Array<{ source: string; chars: number }>;
}

function downloadText(filename: string, content: string) {
  const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

const LoadedFilesModal = ({ open, onOpenChange, graphId, graphName, onChanged }: LoadedFilesModalProps) => {
  const [state, setState] = useState<LoadedState | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState<string | null>(null); // which item is being downloaded/deleted
  const { toast } = useToast();

  const refresh = useCallback(async () => {
    if (!graphId) return;
    setLoading(true);
    try {
      setState(await DatabaseService.getLoadedFiles(graphId));
    } catch (error) {
      toast({
        title: "Could not load file list",
        description: error instanceof Error ? error.message : "Failed to list loaded files",
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  }, [graphId, toast]);

  useEffect(() => {
    if (open) refresh();
  }, [open, refresh]);

  const handleDownloadKnowledge = async () => {
    if (!graphId) return;
    setBusy("knowledge");
    try {
      const content = await DatabaseService.getKnowledge(graphId);
      downloadText(`${graphName || graphId}-knowledge.md`, content);
    } catch (error) {
      toast({ title: "Download failed", description: error instanceof Error ? error.message : "", variant: "destructive" });
    } finally {
      setBusy(null);
    }
  };

  const handleDeleteKnowledge = async () => {
    if (!graphId) return;
    if (!window.confirm("Delete the business knowledge for this database? This removes it from the graph.")) return;
    setBusy("knowledge");
    try {
      await DatabaseService.updateKnowledge(graphId, ""); // empty payload clears
      toast({ title: "Knowledge deleted", variant: "success", description: "Business knowledge removed from the graph." });
      await refresh();
      onChanged?.();
    } catch (error) {
      toast({ title: "Delete failed", description: error instanceof Error ? error.message : "", variant: "destructive" });
    } finally {
      setBusy(null);
    }
  };

  const handleDownloadDoc = async (source: string) => {
    if (!graphId) return;
    setBusy(source);
    try {
      const content = await DatabaseService.getDocument(graphId, source);
      downloadText(source.includes(".") ? source : `${source}.txt`, content);
    } catch (error) {
      toast({ title: "Download failed", description: error instanceof Error ? error.message : "", variant: "destructive" });
    } finally {
      setBusy(null);
    }
  };

  const handleDeleteDoc = async (source: string) => {
    if (!graphId) return;
    if (!window.confirm(`Delete "${source}"? This removes its chunks from the graph.`)) return;
    setBusy(source);
    try {
      await DatabaseService.deleteDocument(graphId, source);
      toast({ title: "File deleted", variant: "success", description: `${source} removed from the graph.` });
      await refresh();
      onChanged?.();
    } catch (error) {
      toast({ title: "Delete failed", description: error instanceof Error ? error.message : "", variant: "destructive" });
    } finally {
      setBusy(null);
    }
  };

  const hasKnowledge = !!state?.knowledge?.present;
  const documents = state?.documents || [];
  const isEmpty = !loading && !hasKnowledge && documents.length === 0;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[600px] max-h-[90vh] overflow-y-auto bg-card border-border">
        <DialogHeader>
          <DialogTitle className="text-xl font-semibold text-card-foreground">
            Loaded files{graphName ? ` — ${graphName}` : ""}
          </DialogTitle>
          <DialogDescription className="text-sm text-muted-foreground">
            Business knowledge and uploaded schema documents indexed into this database.
            Download to inspect, or delete to remove from the graph.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-5 mt-4">
          {loading && (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="w-4 h-4 animate-spin" /> Loading…
            </div>
          )}

          {isEmpty && (
            <div className="text-sm text-muted-foreground py-4 text-center">
              Nothing loaded yet. Use “Load Knowledge” or “Upload Schema”.
            </div>
          )}

          {hasKnowledge && (
            <div>
              <h3 className="text-sm font-semibold text-foreground mb-2">Business knowledge</h3>
              <div className="flex items-center justify-between gap-3 rounded-md border border-border bg-muted/20 px-3 py-2">
                <div className="flex items-center gap-2 min-w-0">
                  <BookOpen className="w-4 h-4 text-primary flex-shrink-0" />
                  <span className="text-sm truncate">Business knowledge</span>
                  <span className="text-xs text-muted-foreground flex-shrink-0">
                    {(state?.knowledge.chars || 0).toLocaleString()} chars
                  </span>
                </div>
                <div className="flex items-center gap-1 flex-shrink-0">
                  <Button variant="ghost" size="icon" className="h-8 w-8" title="Download"
                    disabled={busy === "knowledge"} onClick={handleDownloadKnowledge} data-testid="download-knowledge">
                    {busy === "knowledge" ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
                  </Button>
                  <Button variant="ghost" size="icon" className="h-8 w-8 hover:bg-red-600 hover:text-white" title="Delete"
                    disabled={busy === "knowledge"} onClick={handleDeleteKnowledge} data-testid="delete-knowledge">
                    <Trash2 className="w-4 h-4" />
                  </Button>
                </div>
              </div>
            </div>
          )}

          {documents.length > 0 && (
            <div>
              <h3 className="text-sm font-semibold text-foreground mb-2">
                Uploaded schema files ({documents.length})
              </h3>
              <div className="space-y-2">
                {documents.map((doc) => (
                  <div key={doc.source}
                    className="flex items-center justify-between gap-3 rounded-md border border-border bg-muted/20 px-3 py-2">
                    <div className="flex items-center gap-2 min-w-0">
                      <FileText className="w-4 h-4 text-primary flex-shrink-0" />
                      <span className="text-sm truncate" title={doc.source}>{doc.source}</span>
                      <span className="text-xs text-muted-foreground flex-shrink-0">
                        {doc.chars.toLocaleString()} chars
                      </span>
                    </div>
                    <div className="flex items-center gap-1 flex-shrink-0">
                      <Button variant="ghost" size="icon" className="h-8 w-8" title="Download"
                        disabled={busy === doc.source} onClick={() => handleDownloadDoc(doc.source)}
                        data-testid={`download-doc-${doc.source}`}>
                        {busy === doc.source ? <Loader2 className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
                      </Button>
                      <Button variant="ghost" size="icon" className="h-8 w-8 hover:bg-red-600 hover:text-white" title="Delete"
                        disabled={busy === doc.source} onClick={() => handleDeleteDoc(doc.source)}
                        data-testid={`delete-doc-${doc.source}`}>
                        <Trash2 className="w-4 h-4" />
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        <div className="flex justify-end mt-6">
          <Button variant="outline" onClick={() => onOpenChange(false)} className="border-border">
            Close
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
};

export default LoadedFilesModal;
