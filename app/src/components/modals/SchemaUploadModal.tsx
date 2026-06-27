import { useEffect, useMemo, useState } from "react";
import { Upload, Loader2, CheckCircle2, XCircle } from "lucide-react";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useDatabase } from "@/contexts/DatabaseContext";
import { useToast } from "@/components/ui/use-toast";
import { DatabaseService } from "@/services/database";

interface SchemaUploadModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onUploadingChange?: (uploading: boolean) => void; // drives the toolbar spinner
  onUploaded?: () => void; // called after a successful upload (refresh the lamp)
}

interface UploadStep {
  message: string;
  status: "pending" | "success" | "error";
}

// Any document the enrichment agent can read. The base schema always comes from
// the database itself, so uploads only ENRICH an existing graph — they never
// create or replace one.
const ACCEPTED_FILES =
  ".md,.markdown,.txt,.text,.csv,.tsv,.json,.yml,.yaml,.pdf,.docx,.doc,.xlsx,.xls,.html,.htm,.rst";

const SchemaUploadModal = ({ open, onOpenChange, onUploadingChange, onUploaded }: SchemaUploadModalProps) => {
  const [files, setFiles] = useState<File[]>([]);
  const [targetGraphId, setTargetGraphId] = useState("");
  const [isUploading, setIsUploading] = useState(false);
  const [uploadSteps, setUploadSteps] = useState<UploadStep[]>([]);
  const { graphs, selectedGraph, refreshGraphs } = useDatabase();
  const { toast } = useToast();

  const graphOptions = useMemo(
    () => graphs.filter((graph) => graph.id && graph.name),
    [graphs],
  );

  useEffect(() => {
    if (!open) return;
    refreshGraphs();
    const activeGraph = selectedGraph || graphOptions[0];
    setTargetGraphId(activeGraph ? activeGraph.id : "");
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  const addStep = (message: string, status: UploadStep["status"] = "pending") => {
    setUploadSteps(prev => {
      if (status === "pending" && prev.length > 0 && prev[prev.length - 1].status === "pending") {
        const updated = [...prev];
        updated[updated.length - 1] = { ...updated[updated.length - 1], status: "success" };
        return [...updated, { message, status }];
      }
      if (status !== "pending" && prev.length > 0 && prev[prev.length - 1].status === "pending") {
        const updated = [...prev];
        updated[updated.length - 1] = { ...updated[updated.length - 1], status };
        return updated;
      }
      return [...prev, { message, status }];
    });
  };

  const reset = () => {
    setFiles([]);
    setUploadSteps([]);
  };

  const handleUpload = async () => {
    const graph = graphOptions.find((option) => option.id === targetGraphId) || selectedGraph;
    if (!files.length || !graph) {
      toast({
        title: "Missing Information",
        description: graph ? "Select one or more documents to upload." : "Select a target database first.",
        variant: "destructive",
      });
      return;
    }

    setIsUploading(true);
    onUploadingChange?.(true);
    setUploadSteps([]);
    try {
      await DatabaseService.enrichDatabase({
        database: graph.name || graph.id,
        files,
        onStep: (message, status) => addStep(message, status),
      });

      await refreshGraphs();
      onUploaded?.();
      toast({
        title: "Schema Enriched",
        variant: "success",
        description: `${graph.name} enriched from ${files.length} document${files.length === 1 ? "" : "s"}.`,
      });
      setTimeout(() => {
        reset();
        onOpenChange(false);
      }, 900);
    } catch (error) {
      addStep(error instanceof Error ? error.message : "Enrichment failed", "error");
      toast({
        title: "Enrichment Failed",
        description: error instanceof Error ? error.message : "Failed to enrich schema",
        variant: "destructive",
      });
    } finally {
      setIsUploading(false);
      onUploadingChange?.(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[560px] max-h-[90vh] overflow-y-auto bg-card border-border">
        <DialogHeader>
          <DialogTitle className="text-xl font-semibold text-card-foreground">
            Upload Schema
          </DialogTitle>
          <DialogDescription className="text-sm text-muted-foreground">
            Enrich the selected database from your documents. An agent refines table and
            column descriptions, adds relationships, and indexes the text for retrieval.
            The base schema always comes from the database — this never creates or replaces it.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 mt-4">
          <div className="space-y-2">
            <Label htmlFor="schema-files" className="text-sm font-medium">Documents</Label>
            <Input
              id="schema-files"
              type="file"
              multiple
              accept={ACCEPTED_FILES}
              onChange={(event) => setFiles(Array.from(event.target.files || []))}
              disabled={isUploading}
              className="bg-muted border-border focus-visible:ring-ring"
              data-testid="schema-upload-input"
            />
            <p className="text-xs text-muted-foreground">
              Any document — data dictionary, glossary, ER notes, CSV, PDF, DOCX, XLSX, JSON, YAML, Markdown.
            </p>
          </div>

          <div className="space-y-2">
            <Label htmlFor="schema-target-graph" className="text-sm font-medium">Target Database</Label>
            <Select
              value={targetGraphId}
              onValueChange={setTargetGraphId}
              disabled={isUploading || graphOptions.length === 0}
            >
              <SelectTrigger
                id="schema-target-graph"
                className="bg-muted border-border focus:ring-ring"
                data-testid="schema-target-graph-select"
              >
                <SelectValue placeholder={graphOptions.length ? "Select database" : "No database available"} />
              </SelectTrigger>
              <SelectContent>
                {graphOptions.map((graph) => (
                  <SelectItem key={graph.id} value={graph.id}>
                    {graph.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">
              Documents enrich this database's existing graph.
            </p>
          </div>

          {uploadSteps.length > 0 && (
            <div className="space-y-2 max-h-[180px] overflow-y-auto border border-border rounded-md p-3 bg-muted/30">
              {uploadSteps.map((step, index) => (
                <div key={index} className="flex items-start gap-2 text-sm">
                  {step.status === "pending" && <Loader2 className="w-4 h-4 mt-0.5 text-blue-500 animate-spin flex-shrink-0" />}
                  {step.status === "success" && <CheckCircle2 className="w-4 h-4 mt-0.5 text-green-500 flex-shrink-0" />}
                  {step.status === "error" && <XCircle className="w-4 h-4 mt-0.5 text-red-500 flex-shrink-0" />}
                  <span className={step.status === "error" ? "text-red-400" : "text-card-foreground"}>
                    {step.message}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="flex justify-end gap-3 mt-6">
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={isUploading}
            className="hover:bg-primary/20 hover:text-foreground"
          >
            Cancel
          </Button>
          <Button
            onClick={handleUpload}
            disabled={isUploading || files.length === 0 || !targetGraphId}
            className="bg-primary hover:bg-primary/90"
            data-testid="upload-schema-submit-btn"
          >
            {isUploading ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : <Upload className="w-4 h-4 mr-2" />}
            Upload
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
};

export default SchemaUploadModal;
