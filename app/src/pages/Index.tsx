import { useState, useRef, useEffect, useCallback } from "react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Avatar, AvatarImage, AvatarFallback } from "@/components/ui/avatar";
import { Trash2, RefreshCw, PanelLeft, BookOpen, MessageSquarePlus, FileUp } from "lucide-react";
import Sidebar from "@/components/layout/Sidebar";
import ChatInterface from "@/components/chat/ChatInterface";
import LoginModal from "@/components/modals/LoginModal";
import DatabaseModal from "@/components/modals/DatabaseModal";
import SchemaUploadModal from "@/components/modals/SchemaUploadModal";
import LoadedFilesModal from "@/components/modals/LoadedFilesModal";
import DebugPanel from "@/components/layout/DebugPanel";
import DeleteDatabaseModal from "@/components/modals/DeleteDatabaseModal";
import TokensModal from "@/components/modals/TokensModal";
import SchemaViewer from "@/components/schema";
import LoadingSpinner from "@/components/ui/loading-spinner";
import { useAuth } from "@/contexts/AuthContext";
import { useDatabase } from "@/contexts/DatabaseContext";
import { useChat } from "@/contexts/ChatContext";
import { DatabaseService } from "@/services/database";
import { useToast } from "@/components/ui/use-toast";
import { csrfHeaders } from "@/lib/csrf";
import { renderKnowledgeFileContent } from "@/utils/knowledge";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  DropdownMenuSeparator,
} from "@/components/ui/dropdown-menu";

type KnowledgeStatus = 'unknown' | 'empty' | 'loaded';

