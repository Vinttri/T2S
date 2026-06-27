import { useState, useEffect, useRef, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import { Avatar, AvatarImage, AvatarFallback } from "@/components/ui/avatar";
import { ArrowLeft, PanelLeft, Sparkles, Key, Loader2, TestTube2, Cpu, Brain, Database } from "lucide-react";
import { useToast } from "@/components/ui/use-toast";
import { useAuth } from "@/contexts/AuthContext";
import { useDatabase } from "@/contexts/DatabaseContext";
import { databaseService } from "@/services/database";
import { buildApiUrl } from "@/config/api";
import { csrfHeaders } from "@/lib/csrf";
import { CURRENT_COMPLETION_MODELS, CURRENT_SERVER_MODELS } from "@/utils/vendorConfig";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu";
import Sidebar from "@/components/layout/Sidebar";
import SchemaViewer from "@/components/schema";

type RuntimeGroupKey = "completion" | "memory";
type RuntimeModelField =
  | "model"
  | "api_base"
  | "temperature"
  | "max_tokens"
  | "reasoning"
  | "context"
  | "extra_body";

interface RuntimeModelGroup {
  role?: RuntimeGroupKey;
  model: string;
  api_base: string;
  has_api_key?: boolean;
  api_key_mask?: string;
  temperature?: string;
  max_tokens?: string;
  reasoning?: string;
  context?: string;
  extra_body?: string;
}

interface EmbeddingGroup {
  model: string;
  api_base: string;
  dimensions?: string;
  has_api_key?: boolean;
  api_key_mask?: string;
}

interface RuntimeModelSettings {
  completion: RuntimeModelGroup;
  memory: RuntimeModelGroup;
  embedding: EmbeddingGroup;
  settings_writable?: boolean;
}

const DEFAULT_COMPLETION_BASE_URL = "http://host.docker.internal:1234/v1";
const DEFAULT_MODEL_CONTEXT_TOKENS = "16384";
const DISABLE_THINKING_EXTRA_BODY = '{"chat_template_kwargs":{"enable_thinking":false}}';

const REASONING_OFF_VALUES = ["", "off", "none", "false", "0"];
const isReasoningOn = (value?: string) =>
  !REASONING_OFF_VALUES.includes(String(value ?? "").trim().toLowerCase());

const DEFAULT_GROUP = (role: RuntimeGroupKey): RuntimeModelGroup => ({
  role,
  model: role === "completion" ? CURRENT_SERVER_MODELS.completion : CURRENT_SERVER_MODELS.memoryCompletion,
  api_base: DEFAULT_COMPLETION_BASE_URL,
  temperature: role === "completion" ? "0.0" : "0.1",
  max_tokens: "8000",
  reasoning: "off",
  context: DEFAULT_MODEL_CONTEXT_TOKENS,
  extra_body: DISABLE_THINKING_EXTRA_BODY,
});

const DEFAULT_EMBEDDING: EmbeddingGroup = {
  model: "openai/qwen3-embedding",
  api_base: "http://embeddings:7997/v1",
  dimensions: "1024", // native size of the built-in Qwen3-Embedding-0.6B
};

const DEFAULT_RUNTIME_MODELS: RuntimeModelSettings = {
  completion: DEFAULT_GROUP("completion"),
  memory: DEFAULT_GROUP("memory"),
  embedding: { ...DEFAULT_EMBEDDING },
};

const RUNTIME_MODEL_FIELDS: RuntimeModelField[] = [
  "model",
  "api_base",
  "temperature",
  "max_tokens",
  "reasoning",
  "context",
  "extra_body",
];

const normalizeRuntimeGroup = (
  defaults: RuntimeModelGroup,
  data?: Partial<RuntimeModelGroup>,
): RuntimeModelGroup => {
  const merged = { ...defaults, ...(data || {}) };
  return {
    ...merged,
    model: String(merged.model || defaults.model),
    api_base: String(merged.api_base || defaults.api_base),
    temperature: String(merged.temperature ?? defaults.temperature ?? ""),
    max_tokens: String(merged.max_tokens ?? defaults.max_tokens ?? ""),
    reasoning: String(merged.reasoning ?? defaults.reasoning ?? ""),
    context: String(merged.context ?? defaults.context ?? ""),
    extra_body: String(merged.extra_body ?? defaults.extra_body ?? ""),
  };
};

const normalizeRuntimeSettings = (data: Partial<RuntimeModelSettings> = {}): RuntimeModelSettings => ({
  completion: normalizeRuntimeGroup(DEFAULT_RUNTIME_MODELS.completion, data.completion),
  memory: normalizeRuntimeGroup(DEFAULT_RUNTIME_MODELS.memory, data.memory),
  embedding: {
    ...DEFAULT_EMBEDDING,
    ...(data.embedding || {}),
    model: String(data.embedding?.model || DEFAULT_EMBEDDING.model),
    api_base: String(data.embedding?.api_base || DEFAULT_EMBEDDING.api_base),
    dimensions: String(data.embedding?.dimensions ?? ""),
  },
  settings_writable: data.settings_writable,
});

const Settings = () => {
  const navigate = useNavigate();
  const { toast } = useToast();
  const { isAuthenticated, logout, user } = useAuth();
  const { selectedGraph, graphs } = useDatabase();

  const [runtimeModels, setRuntimeModels] = useState<RuntimeModelSettings | null>(null);
  const [runtimeDraft, setRuntimeDraft] = useState<RuntimeModelSettings>(DEFAULT_RUNTIME_MODELS);
  const [runtimeApiKeys, setRuntimeApiKeys] = useState<Record<RuntimeGroupKey, string>>({
    completion: "",
    memory: "",
  });
  const [embeddingApiKey, setEmbeddingApiKey] = useState("");
  const [embeddingDetecting, setEmbeddingDetecting] = useState(false);
  const [isLoadingRuntimeModels, setIsLoadingRuntimeModels] = useState(true);
  const [isSavingRuntimeModels, setIsSavingRuntimeModels] = useState(false);
  const [reindexing, setReindexing] = useState(false);
  const [testingContextRole, setTestingContextRole] = useState<RuntimeGroupKey | null>(null);

  const [rules, setRules] = useState('');
  const [isLoadingRules, setIsLoadingRules] = useState(true);
  const [initialRulesLoaded, setInitialRulesLoaded] = useState(false);
  const loadedRulesRef = useRef<string>('');
  const currentRulesRef = useRef<string>('');
  const currentGraphIdRef = useRef<string | null>(null);
  const initialRulesLoadedRef = useRef<boolean>(false);

  const [sidebarCollapsed, setSidebarCollapsed] = useState(() =>
    typeof window !== 'undefined' ? window.innerWidth < 768 : false
  );
  const [windowWidth, setWindowWidth] = useState(typeof window !== 'undefined' ? window.innerWidth : 1024);
  const [showSchemaViewer, setShowSchemaViewer] = useState(false);
  const [schemaViewerWidth, setSchemaViewerWidth] = useState(() =>
    typeof window !== "undefined" ? Math.floor(window.innerWidth * 0.4) : 0,
  );

  // Per-user memory context, written to the graph. Default OFF.
  const [useMemory, setUseMemory] = useState(() => {
    if (typeof window !== 'undefined') {
      const saved = localStorage.getItem('t2s_use_memory');
      return saved === null ? false : saved === 'true';
    }
    return false;
  });
  // Debug mode — OFF by default (clean, user-friendly UI). When on, the header
  // shows the backend debug panel.
  const [debugMode, setDebugMode] = useState(() =>
    typeof window !== 'undefined' && localStorage.getItem('t2s_debug_mode') === 'true'
  );
  useEffect(() => {
    if (typeof window !== 'undefined') localStorage.setItem('t2s_debug_mode', String(debugMode));
  }, [debugMode]);

  // Load per-user prefs (memory + debug) from the DB on mount; localStorage stays
  // in sync (via the effects above) so the query path keeps reading it.
  useEffect(() => {
    (async () => {
      try {
        const res = await fetch(buildApiUrl("/settings/prefs"), { credentials: "include" });
        if (!res.ok) return;
        const p = await res.json();
        if (typeof p.use_memory === "boolean") setUseMemory(p.use_memory);
        if (typeof p.debug_mode === "boolean") setDebugMode(p.debug_mode);
      } catch {
        /* ignore */
      }
    })();
  }, []);

  // Persist a per-user pref to the DB (memory/debug toggles).
  const savePref = (body: { use_memory?: boolean; debug_mode?: boolean }) => {
    fetch(buildApiUrl("/settings/prefs"), {
      method: "PUT",
      headers: { "Content-Type": "application/json", ...csrfHeaders() },
      credentials: "include",
      body: JSON.stringify(body),
    }).catch(() => { /* ignore */ });
  };

  useEffect(() => {
    const handleResize = () => setWindowWidth(window.innerWidth);
    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);

  useEffect(() => {
    if (windowWidth < 768) setSidebarCollapsed(true);
  }, [windowWidth]);

  const getSidebarWidth = () => {
    if (windowWidth < 768) return sidebarCollapsed ? 0 : 64;
    return 64;
  };
  const sidebarWidth = getSidebarWidth();

  const getMainContentStyles = () => {
    if (windowWidth < 768) {
      return { marginLeft: `${sidebarWidth}px`, width: `calc(100% - ${sidebarWidth}px)` };
    }
    const totalOffset = showSchemaViewer ? schemaViewerWidth + sidebarWidth : sidebarWidth;
    return { marginLeft: `${totalOffset}px`, width: `calc(100% - ${totalOffset}px)` };
  };

  // Rules are bound to the DATABASE (not the user). Switching DB shows that DB's
  // rules (empty if none); returning reloads them. The order of rules is their rank.
  useEffect(() => {
    const loadRules = async () => {
      if (!selectedGraph) {
        setRules('');
        setIsLoadingRules(false);
        setInitialRulesLoaded(false);
        loadedRulesRef.current = '';
        currentGraphIdRef.current = null;
        return;
      }
      currentGraphIdRef.current = selectedGraph.id;
      try {
        setIsLoadingRules(true);
        setInitialRulesLoaded(false);
        const userRules = await databaseService.getUserRules(selectedGraph.id);
        const rulesValue = userRules || '';
        setRules(rulesValue);
        loadedRulesRef.current = rulesValue;
      } catch (error) {
        console.error('Failed to load user rules:', error);
        toast({ title: "Error", description: "Failed to load rules from database", variant: "destructive" });
      } finally {
        setIsLoadingRules(false);
        setInitialRulesLoaded(true);
        initialRulesLoadedRef.current = true;
      }
    };
    loadRules();
    return () => {
      // Flush unsaved edits to the OUTGOING database before loading another
      // graph's rules (and on unmount), so a DB switch never silently drops them.
      const oldGraphId = currentGraphIdRef.current;
      if (oldGraphId && currentRulesRef.current !== loadedRulesRef.current && initialRulesLoadedRef.current) {
        databaseService.updateUserRules(oldGraphId, currentRulesRef.current).catch(
          err => console.error('Failed to flush rules on switch/unmount:', err));
      }
    };
  }, [selectedGraph?.id, toast]);

  useEffect(() => { currentRulesRef.current = rules; }, [rules]);

  useEffect(() => {
    if (typeof window !== 'undefined') localStorage.setItem('t2s_use_memory', String(useMemory));
  }, [useMemory]);

  useEffect(() => {
    const loadRuntimeModels = async () => {
      try {
        setIsLoadingRuntimeModels(true);
        const response = await fetch(buildApiUrl("/settings/runtime-models"), { credentials: "include" });
        if (!response.ok) throw new Error(`Request failed with status ${response.status}`);
        const data = await response.json();
        const loaded = normalizeRuntimeSettings(data);
        setRuntimeModels(loaded);
        setRuntimeDraft(loaded);
      } catch (error) {
        console.error("Failed to load runtime model settings:", error);
        toast({
          title: "Runtime settings unavailable",
          description: error instanceof Error ? error.message : "Failed to load model settings",
          variant: "destructive",
        });
      } finally {
        setIsLoadingRuntimeModels(false);
      }
    };
    loadRuntimeModels();
  }, [toast]);

  const updateRuntimeDraft = (group: RuntimeGroupKey, field: RuntimeModelField, value: string) => {
    setRuntimeDraft((current) => ({ ...current, [group]: { ...current[group], [field]: value } }));
  };

  const updateRuntimeApiKey = (group: RuntimeGroupKey, value: string) => {
    setRuntimeApiKeys((current) => ({ ...current, [group]: value }));
  };

  const setReasoning = (group: RuntimeGroupKey, on: boolean) => {
    setRuntimeDraft((current) => ({
      ...current,
      [group]: {
        ...current[group],
        // "on" => model-default thinking (no disable); "off" => disable thinking.
        reasoning: on ? "on" : "off",
        extra_body: on ? "{}" : DISABLE_THINKING_EXTRA_BODY,
      },
    }));
  };

  const updateEmbeddingDraft = (field: "model" | "api_base" | "dimensions", value: string) => {
    setRuntimeDraft((current) => ({ ...current, embedding: { ...current.embedding, [field]: value } }));
  };

  const handleContextTest = async (group: RuntimeGroupKey) => {
    const roleDraft = runtimeDraft[group];
    const contextTarget = Number(roleDraft.context || DEFAULT_MODEL_CONTEXT_TOKENS);
    try {
      setTestingContextRole(group);
      const response = await fetch(buildApiUrl("/settings/context-test"), {
        method: "POST",
        headers: { "Content-Type": "application/json", ...csrfHeaders() },
        credentials: "include",
        body: JSON.stringify({
          role: group,
          model: roleDraft.model.trim(),
          api_base: roleDraft.api_base.trim(),
          api_key: runtimeApiKeys[group].trim() || undefined,
          max_tokens: roleDraft.max_tokens?.trim() || "8",
          reasoning: roleDraft.reasoning?.trim() || "off",
          extra_body: roleDraft.extra_body?.trim() || undefined,
          max_probe_tokens: String(Math.max(256000, contextTarget || 128000)),
        }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `Request failed with status ${response.status}`);
      const detected = result.detected_context_tokens || result.declared_context_tokens;
      if (detected) updateRuntimeDraft(group, "context", String(detected));
      toast({
        title: "Context test completed",
        variant: "success",
        description: detected ? `${group} context detected as ${detected} tokens` : "Endpoint responded, but context size was not reported",
      });
    } catch (error) {
      console.error("Context test failed:", error);
      toast({
        title: "Context test failed",
        description: error instanceof Error ? error.message : "Failed to test model context",
        variant: "destructive",
      });
    } finally {
      setTestingContextRole(null);
    }
  };

  const detectEmbeddingInfo = async () => {
    try {
      setEmbeddingDetecting(true);
      const res = await fetch(buildApiUrl("/settings/embedding-info"), { credentials: "include" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `Request failed with status ${res.status}`);
      if (typeof data.dimensions === "number") {
        updateEmbeddingDraft("dimensions", String(data.dimensions));
        toast({
          title: "Embedding detected",
          variant: "success",
          description: `Vector size: ${data.dimensions}${data.device ? ` · ${data.device}` : ""}`,
        });
      } else {
        toast({
          title: "Embedding detection",
          variant: "warning",
          description: "Endpoint responded, but no dimension was reported",
        });
      }
    } catch (error) {
      toast({
        title: "Embedding detection failed",
        description: error instanceof Error ? error.message : "Failed to detect embedding size",
        variant: "destructive",
      });
    } finally {
      setEmbeddingDetecting(false);
    }
  };

  const runtimeModelsChanged = Boolean(
    runtimeModels && (
      (["completion"] as RuntimeGroupKey[]).some((group) => (
        RUNTIME_MODEL_FIELDS.some((field) => (
          String(runtimeDraft[group][field] ?? "") !== String(runtimeModels[group][field] ?? "")
        )) || runtimeApiKeys[group].trim()
      )) ||
      embeddingApiKey.trim() ||
      String(runtimeDraft.embedding?.model ?? "") !== String(runtimeModels.embedding?.model ?? "") ||
      String(runtimeDraft.embedding?.api_base ?? "") !== String(runtimeModels.embedding?.api_base ?? "") ||
      String(runtimeDraft.embedding?.dimensions ?? "") !== String(runtimeModels.embedding?.dimensions ?? "")
    )
  );

  const resetRuntimeDraft = () => {
    setRuntimeDraft(runtimeModels || DEFAULT_RUNTIME_MODELS);
    setRuntimeApiKeys({ completion: "", memory: "" });
    setEmbeddingApiKey("");
  };

  const handleSaveRuntimeModels = async () => {
    const required = [
      runtimeDraft.completion.model, runtimeDraft.completion.api_base,
    ];
    if (required.some((value) => !value.trim())) {
      toast({ title: "Missing setting", description: "Model and base URL are required for the completion endpoint", variant: "destructive" });
      return;
    }
    const groupPayload = (group: RuntimeGroupKey) => ({
      model: runtimeDraft[group].model.trim(),
      api_base: runtimeDraft[group].api_base.trim(),
      api_key: runtimeApiKeys[group].trim() || undefined,
      temperature: runtimeDraft[group].temperature?.trim() || undefined,
      max_tokens: runtimeDraft[group].max_tokens?.trim() || undefined,
      reasoning: (runtimeDraft[group].reasoning?.trim() ?? "") || "off",
      context: runtimeDraft[group].context?.trim() || undefined,
      extra_body: runtimeDraft[group].extra_body?.trim() || "",
    });
    try {
      setIsSavingRuntimeModels(true);
      const response = await fetch(buildApiUrl("/settings/runtime-models"), {
        method: "POST",
        headers: { "Content-Type": "application/json", ...csrfHeaders() },
        credentials: "include",
        body: JSON.stringify({
          completion: groupPayload("completion"),
          memory: groupPayload("memory"),
          embedding: {
            model: runtimeDraft.embedding.model.trim(),
            api_base: runtimeDraft.embedding.api_base.trim(),
            api_key: embeddingApiKey.trim() || undefined,
            dimensions: runtimeDraft.embedding.dimensions?.trim() || undefined,
          },
          restart: false,
        }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `Request failed with status ${response.status}`);
      const saved = normalizeRuntimeSettings({
        ...result,
        completion: { ...runtimeDraft.completion, ...(result.completion || {}) },
        memory: { ...runtimeDraft.memory, ...(result.memory || {}) },
        embedding: { ...runtimeDraft.embedding, ...(result.embedding || {}) },
      });
      setRuntimeModels(saved);
      setRuntimeDraft(saved);
      setRuntimeApiKeys({ completion: "", memory: "" });
      setEmbeddingApiKey("");
      toast({ title: "Saved", variant: "success", description: "Endpoint settings saved and applied." });
    } catch (error) {
      console.error("Failed to save runtime model settings:", error);
      toast({ title: "Save failed", description: error instanceof Error ? error.message : "Failed to save model settings", variant: "destructive" });
    } finally {
      setIsSavingRuntimeModels(false);
    }
  };

  // The embedding model/dimensions differ from what is saved — i.e. from what
  // the databases were indexed with — so a full re-index is required.
  const embeddingChanged =
    String(runtimeDraft.embedding?.model ?? "") !== String(runtimeModels?.embedding?.model ?? "") ||
    String(runtimeDraft.embedding?.dimensions ?? "") !== String(runtimeModels?.embedding?.dimensions ?? "");

  // Save the embedding change (so the new model is live), then re-index every
  // non-demo database: each is dropped, re-pulled from its source DB, and its
  // knowledge / rules / uploaded schemas are re-embedded with the new model.
  const handleReindexAll = async () => {
    setReindexing(true);
    try {
      if (runtimeModelsChanged) {
        await handleSaveRuntimeModels();
      }
      const targets = (graphs || []).filter((g) => !g.id.startsWith("general_"));
      if (targets.length === 0) {
        toast({ title: "Nothing to re-index", description: "No re-indexable databases found." });
        return;
      }
      let ok = 0;
      let failed = 0;
      for (const g of targets) {
        try {
          const res = await fetch(buildApiUrl(`/graphs/${encodeURIComponent(g.id)}/refresh`), {
            method: "POST",
            headers: { "Content-Type": "application/json", ...csrfHeaders() },
            credentials: "include",
          });
          if (!res.ok) { failed += 1; continue; }
          const reader = res.body?.getReader();
          if (reader) { for (;;) { const { done } = await reader.read(); if (done) break; } }
          ok += 1;
        } catch {
          failed += 1;
        }
      }
      toast({
        title: failed ? "Re-index finished with errors" : "Re-index complete",
        description: `${ok} database(s) re-indexed with the new embedding model${failed ? `, ${failed} failed` : ""}.`,
        variant: failed ? "warning" : "success",
      });
    } finally {
      setReindexing(false);
    }
  };

  const handleLogout = async () => {
    try {
      await logout();
      toast({ title: "Logged Out", description: "You have been successfully logged out" });
      window.location.reload();
    } catch (error) {
      toast({ title: "Logout Failed", description: error instanceof Error ? error.message : "Failed to logout", variant: "destructive" });
    }
  };

  const rulesChanged = initialRulesLoaded && rules !== loadedRulesRef.current;

  const handleApplyRules = async () => {
    if (!selectedGraph?.id) return;
    try {
      await databaseService.updateUserRules(selectedGraph.id, rules);
      loadedRulesRef.current = rules;
      toast({ title: "Rules applied", variant: "success", description: `Rules saved to ${selectedGraph.name} and indexed.` });
    } catch (error) {
      toast({ title: "Error", description: error instanceof Error ? error.message : "Failed to save rules", variant: "destructive" });
    }
  };

  const handleBackClick = async () => {
    const graphId = currentGraphIdRef.current;
    if (graphId && currentRulesRef.current !== loadedRulesRef.current && initialRulesLoaded) {
      try {
        await databaseService.updateUserRules(graphId, currentRulesRef.current);
        loadedRulesRef.current = currentRulesRef.current;
      } catch (error) {
        toast({ title: "Error", description: error instanceof Error ? error.message : "Failed to save rules", variant: "destructive" });
      }
    }
    navigate('/');
  };

  const Hint = ({ children }: { children: ReactNode }) => (
    <p className="text-[11px] leading-snug text-muted-foreground mt-1">{children}</p>
  );

  const renderEndpoint = (group: RuntimeGroupKey, title: string, subtitle: string) => {
    const draft = runtimeDraft[group];
    const disabled = isLoadingRuntimeModels || isSavingRuntimeModels;
    return (
      <div className="rounded-lg border border-border bg-muted/20 p-4 space-y-4">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            {group === "completion" ? <Brain className="h-4 w-4 text-primary" /> : <Cpu className="h-4 w-4 text-primary" />}
            <h3 className="text-base font-semibold text-foreground">{title}</h3>
          </div>
          <Badge variant="outline" className="font-mono text-[11px]">
            {draft.has_api_key ? "key set" : "no key"}
          </Badge>
        </div>
        <p className="text-[11px] text-muted-foreground -mt-1">{subtitle}</p>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="space-y-1">
            <Label htmlFor={`rt-${group}-model`} className="text-sm font-medium">Model</Label>
            <Input id={`rt-${group}-model`} type="text" list="rt-completion-models"
              value={draft.model} onChange={(e) => updateRuntimeDraft(group, "model", e.target.value)}
              className="h-11 font-mono text-sm bg-muted border-border" disabled={disabled} />
            <Hint>Model name sent to the OpenAI-compatible endpoint (e.g. the name your LM Studio / vLLM serves).</Hint>
          </div>
          <div className="space-y-1">
            <Label htmlFor={`rt-${group}-base`} className="text-sm font-medium">Base URL</Label>
            <Input id={`rt-${group}-base`} type="text"
              value={draft.api_base} onChange={(e) => updateRuntimeDraft(group, "api_base", e.target.value)}
              className="h-11 font-mono text-sm bg-muted border-border" disabled={disabled} />
            <Hint>This endpoint's OpenAI-compatible base URL. Use host.docker.internal to reach a model on your Mac.</Hint>
          </div>
        </div>

        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <Key className="h-4 w-4 text-muted-foreground" />
            <Label htmlFor={`rt-${group}-key`} className="text-sm font-medium">API Key</Label>
          </div>
          <Input id={`rt-${group}-key`} type="password"
            placeholder={draft.api_key_mask || "leave blank to keep current key"}
            value={runtimeApiKeys[group]} onChange={(e) => updateRuntimeApiKey(group, e.target.value)}
            className="h-11 font-mono text-sm bg-muted border-border" disabled={disabled} />
          <Hint>Stored only on this machine's data volume. Leave blank to keep the existing key.</Hint>
        </div>

        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          <div className="space-y-1">
            <Label htmlFor={`rt-${group}-temp`} className="text-sm font-medium">Temperature</Label>
            <Input id={`rt-${group}-temp`} type="number" min="0" max="2" step="0.1"
              value={draft.temperature || ""} onChange={(e) => updateRuntimeDraft(group, "temperature", e.target.value)}
              className="h-11 font-mono text-sm bg-muted border-border" disabled={disabled} />
            <Hint>Sampling randomness. Keep low (0–0.2) for deterministic SQL.</Hint>
          </div>
          <div className="space-y-1">
            <Label htmlFor={`rt-${group}-max`} className="text-sm font-medium">Max Tokens</Label>
            <Input id={`rt-${group}-max`} type="number" min="1" step="1"
              value={draft.max_tokens || ""} onChange={(e) => updateRuntimeDraft(group, "max_tokens", e.target.value)}
              className="h-11 font-mono text-sm bg-muted border-border" disabled={disabled} />
            <Hint>Max tokens the model may generate per call.</Hint>
          </div>
          <div className="space-y-1">
            <Label htmlFor={`rt-${group}-ctx`} className="text-sm font-medium">Context Tokens</Label>
            <div className="flex gap-2">
              <Input id={`rt-${group}-ctx`} type="number" min="1" step="1"
                value={draft.context || ""} onChange={(e) => updateRuntimeDraft(group, "context", e.target.value)}
                className="h-11 font-mono text-sm bg-muted border-border" disabled={disabled || testingContextRole === group} />
              <Button type="button" variant="outline" size="icon"
                onClick={() => handleContextTest(group)}
                disabled={disabled || testingContextRole !== null || !draft.model.trim() || !draft.api_base.trim()}
                title="Probe the endpoint's context window and fill this field" className="h-11 w-11 shrink-0">
                {testingContextRole === group ? <Loader2 className="h-4 w-4 animate-spin" /> : <TestTube2 className="h-4 w-4" />}
              </Button>
            </div>
            <Hint>The model's context window. Click the probe to auto-detect it.</Hint>
          </div>
        </div>

        <div className="flex items-center justify-between gap-4 rounded-md border border-border bg-background/40 p-3">
          <div className="min-w-0">
            <div className="text-sm font-medium">Reasoning</div>
            <Hint>On lets the model "think" before answering. Off disables thinking (faster, fits small context windows).</Hint>
          </div>
          <Switch checked={isReasoningOn(draft.reasoning)} onCheckedChange={(c) => setReasoning(group, c)} disabled={disabled} />
        </div>
      </div>
    );
  };

  return (
    <div className="flex h-screen bg-background text-foreground overflow-hidden">
      <Sidebar
        onSchemaClick={() => setShowSchemaViewer(!showSchemaViewer)}
        isSchemaOpen={showSchemaViewer}
        isCollapsed={sidebarCollapsed}
        onToggleCollapse={() => setSidebarCollapsed(!sidebarCollapsed)}
        onSettingsClick={() => {}}
      />
      <SchemaViewer
        isOpen={showSchemaViewer}
        onClose={() => setShowSchemaViewer(false)}
        onWidthChange={setSchemaViewerWidth}
        sidebarWidth={sidebarWidth}
      />

      <div className="flex flex-1 flex-col transition-all duration-300" style={getMainContentStyles()}>
        {/* Top Header Bar */}
        <header className="border-b border-border bg-background">
          <div className="hidden md:flex items-center justify-between p-6">
            <div className="flex items-center gap-4">
              <img src="/img/t2s-emblem-256.png" alt="T2S" className="h-16 w-16" />
              <div className="leading-tight">
                <div className="text-4xl font-bold tracking-tight"><span className="text-primary">T2</span><span style={{ color: '#4FA84E' }}>S</span></div>
                <p className="text-xs text-muted-foreground mt-0.5">Text-to-SQL AI Platform <span className="font-mono opacity-70">v1.0</span></p>
              </div>
            </div>
            <div className="flex items-center gap-2">
              {selectedGraph ? (
                <Badge variant="default" className="bg-green-600 hover:bg-green-700">Connected: {selectedGraph.name}</Badge>
              ) : (
                <Badge variant="secondary" className="bg-yellow-600 hover:bg-yellow-700">No Database Selected</Badge>
              )}
              {isAuthenticated && (
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button variant="ghost" className="p-0 h-auto rounded-full hover:opacity-80 transition-opacity" title={user?.name || user?.email}>
                      <Avatar className="h-10 w-10 border-2 border-primary">
                        <AvatarImage src={user?.picture} alt={user?.name || user?.email} />
                        <AvatarFallback className="bg-primary text-white font-medium">
                          {(user?.name || user?.email || 'U').charAt(0).toUpperCase()}
                        </AvatarFallback>
                      </Avatar>
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent className="bg-card border-border text-foreground" align="end">
                    <div className="px-3 py-2 border-b border-border">
                      <p className="text-sm font-medium text-foreground">{user?.name}</p>
                      <p className="text-xs text-muted-foreground">{user?.email}</p>
                    </div>
                    <DropdownMenuItem className="hover:!bg-muted cursor-pointer">API Tokens</DropdownMenuItem>
                    <DropdownMenuSeparator className="bg-border" />
                    <DropdownMenuItem className="hover:!bg-muted cursor-pointer" onClick={handleLogout}>Logout</DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              )}
            </div>
          </div>

          {/* Mobile Header */}
          <div className="md:hidden p-4 space-y-3">
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                {sidebarCollapsed && (
                  <button onClick={() => setSidebarCollapsed(false)}
                    className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary text-white hover:bg-primary/90 transition-all">
                    <PanelLeft className="h-5 w-5" />
                  </button>
                )}
                <span className="flex items-center gap-2"><img src="/img/t2s-emblem-256.png" alt="T2S" className="h-10 w-10" /><span className="text-2xl font-bold"><span className="text-primary">T2</span><span style={{ color: '#4FA84E' }}>S</span></span></span>
              </div>
            </div>
            <div className="flex justify-center">
              {selectedGraph ? (
                <Badge variant="default" className="bg-green-600 hover:bg-green-700 text-xs">Connected: {selectedGraph.name}</Badge>
              ) : (
                <Badge variant="secondary" className="bg-yellow-600 hover:bg-yellow-700 text-xs">No Database Selected</Badge>
              )}
            </div>
          </div>
        </header>

        {/* Settings sub-header */}
        <div className="border-b border-border bg-card px-4 md:px-6 py-4">
          <div className="flex items-center gap-4">
            <Button variant="ghost" size="sm" onClick={handleBackClick} className="text-muted-foreground hover:text-foreground hover:bg-muted">
              <ArrowLeft className="w-4 h-4 mr-2" /> Back
            </Button>
            <div>
              <h1 className="text-xl md:text-2xl font-semibold">Settings</h1>
              <p className="text-xs md:text-sm text-muted-foreground mt-1">
                Configure the model endpoints, memory, and database rules. The retrieval &amp; SQL algorithm is fixed.
              </p>
            </div>
          </div>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-auto p-4 md:p-6">
          <div className="max-w-4xl mx-auto space-y-6">
            {/* Model Endpoints */}
            <div className="bg-card border border-border rounded-lg p-5 space-y-4">
              <div className="flex items-center gap-2">
                <Sparkles className="h-5 w-5 text-primary" />
                <h2 className="text-lg font-semibold text-foreground">Model Endpoints</h2>
              </div>
              <p className="text-sm text-muted-foreground">
                The OpenAI-compatible completion model used by T2S. Memory uses this same model automatically. Changes are saved locally and applied immediately.
              </p>

              {renderEndpoint("completion", "Completion", "Generates SQL, analyses the question, and summarises conversation memory — the one model T2S uses.")}

              {/* Embedding — built-in by default, but editable */}
              <div className="rounded-lg border border-border bg-muted/20 p-4 space-y-4">
                <div className="flex items-center gap-2">
                  <Database className="h-4 w-4 text-primary" />
                  <h3 className="text-base font-semibold text-foreground">Embedding</h3>
                  <Badge variant="outline" className="font-mono text-[11px]">
                    {runtimeDraft.embedding?.has_api_key ? "key set" : "built-in default"}
                  </Badge>
                </div>
                <p className="text-[11px] text-muted-foreground -mt-1">
                  Defaults to the built-in <span className="font-mono">embeddings</span> container (Qwen3-Embedding, auto GPU/CPU).
                  You can point it at any OpenAI-compatible embedding endpoint — changing the model or dimensions requires re-indexing the database.
                </p>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <div className="space-y-1">
                    <Label htmlFor="rt-embed-model" className="text-sm font-medium">Model</Label>
                    <Input id="rt-embed-model" type="text" value={runtimeDraft.embedding?.model || ""}
                      onChange={(e) => updateEmbeddingDraft("model", e.target.value)}
                      className="h-11 font-mono text-sm bg-muted border-border"
                      disabled={isLoadingRuntimeModels || isSavingRuntimeModels} />
                    <Hint>Embedding model the endpoint serves (built-in: openai/qwen3-embedding).</Hint>
                  </div>
                  <div className="space-y-1">
                    <Label htmlFor="rt-embed-base" className="text-sm font-medium">Base URL</Label>
                    <Input id="rt-embed-base" type="text" value={runtimeDraft.embedding?.api_base || ""}
                      onChange={(e) => updateEmbeddingDraft("api_base", e.target.value)}
                      className="h-11 font-mono text-sm bg-muted border-border"
                      disabled={isLoadingRuntimeModels || isSavingRuntimeModels} />
                    <Hint>OpenAI-compatible base URL (built-in: http://embeddings:7997/v1).</Hint>
                  </div>
                </div>
                <div className="space-y-1">
                  <div className="flex items-center gap-2">
                    <Key className="h-4 w-4 text-muted-foreground" />
                    <Label htmlFor="rt-embed-key" className="text-sm font-medium">API Key</Label>
                  </div>
                  <Input id="rt-embed-key" type="password"
                    placeholder={runtimeDraft.embedding?.api_key_mask || "leave blank — built-in needs no key"}
                    value={embeddingApiKey} onChange={(e) => setEmbeddingApiKey(e.target.value)}
                    className="h-11 font-mono text-sm bg-muted border-border"
                    disabled={isLoadingRuntimeModels || isSavingRuntimeModels} />
                  <Hint>Only needed for an external embedding endpoint.</Hint>
                </div>
                <div className="space-y-1">
                  <Label htmlFor="rt-embed-dim" className="text-sm font-medium">Dimensions</Label>
                  <div className="flex gap-2">
                    <Input id="rt-embed-dim" type="number" min="1" step="1"
                      value={runtimeDraft.embedding?.dimensions || ""}
                      onChange={(e) => updateEmbeddingDraft("dimensions", e.target.value)}
                      className="h-11 font-mono text-sm bg-muted border-border"
                      disabled={isLoadingRuntimeModels || isSavingRuntimeModels || embeddingDetecting} />
                    <Button type="button" variant="outline" size="icon"
                      onClick={detectEmbeddingInfo}
                      disabled={isLoadingRuntimeModels || isSavingRuntimeModels || embeddingDetecting}
                      title="Detect the embedding endpoint's vector size and fill this field"
                      className="h-11 w-11 shrink-0">
                      {embeddingDetecting ? <Loader2 className="h-4 w-4 animate-spin" /> : <TestTube2 className="h-4 w-4" />}
                    </Button>
                  </div>
                  <Hint>Vector size — click to auto-detect, or enter your own. Changing it requires re-indexing.</Hint>
                </div>
                {(embeddingChanged || reindexing) && (
                  <div
                    className="rounded-md border border-red-500/50 bg-red-500/10 px-3 py-2 text-sm font-medium text-red-300 space-y-2"
                    data-testid="embedding-reindex-warning"
                  >
                    <div>
                      ⚠ Full re-index required. The embedding model/dimensions must match how each
                      database was indexed — changing them invalidates every stored vector (they were
                      built in the current model's space). Re-indexing re-embeds every database
                      (schema + knowledge + rules + uploaded schemas) with the new model.
                    </div>
                    <Button
                      onClick={handleReindexAll}
                      disabled={reindexing}
                      className="border-0 bg-red-600 text-white hover:bg-red-700 gap-1.5"
                      data-testid="embedding-reindex-now"
                    >
                      {reindexing ? <Loader2 className="h-4 w-4 animate-spin" /> : <Database className="h-4 w-4" />}
                      {reindexing ? "Re-indexing…" : "Save & re-index now"}
                    </Button>
                  </div>
                )}
              </div>

              <datalist id="rt-completion-models">
                {CURRENT_COMPLETION_MODELS.map((model) => (<option key={model} value={model} />))}
              </datalist>

              <div className="flex justify-end gap-2 pt-1">
                <Button variant="outline" onClick={resetRuntimeDraft}
                  disabled={isLoadingRuntimeModels || isSavingRuntimeModels || !runtimeModelsChanged} className="border-border">
                  Reset
                </Button>
                <Button onClick={handleSaveRuntimeModels}
                  disabled={isLoadingRuntimeModels || isSavingRuntimeModels || !runtimeModelsChanged ||
                    !runtimeDraft.completion.model.trim() || !runtimeDraft.completion.api_base.trim()}
                  className="bg-primary hover:bg-primary/90 text-white min-w-[120px]">
                  {isSavingRuntimeModels && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                  {isSavingRuntimeModels ? "Saving..." : "Save"}
                </Button>
              </div>
            </div>

            {/* Memory Context */}
            <div className="bg-card border border-border rounded-lg p-4">
              <div className="flex items-center justify-between">
                <div className="space-y-1">
                  <Label htmlFor="use-memory" className="text-base font-semibold text-foreground">Use Memory Context</Label>
                  <p className="text-sm text-muted-foreground">
                    Per-user context written to the graph — remembers your previous interactions and preferences across queries. On by default.
                  </p>
                </div>
                <Switch id="use-memory" checked={useMemory} onCheckedChange={(v) => { setUseMemory(v); savePref({ use_memory: v }); }} className="data-[state=checked]:bg-primary" />
              </div>
            </div>

            {/* Debug mode */}
            <div className="bg-card border border-border rounded-lg p-4">
              <div className="flex items-center justify-between">
                <div className="space-y-1">
                  <Label htmlFor="debug-mode" className="text-base font-semibold text-foreground">Debug mode</Label>
                  <p className="text-sm text-muted-foreground">
                    Show the backend debug panel in the header. Off keeps a clean, user-friendly interface.
                  </p>
                </div>
                <Switch id="debug-mode" checked={debugMode} onCheckedChange={(v) => { setDebugMode(v); savePref({ debug_mode: v }); }} className="data-[state=checked]:bg-primary" />
              </div>
            </div>

            {/* User Rules — bound to the database */}
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <Label htmlFor="rules" className="text-base font-semibold text-foreground">User Rules &amp; Specifications</Label>
                <span className="text-xs text-muted-foreground">{rules.length} characters</span>
              </div>
              <p className="text-[11px] text-muted-foreground -mt-1">
                Rules are bound to <span className="font-medium">{selectedGraph?.name || "the selected database"}</span>, not to your account — switching databases shows that database's rules. One rule per line; their order is their rank (highest first). Click Apply to save them into the graph (and index them for retrieval).
              </p>
              <Textarea
                id="rules"
                placeholder={`Example rules (order = rank):
- Always use ISO date format (YYYY-MM-DD)
- Limit results to 100 rows unless specified
- Prefer explicit column names over SELECT *`}
                value={rules}
                onChange={(e) => setRules(e.target.value)}
                disabled={isLoadingRules || !selectedGraph}
                className="min-h-[360px] bg-muted border-border text-foreground placeholder:text-muted-foreground focus:border-primary focus:ring-ring font-mono text-sm"
              />
              <div className="flex justify-end gap-2">
                <Button variant="outline" size="sm" onClick={() => setRules('')}
                  disabled={!selectedGraph}
                  className="border-border text-muted-foreground hover:text-foreground hover:bg-muted">
                  Clear
                </Button>
                <Button size="sm" onClick={handleApplyRules} disabled={!selectedGraph || !rulesChanged}
                  className="bg-primary hover:bg-primary/90 text-white">
                  Apply
                </Button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};

export default Settings;