const Index = () => {
  const { isAuthenticated, isLoading: authLoading, logout, user } = useAuth();
  const { selectedGraph, graphs, selectGraph } = useDatabase();
  const { resetChat, messages } = useChat();
  const { toast } = useToast();

  // "New Session": wipe the conversation so the next question starts from
  // scratch (the backend re-derives the JSON fresh — nothing is carried over).
  const handleNewSession = () => {
    if (messages.length > 0 &&
        !window.confirm("Start a new session? This clears the current conversation.")) {
      return;
    }
    resetChat();
    toast({ title: "New session started", description: "The conversation was cleared." });
  };
  const [showDatabaseModal, setShowDatabaseModal] = useState(false);
  const [showSchemaUploadModal, setShowSchemaUploadModal] = useState(false);
  const [showLoginModal, setShowLoginModal] = useState(false);
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const [showSchemaViewer, setShowSchemaViewer] = useState(false);
  const [showTokensModal, setShowTokensModal] = useState(false);
  // userRulesSpec is now fetched from the graph database per query
  const [useMemory] = useState(() => {
    // Per-user memory context written to the graph — OFF by default.
    if (typeof window !== 'undefined') {
      const saved = localStorage.getItem('t2s_use_memory');
      return saved === null ? false : saved === 'true';
    }
    return false;
  });
  // Debug mode (off by default = clean UI). When on, the header shows the
  // backend debug panel. Toggled on the Settings page.
  const [debugMode] = useState(() =>
    typeof window !== 'undefined' && localStorage.getItem('t2s_debug_mode') === 'true'
  );
  // Rules are bound to the database and managed on the Settings page; always
  // request them (the backend returns empty when a database has none).
  const useRulesFromDatabase = true;
  const [isRefreshingSchema, setIsRefreshingSchema] = useState(false);
  const [isChatProcessing, setIsChatProcessing] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() =>
    typeof window !== 'undefined' ? window.innerWidth < 768 : false
  );
  const [schemaViewerWidth, setSchemaViewerWidth] = useState(() =>
    typeof window !== "undefined" ? Math.floor(window.innerWidth * 0.4) : 0,
  );
  const [databaseToDelete, setDatabaseToDelete] = useState<{ id: string; name: string; isDemo: boolean } | null>(null);
  const [windowWidth, setWindowWidth] = useState(typeof window !== 'undefined' ? window.innerWidth : 1024);
  const [knowledgeStatusByGraphId, setKnowledgeStatusByGraphId] = useState<Record<string, KnowledgeStatus>>({});
  const [knowledgeLoadingByGraphId, setKnowledgeLoadingByGraphId] = useState<Record<string, boolean>>({});
  const knowledgeFileInputRef = useRef<HTMLInputElement>(null);
  const [showLoadedFilesModal, setShowLoadedFilesModal] = useState(false);
  const [schemaDocCountByGraphId, setSchemaDocCountByGraphId] = useState<Record<string, number>>({});
  const [isSchemaUploading, setIsSchemaUploading] = useState(false);
  const selectedGraphId = selectedGraph?.id ?? null;
  const selectedKnowledgeStatus = selectedGraphId
    ? knowledgeStatusByGraphId[selectedGraphId] ?? 'unknown'
    : 'unknown';
  const knowledgeLoaded = selectedKnowledgeStatus === 'loaded';
  const isKnowledgeLoading = selectedGraphId ? Boolean(knowledgeLoadingByGraphId[selectedGraphId]) : false;
  const schemaLoaded = selectedGraphId ? (schemaDocCountByGraphId[selectedGraphId] ?? 0) > 0 : false;

  // Refresh the count of uploaded schema files for the current graph (drives the
  // Upload Schema lamp + the Loaded-files modal).
  const refreshLoadedFiles = useCallback(async (graphId?: string | null) => {
    const id = graphId ?? selectedGraphId;
    if (!id) return;
    try {
      const data = await DatabaseService.getLoadedFiles(id);
      setSchemaDocCountByGraphId(prev => ({ ...prev, [id]: data.documents.length }));
    } catch { /* ignore transient errors */ }
  }, [selectedGraphId]);

  useEffect(() => {
    if (selectedGraphId) refreshLoadedFiles(selectedGraphId);
  }, [selectedGraphId, refreshLoadedFiles]);

  // Handle window resize to update layout
  useEffect(() => {
    const handleResize = () => {
      setWindowWidth(window.innerWidth);
    };

    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);

  // Auto-collapse sidebar when switching to mobile view
  useEffect(() => {
    const isMobile = windowWidth < 768;
    if (isMobile) {
      setSidebarCollapsed(true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [windowWidth]); // Only run when windowWidth changes, not on manual toggle

  // Calculate sidebar width based on collapsed state
  // On desktop: sidebar is always visible (64px), on mobile: can be collapsed (0px)
  const getSidebarWidth = () => {
    const isMobile = windowWidth < 768;
    if (isMobile) {
      return sidebarCollapsed ? 0 : 64;
    }
    return 64; // Always visible on desktop
  };
  
  const sidebarWidth = getSidebarWidth();
  
  // Calculate main content margin and width
  // On mobile: ignore schema viewer (it's an overlay), only account for sidebar
  // On desktop: account for both sidebar and schema viewer
  const getMainContentStyles = () => {
    const isMobile = windowWidth < 768;

    if (isMobile) {
      return {
        marginLeft: `${sidebarWidth}px`,
        width: `calc(100% - ${sidebarWidth}px)`
      };
    }

    // Desktop
    const totalOffset = showSchemaViewer ? schemaViewerWidth + sidebarWidth : sidebarWidth;
    return {
      marginLeft: `${totalOffset}px`,
      width: `calc(100% - ${totalOffset}px)`
    };
  };


  // Track persisted DB-specific knowledge per graph, not globally.
  useEffect(() => {
    const loadKnowledgeStatus = async () => {
      if (!selectedGraphId || selectedKnowledgeStatus !== 'unknown' || isKnowledgeLoading) {
        return;
      }

      const graphId = selectedGraphId;
      setKnowledgeLoadingByGraphId(prev => ({ ...prev, [graphId]: true }));

      try {
        const knowledge = await DatabaseService.getKnowledge(graphId);
        const status: KnowledgeStatus = knowledge.trim().length > 0 ? 'loaded' : 'empty';
        setKnowledgeStatusByGraphId(prev => {
          if ((prev[graphId] ?? 'unknown') !== 'unknown') {
            return prev;
          }
          return { ...prev, [graphId]: status };
        });
      } finally {
        setKnowledgeLoadingByGraphId(prev => ({ ...prev, [graphId]: false }));
      }
    };

    loadKnowledgeStatus();
  }, [selectedGraphId, selectedKnowledgeStatus, isKnowledgeLoading]);

  // Show login modal when not authenticated after loading completes
  useEffect(() => {
    // Only auto-open the login modal once per user/session to avoid locking
    // the SPA when the backend is down or in demo mode. Allow users to
    // dismiss it and remember that choice in sessionStorage.
    if (!authLoading && !isAuthenticated) {
      const dismissed = sessionStorage.getItem('loginModalDismissed');
      if (!dismissed) {
        setShowLoginModal(true);
      }
    }
  }, [authLoading, isAuthenticated]);

  const handleConnectDatabase = () => {
    if (isRefreshingSchema || isChatProcessing) return;
    setShowDatabaseModal(true);
  };

  const handleUploadSchema = () => {
    if (isRefreshingSchema || isChatProcessing) return;
    setShowSchemaUploadModal(true);
  };

  const handleLoadKnowledge = () => {
    if (!selectedGraph) {
      toast({
        title: "No Database Selected",
        description: "Please select a database before loading knowledge",
        variant: "destructive",
      });
      return;
    }

    if (isRefreshingSchema || isChatProcessing || isKnowledgeLoading) return;
    knowledgeFileInputRef.current?.click();
  };

  const handleKnowledgeFileSelect = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    const graph = selectedGraph;
    if (!file || !graph) return;

    try {
      setKnowledgeLoadingByGraphId(prev => ({ ...prev, [graph.id]: true }));
      const content = await file.text();
      const knowledge = renderKnowledgeFileContent(graph.name || graph.id, file.name, content);

      await DatabaseService.enrichDatabase({
        database: graph.name || graph.id,
        knowledge,
      });
      setKnowledgeStatusByGraphId(prev => ({ ...prev, [graph.id]: 'loaded' }));

      toast({
        title: "Knowledge Loaded",
        variant: "success",
        description: `${file.name} added to ${graph.name} — indexed for retrieval and used to enrich the schema.`,
      });
    } catch (error) {
      toast({
        title: "Knowledge Load Failed",
        description: error instanceof Error ? error.message : "Failed to load knowledge",
        variant: "destructive",
      });
    } finally {
      setKnowledgeLoadingByGraphId(prev => graph ? ({ ...prev, [graph.id]: false }) : prev);
      if (knowledgeFileInputRef.current) {
        knowledgeFileInputRef.current.value = '';
      }
    }
  };

  const handleClearKnowledge = async () => {
    const graph = selectedGraph;
    if (!graph || isRefreshingSchema || isChatProcessing || isKnowledgeLoading) return;

    try {
      setKnowledgeLoadingByGraphId(prev => ({ ...prev, [graph.id]: true }));
      await DatabaseService.updateKnowledge(graph.id, '');
      setKnowledgeStatusByGraphId(prev => ({ ...prev, [graph.id]: 'empty' }));

      toast({
        title: "Knowledge Cleared",
        description: `Knowledge removed for ${graph.name}`,
      });
    } catch (error) {
      toast({
        title: "Knowledge Clear Failed",
        description: error instanceof Error ? error.message : "Failed to clear knowledge",
        variant: "destructive",
      });
    } finally {
      setKnowledgeLoadingByGraphId(prev => ({ ...prev, [graph.id]: false }));
    }
  };

  const handleDeleteGraph = async (graphId: string, graphName: string, event: React.MouseEvent) => {
    event.stopPropagation(); // Prevent dropdown from closing/selecting
    
    // Check if this is a demo database
    const isDemo = graphId.startsWith('general_');
    
    if (isRefreshingSchema) return;
    // Show the delete confirmation modal
    setDatabaseToDelete({ id: graphId, name: graphName, isDemo });
    setShowDeleteModal(true);
  };

  const confirmDeleteGraph = async () => {
    if (!databaseToDelete) return;

    try {
      await DatabaseService.deleteGraph(databaseToDelete.id);
      setKnowledgeStatusByGraphId(prev => {
        const next = { ...prev };
        delete next[databaseToDelete.id];
        return next;
      });
      setKnowledgeLoadingByGraphId(prev => {
        const next = { ...prev };
        delete next[databaseToDelete.id];
        return next;
      });

      toast({
        title: "Database Deleted",
        description: `Successfully deleted "${databaseToDelete.name}"`,
      });

      // Close modal before refresh
      setShowDeleteModal(false);
      setDatabaseToDelete(null);

      // Refresh the graphs list (can be replaced with a context refresh later)
      window.location.reload();
    } catch (error) {
      toast({
        title: "Delete Failed",
        description: error instanceof Error ? error.message : "Failed to delete database",
        variant: "destructive",
      });
    }
  };

  const handleLogout = async () => {
    try {
      await logout();
      toast({
        title: "Logged Out",
        description: "You have been successfully logged out",
      });
      // Refresh to reset state
      window.location.reload();
    } catch (error) {
      toast({
        title: "Logout Failed",
        description: error instanceof Error ? error.message : "Failed to logout",
        variant: "destructive",
      });
    }
  };

  const handleRefreshSchema = async () => {
    if (!selectedGraph) {
      toast({
        title: "No Database Selected",
        description: "Please select a database first",
        variant: "destructive",
      });
      return;
    }

    if (isChatProcessing) {
      toast({
        title: "Chat is Processing",
        description: "Please wait for the current query to complete",
        variant: "destructive",
      });
      return;
    }

    try {
      setIsRefreshingSchema(true);
      const response = await fetch(`/graphs/${selectedGraph.id}/refresh`, {
        method: 'POST',
        headers: {
          ...csrfHeaders(),
        },
        credentials: 'include',
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({ error: 'Failed to refresh schema' }));
        throw new Error(errorData.error || `Server error: ${response.status}`);
      }

      // Process streaming response
      const reader = response.body?.getReader();
      if (!reader) {
        throw new Error('No response body');
      }

      const decoder = new TextDecoder();
      let buffer = '';
      let hasError = false;
      const delimiter = '|||FALKORDB_MESSAGE_BOUNDARY|||';

      while (true) {
        const { done, value } = await reader.read();

        if (done) break;

        const chunk = decoder.decode(value, { stream: true });
        buffer += chunk;

        // Process complete messages
        const parts = buffer.split(delimiter);
        buffer = parts.pop() || ''; // Keep incomplete part in buffer

        for (const part of parts) {
          const trimmed = part.trim();
          if (!trimmed) continue;

          try {
            const message = JSON.parse(trimmed);
            if (message.type === 'error') {
              hasError = true;
              throw new Error(message.message || 'Schema refresh failed');
            }
          } catch (e) {
            if (e instanceof SyntaxError) {
              console.error('Failed to parse message:', trimmed);
            } else {
              throw e;
            }
          }
        }
      }

      if (hasError) {
        return; // Error already thrown and caught
      }

      toast({
        title: "Schema Refreshed",
        variant: "success",
        description: "Database schema refreshed successfully!",
      });

      // Reload to show updated schema
      window.location.reload();
    } catch (error) {
      console.error('Refresh error:', error);
      toast({
        title: "Refresh Failed",
        description: error instanceof Error ? error.message : "Failed to refresh schema",
        variant: "destructive",
      });
    }
    finally {
      setIsRefreshingSchema(false);
    }
  };

  return (
    <div className="flex h-screen bg-background overflow-hidden">
      <input
        ref={knowledgeFileInputRef}
        type="file"
        accept=".jsonl,.json,.md,.txt"
        onChange={handleKnowledgeFileSelect}
        style={{ display: 'none' }}
        data-testid="knowledge-upload-input"
      />
      
      {/* Left Sidebar */}
      <Sidebar 
        onSchemaClick={() => { if (!isRefreshingSchema) setShowSchemaViewer(!showSchemaViewer); }}
        isSchemaOpen={showSchemaViewer}
        isCollapsed={sidebarCollapsed}
        onToggleCollapse={() => setSidebarCollapsed(!sidebarCollapsed)}
      />
      
      {/* Schema Viewer */}
      <SchemaViewer 
        isOpen={showSchemaViewer}
        onClose={() => setShowSchemaViewer(false)}
        onWidthChange={setSchemaViewerWidth}
        sidebarWidth={sidebarWidth}
      />
      
      {/* Main Content */}
      <div className="flex flex-1 flex-col transition-all duration-300" style={getMainContentStyles()}>
        {/* Header */}
        <header className="border-b border-border">
          {/* Desktop Header */}
          <div className="hidden md:grid grid-cols-3 items-center p-6">
            <div className="flex items-center gap-3 justify-self-start">
              <img src="/img/t2s-emblem-256.png" alt="T2S" className="h-11 w-11" data-testid="logo" />
              <div className="leading-tight">
                <div className="text-2xl font-bold tracking-tight"><span className="text-primary">T2</span><span style={{ color: '#4FA84E' }}>S</span></div>
                <p className="text-xs text-muted-foreground">Graph-Powered Text-to-SQL <span className="font-mono opacity-70">v1.0</span></p>
              </div>
            </div>
            <div className="justify-self-center">{debugMode && <DebugPanel />}</div>
            <div className="flex items-center gap-2 justify-self-end">
              {selectedGraph ? (
                <Badge variant="outline" className="bg-emerald-500/15 text-emerald-300 border-emerald-500/30" data-testid="database-status-badge">
                  Connected: {selectedGraph.name}
                </Badge>
              ) : (
                <Badge variant="outline" className="bg-muted text-muted-foreground border-border" data-testid="database-status-badge">
                  No Database Selected
                </Badge>
              )}
              {isAuthenticated ? (
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button
                      variant="ghost"
                      className="p-0 h-auto rounded-full hover:opacity-80 transition-opacity"
                      title={user?.name || user?.email}
                      data-testid="user-menu-trigger"
                    >
                      <Avatar className="h-10 w-10 border-2 border-primary">
                        <AvatarImage src={user?.picture} alt={user?.name || user?.email} />
                        <AvatarFallback className="bg-primary text-white font-medium">
                          {(user?.name || user?.email || 'U').charAt(0).toUpperCase()}
                        </AvatarFallback>
                      </Avatar>
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent className="bg-card border-border text-foreground" align="end">
                    <div className="px-3 py-2 border-b border-border" data-testid="user-info-section">
                      <p className="text-sm font-medium text-foreground" data-testid="user-name-display">{user?.name}</p>
                      <p className="text-xs text-muted-foreground" data-testid="user-email-display">{user?.email}</p>
                    </div>
                    <DropdownMenuItem className="hover:!bg-muted cursor-pointer" onClick={() => setShowTokensModal(true)} data-testid="api-tokens-menu-item">
                      API Tokens
                    </DropdownMenuItem>
                    <DropdownMenuSeparator className="bg-border" />
                    <DropdownMenuItem className="hover:!bg-muted cursor-pointer" onClick={handleLogout} data-testid="logout-menu-item">
                      Logout
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              ) : (
                <Button
                  variant="outline"
                  className="bg-primary border-primary text-white hover:bg-primary/90"
                  onClick={() => setShowLoginModal(true)}
                  data-testid="sign-in-btn"
                >
                  Sign In
                </Button>
              )}
            </div>
          </div>

          {/* Mobile Header */}
          <div className="md:hidden p-4 space-y-3">
            {/* Row 1: Hamburger (if collapsed) + Logo + User */}
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                {sidebarCollapsed && (
                  <button
                    onClick={() => setSidebarCollapsed(false)}
                    className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary text-white hover:bg-primary/90 transition-all"
                    data-testid="sidebar-toggle"
                  >
                    <PanelLeft className="h-5 w-5" />
                  </button>
                )}
                <span className="flex items-center gap-2"><img src="/img/t2s-emblem-256.png" alt="T2S" className="h-8 w-8" data-testid="logo" /><span className="text-xl font-bold"><span className="text-primary">T2</span><span style={{ color: '#4FA84E' }}>S</span></span></span>
              </div>
              <div className="flex items-center gap-2">
                {isAuthenticated ? (
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button
                        variant="ghost"
                        className="p-0 h-auto rounded-full hover:opacity-80 transition-opacity"
                      >
                        <Avatar className="h-8 w-8 border-2 border-primary">
                          <AvatarImage src={user?.picture} alt={user?.name || user?.email} />
                          <AvatarFallback className="bg-primary text-white font-medium text-xs">
                            {(user?.name || user?.email || 'U').charAt(0).toUpperCase()}
                          </AvatarFallback>
                        </Avatar>
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent className="bg-card border-border text-foreground" align="end">
                      <div className="px-3 py-2 border-b border-border" data-testid="user-info-section">
                        <p className="text-sm font-medium text-foreground" data-testid="user-name-display">{user?.name}</p>
                        <p className="text-xs text-muted-foreground" data-testid="user-email-display">{user?.email}</p>
                      </div>
                      <DropdownMenuItem className="hover:!bg-muted cursor-pointer" onClick={() => setShowTokensModal(true)} data-testid="api-tokens-menu-item">
                        API Tokens
                      </DropdownMenuItem>
                      <DropdownMenuSeparator className="bg-border" />
                      <DropdownMenuItem className="hover:!bg-muted cursor-pointer" onClick={handleLogout} data-testid="logout-menu-item">
                        Logout
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                ) : (
                  <Button
                    variant="outline"
                    size="sm"
                    className="bg-primary border-primary text-white hover:bg-primary/90"
                    onClick={() => setShowLoginModal(true)}
                  >
                    Sign In
                  </Button>
                )}
              </div>
            </div>
            
            {/* Row 2: Tagline + Database Status */}
            <div className="flex items-center justify-between gap-2">
              <p className="text-xs text-muted-foreground">Graph-Powered Text-to-SQL<span className="ml-1.5 font-mono opacity-70">V1.0</span></p>
              {selectedGraph ? (
                <Badge variant="default" className="bg-emerald-500/15 text-emerald-300 border-emerald-500/30 text-xs px-2 py-0.5 flex-shrink-0">
                  {selectedGraph.name === 'DEMO_CRM' ? 'CRM' : selectedGraph.name}
                </Badge>
              ) : (
                <Badge variant="secondary" className="bg-muted text-muted-foreground border-border text-xs px-2 py-0.5 flex-shrink-0">
                  No DB
                </Badge>
              )}
            </div>
          </div>
        </header>

        {/* Sub-header for controls */}
        <div className="px-6 py-4 border-b border-border">
          <div className="flex gap-3 flex-wrap md:flex-nowrap items-center">
              <Button
                onClick={handleNewSession}
                disabled={isRefreshingSchema || isChatProcessing}
                className="border-0 text-white font-medium hover:opacity-90 gap-1.5 flex-shrink-0 disabled:opacity-50"
                style={{ backgroundColor: '#2E7D32' }}
                title="Start a new session (clears the conversation)"
                data-testid="new-session-btn"
              >
                <MessageSquarePlus className="h-4 w-4" />
                <span className="hidden sm:inline">New Session</span>
              </Button>
              <div className="self-stretch w-px bg-border mx-1 flex-shrink-0" aria-hidden="true" />
              <Button
                variant="outline"
                className="bg-card border-border text-muted-foreground hover:bg-muted disabled:opacity-50 disabled:cursor-not-allowed p-2"
                onClick={handleRefreshSchema}
                disabled={!selectedGraph || isRefreshingSchema || isChatProcessing}
                title={selectedGraph ? (isRefreshingSchema ? 'Re-indexing…' : isChatProcessing ? 'Wait for query to complete' : 'Re-index: re-pull the schema from the DB and re-embed knowledge / rules / uploaded schemas with the current model') : "Select a database first"}
                data-testid="refresh-schema-btn"
              >
                {isRefreshingSchema ? <LoadingSpinner size="sm" /> : <RefreshCw className="w-4 h-4" />}
              </Button>
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    variant="outline"
                    className="bg-card border-border text-muted-foreground hover:bg-muted flex-1 md:flex-initial"
                    disabled={isRefreshingSchema || isChatProcessing}
                    title={isRefreshingSchema ? 'Refreshing schema...' : isChatProcessing ? 'Wait for query to complete' : undefined}
                    data-testid="database-selector-trigger"
                  >
                    <span className="truncate">{selectedGraph?.name || 'Select Database'}</span>
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent className="bg-card border-border text-foreground">
                  {graphs.map((graph) => {
                    const isDemo = graph.id.startsWith('general_');
                    return (
                      <DropdownMenuItem
                        key={graph.id}
                        className="hover:!bg-muted flex items-center justify-between group"
                        onClick={() => { if (!isRefreshingSchema && !isChatProcessing) selectGraph(graph.id); }}
                        disabled={isRefreshingSchema || isChatProcessing}
                        data-testid={`database-option-${graph.id}`}
                      >
                        <span>{graph.name}</span>
                        <Button
                          variant="ghost"
                          size="sm"
                          className={`h-6 w-6 p-0 opacity-0 group-hover:opacity-100 transition-opacity ${
                            isDemo || isRefreshingSchema || isChatProcessing ? 'cursor-not-allowed opacity-40' : 'hover:bg-red-600 hover:text-white'
                          }`}
                          onClick={(e) => { if (isDemo || isRefreshingSchema || isChatProcessing) return; handleDeleteGraph(graph.id, graph.name, e); }}
                          disabled={isDemo || isRefreshingSchema}
                          title={isDemo ? 'Demo databases cannot be deleted' : (isRefreshingSchema ? 'Refreshing schema...' : `Delete ${graph.name}`)}
                          data-testid={`delete-graph-btn-${graph.id}`}
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </DropdownMenuItem>
                    );
                  })}
                  {graphs.length === 0 && (
                    <DropdownMenuItem disabled className="text-muted-foreground">
                      No databases available
                    </DropdownMenuItem>
                  )}
                </DropdownMenuContent>
              </DropdownMenu>
              <Button
                variant="outline"
                className="bg-primary border-primary text-white hover:bg-primary/90 hover:border-primary hover:text-white flex-1 md:flex-initial shadow-sm hover:shadow-md transition-all"
                onClick={handleConnectDatabase}
                disabled={isRefreshingSchema || isChatProcessing}
                title={isRefreshingSchema ? 'Refreshing schema...' : isChatProcessing ? 'Wait for query to complete' : undefined}
                data-testid="connect-database-btn"
              >
                  <span className="hidden sm:inline">Connect to Database</span>
                  <span className="sm:hidden">Connect DB</span>
              </Button>
              <Button
                variant="outline"
                className="bg-card border-border text-muted-foreground hover:bg-muted hidden md:flex items-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed"
                disabled={isRefreshingSchema || isChatProcessing || isSchemaUploading}
                title={isRefreshingSchema ? 'Refreshing schema...' : isChatProcessing ? 'Wait for query to complete' : 'Upload schema/metadata documents to enrich this database'}
                onClick={handleUploadSchema}
                data-testid="upload-schema-btn"
              >
                  {isSchemaUploading ? <LoadingSpinner size="sm" /> : <FileUp className="w-4 h-4" />}
                  <span>Upload Schema</span>
                  <span
                    role={schemaLoaded ? "button" : undefined}
                    tabIndex={schemaLoaded ? 0 : undefined}
                    onClick={(e) => { if (schemaLoaded) { e.stopPropagation(); setShowLoadedFilesModal(true); } }}
                    className={`h-2.5 w-2.5 rounded-full border ${
                      schemaLoaded
                        ? 'border-green-400 bg-green-500 shadow-[0_0_6px_rgba(34,197,94,0.8)] cursor-pointer'
                        : 'border-muted-foreground/30 bg-transparent'
                    }`}
                    title={schemaLoaded ? 'View / manage uploaded schema files' : undefined}
                    aria-label={schemaLoaded ? 'Uploaded schemas — click to manage' : 'No uploaded schemas'}
                  />
              </Button>
              <div className="hidden md:flex items-center gap-2">
                <Button
                  variant="outline"
                  className="bg-card border-border text-muted-foreground hover:bg-muted flex items-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed"
                  onClick={handleLoadKnowledge}
                  disabled={!selectedGraph || isRefreshingSchema || isChatProcessing || isKnowledgeLoading}
                  title={
                    selectedGraph
                      ? knowledgeLoaded
                        ? 'Knowledge is loaded for this database'
                        : 'Load knowledge file for this database'
                      : 'Select a database first'
                  }
                  data-testid="load-knowledge-btn"
                >
                  {isKnowledgeLoading ? <LoadingSpinner size="sm" /> : <BookOpen className="w-4 h-4" />}
                  <span>Load Knowledge</span>
                  <span
                    role={knowledgeLoaded ? "button" : undefined}
                    tabIndex={knowledgeLoaded ? 0 : undefined}
                    onClick={(e) => { if (knowledgeLoaded) { e.stopPropagation(); setShowLoadedFilesModal(true); } }}
                    className={`h-2.5 w-2.5 rounded-full border ${
                      knowledgeLoaded
                        ? 'border-green-400 bg-green-500 shadow-[0_0_6px_rgba(34,197,94,0.8)] cursor-pointer'
                        : 'border-muted-foreground/30 bg-transparent'
                    }`}
                    title={knowledgeLoaded ? 'View / manage loaded files' : undefined}
                    aria-label={knowledgeLoaded ? 'Knowledge loaded — click to manage' : 'Knowledge not loaded'}
                  />
                </Button>
                {selectedGraph && knowledgeLoaded && (
                  <Button
                    variant="outline"
                    className="bg-card border-border text-muted-foreground hover:bg-red-600 hover:border-red-600 hover:text-white p-2 disabled:opacity-50 disabled:cursor-not-allowed"
                    onClick={handleClearKnowledge}
                    disabled={isRefreshingSchema || isChatProcessing || isKnowledgeLoading}
                    title="Clear knowledge for this database"
                    aria-label="Clear knowledge for this database"
                    data-testid="clear-knowledge-btn"
                  >
                    <Trash2 className="w-4 h-4" />
                  </Button>
                )}
              </div>
          </div>
        </div>
        
        {/* Chat Interface - Full remaining height */}
        <div className="flex-1 overflow-hidden flex justify-center">
          <div className="h-full w-full max-w-7xl md:px-[15px]">
            <ChatInterface
              disabled={isRefreshingSchema}
              onProcessingChange={setIsChatProcessing}
              useMemory={useMemory}
              useRulesFromDatabase={useRulesFromDatabase}
              useKnowledge={knowledgeLoaded}
            />
          </div>
        </div>
      </div>

      {/* Modals */}
      <LoginModal 
        open={showLoginModal} 
        onOpenChange={(open) => {
          setShowLoginModal(open);
          if (!open) {
            // Remember dismissal for this session to avoid pinning the modal
            sessionStorage.setItem('loginModalDismissed', '1');
          }
        }}
        canClose={true}
      />
      <DatabaseModal open={showDatabaseModal} onOpenChange={setShowDatabaseModal} />
      <SchemaUploadModal
        open={showSchemaUploadModal}
        onOpenChange={setShowSchemaUploadModal}
        onUploadingChange={setIsSchemaUploading}
        onUploaded={() => refreshLoadedFiles(selectedGraph?.id)}
      />
      <LoadedFilesModal
        open={showLoadedFilesModal}
        onOpenChange={setShowLoadedFilesModal}
        graphId={selectedGraph?.id}
        graphName={selectedGraph?.name}
        onChanged={() => {
          const gid = selectedGraph?.id;
          refreshLoadedFiles(gid);
          // also refresh the knowledge lamp (knowledge may have been cleared)
          if (gid) {
            setKnowledgeStatusByGraphId(prev => ({ ...prev, [gid]: 'unknown' }));
          }
        }}
      />
      <DeleteDatabaseModal 
        open={showDeleteModal} 
        onOpenChange={setShowDeleteModal}
        databaseName={databaseToDelete?.name || ''}
        onConfirm={confirmDeleteGraph}
        isDemo={databaseToDelete?.isDemo || false}
      />
      <TokensModal open={showTokensModal} onOpenChange={setShowTokensModal} />
    </div>
  );
};

export default Index;
