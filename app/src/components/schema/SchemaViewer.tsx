import { useEffect, useRef, useState, useCallback } from 'react';
import type { Data, FalkorDBCanvas, GraphLink, GraphNode } from '@falkordb/canvas';
import { ZoomIn, ZoomOut, Locate, RotateCcw, X, GripVertical } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useDatabase } from '@/contexts/DatabaseContext';
import { DatabaseService } from '@/services/database';
import { useToast } from '@/components/ui/use-toast';

interface SchemaReference {
  table?: string;
  column?: string;
  note?: string;
}

interface SchemaColumn {
  name: string;
  type?: string;
  dataType?: string;
  description?: string;
  nullable?: string;
  key_type?: string;
  references?: SchemaReference[];
  referenced_by?: SchemaReference[];
}

interface SchemaNode {
  id: number | string;
  userId: string;
  name: string;
  description?: string;
  columns: Array<string | SchemaColumn>;
}

interface SchemaLink {
  source: number;
  target: number;
  source_column?: string;
  target_column?: string;
}

interface SchemaData {
  nodes: Array<SchemaNode & { id: number }>;
  links: SchemaLink[];
  nodesMap: Map<number, SchemaNode>
}

interface HoveredLink {
  source: string;
  target: string;
  sourceColumn?: string;
  targetColumn?: string;
}

interface SchemaViewerProps {
  isOpen: boolean;
  onClose: () => void;
  onWidthChange?: (width: number) => void;
  sidebarWidth?: number;
}

const SchemaViewer = ({ isOpen, onClose, onWidthChange, sidebarWidth = 64 }: SchemaViewerProps) => {
  const canvasRef = useRef<FalkorDBCanvas>(null);
  const resizeRef = useRef<HTMLDivElement>(null);
  const hoverClearTimerRef = useRef<number | null>(null);
  const infoPanelHoveredRef = useRef(false);
  const [schemaData, setSchemaData] = useState<SchemaData | null>(null);
  const [loading, setLoading] = useState(false);
  const [hoveredNode, setHoveredNode] = useState<SchemaNode | null>(null);
  const [hoveredLink, setHoveredLink] = useState<HoveredLink | null>(null);
  const { selectedGraph } = useDatabase();
  const { toast } = useToast();

  // Track current theme for canvas colors
  const [theme, setTheme] = useState<string>(() => {
    return document.documentElement.getAttribute('data-theme') || 'dark';
  });

  // Listen for theme changes
  useEffect(() => {
    const observer = new MutationObserver((mutations) => {
      mutations.forEach((mutation) => {
        if (mutation.type === 'attributes' && mutation.attributeName === 'data-theme') {
          const newTheme = document.documentElement.getAttribute('data-theme') || 'dark';
          setTheme(newTheme);
        }
      });
    });

    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-theme']
    });

    return () => observer.disconnect();
  }, []);

  const NODE_WIDTH = 160;
  const MAX_RENDERED_COLUMNS = 14;
  const MAX_PANEL_COLUMNS = 18;
  const MIN_WIDTH = 300;
  const MAX_WIDTH_PERCENT = 0.6;
  const DEFAULT_WIDTH_PERCENT = 0.5;

  const [width, setWidth] = useState(() => {
    const initialWidth = Math.floor(window.innerWidth * DEFAULT_WIDTH_PERCENT);
    return initialWidth;
  });
  const [isResizing, setIsResizing] = useState(false);
  const [canvasLoaded, setCanvasLoaded] = useState(false);

  const cancelHoverClear = () => {
    if (hoverClearTimerRef.current !== null) {
      window.clearTimeout(hoverClearTimerRef.current);
      hoverClearTimerRef.current = null;
    }
  };

  const scheduleHoverClear = () => {
    cancelHoverClear();
    hoverClearTimerRef.current = window.setTimeout(() => {
      if (!infoPanelHoveredRef.current) {
        setHoveredNode(null);
        setHoveredLink(null);
      }
      hoverClearTimerRef.current = null;
    }, 160);
  };

  const getColumnName = (column: string | SchemaColumn): string => {
    if (typeof column === 'string') return column;
    return column.name || '';
  };

  const getColumnType = (column: string | SchemaColumn): string => {
    if (typeof column === 'string') return '';
    return String(column.type || column.dataType || '');
  };

  const getRenderedColumnRowCount = (columns: Array<string | SchemaColumn>): number => {
    return Math.min(columns.length, MAX_RENDERED_COLUMNS) + (columns.length > MAX_RENDERED_COLUMNS ? 1 : 0);
  };

  const trimCanvasText = (
    ctx: CanvasRenderingContext2D,
    value: string,
    maxWidth: number
  ): string => {
    const text = String(value || '');
    if (ctx.measureText(text).width <= maxWidth) return text;
    let trimmed = text;
    while (trimmed.length > 1 && ctx.measureText(`${trimmed}...`).width > maxWidth) {
      trimmed = trimmed.slice(0, -1);
    }
    return `${trimmed}...`;
  };

  const formatReference = (reference: SchemaReference): string => {
    return [reference.table, reference.column].filter(Boolean).join('.');
  };

  // Notify parent of width changes
  useEffect(() => {
    if (onWidthChange) {
      onWidthChange(width);
    }
  }, [width, onWidthChange]);

  // Load falkordb-canvas dynamically
  useEffect(() => {
    import('@falkordb/canvas').then(() => {
      setCanvasLoaded(true);
    });
  }, []);

  useEffect(() => {
    if (isOpen && selectedGraph) {
      loadSchemaData();
    }
  }, [isOpen, selectedGraph]);

  useEffect(() => {
    return () => cancelHoverClear();
  }, []);

  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      if (!isResizing) return;

      const newWidth = e.clientX - sidebarWidth;
      const maxWidth = Math.floor(window.innerWidth * MAX_WIDTH_PERCENT);

      if (newWidth >= MIN_WIDTH && newWidth <= maxWidth) {
        setWidth(newWidth);
      }
    };

    const handleMouseUp = () => {
      setIsResizing(false);
    };

    if (isResizing) {
      document.addEventListener('mousemove', handleMouseMove);
      document.addEventListener('mouseup', handleMouseUp);
      document.body.style.cursor = 'ew-resize';
      document.body.style.userSelect = 'none';
    }

    return () => {
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
  }, [isResizing, sidebarWidth]);

  const loadSchemaData = async () => {
    if (!selectedGraph) return;

    setLoading(true);
    try {
      const data = await DatabaseService.getGraphData(selectedGraph.id);
      setHoveredNode(null);
      setHoveredLink(null);

      // Create a mapping from old IDs to new IDs
      const oldIdToNewId = new Map<string, number>();

      // Remap nodes with new sequential IDs
      data.nodes = data.nodes.map((node, index) => {
        const newId = index + 1;
        oldIdToNewId.set(String(node.id), newId);
        return {
          ...node,
          userId: String(node.id),
          id: newId,
        };
      });

      // Update links to use the new node IDs
      data.links = data.links
        .map((link) => ({
          ...link,
          source: oldIdToNewId.get(String(link.source)) || link.source,
          target: oldIdToNewId.get(String(link.target)) || link.target,
        }))
        .filter((link) => typeof link.source === 'number' && typeof link.target === 'number');

      const nodesMap = new Map<number, SchemaNode>(data.nodes.map((node) => [node.id, node]));

      setSchemaData({ ...data, nodesMap });
    } catch (error) {
      console.error('Failed to load schema:', error);
      toast({
        title: 'Failed to Load Schema',
        description: error instanceof Error ? error.message : 'Unknown error occurred',
        variant: 'destructive',
      });
      setSchemaData({ nodes: [], links: [], nodesMap: new Map() });
    } finally {
      setLoading(false);
    }
  };

  const handleZoomIn = () => {
    const canvas = canvasRef.current

    if (canvas) {
      canvas.zoom(canvas.getZoom() * 1.1);
    }
  };

  const handleZoomOut = () => {
    const canvas = canvasRef.current;

    if (canvas) {
      canvas.zoom(canvas.getZoom() * 0.9);
    }
  };

  const handleCenter = useCallback(() => {
    canvasRef.current?.zoomToFit();
  }, []);

  // Convert schema data to canvas format
  const convertToCanvasData = useCallback((data: SchemaData): Data => {
    const nodes = data.nodes.map((node) => {
      // Calculate node size based on height (same calculation as in nodeCanvasObject)
      const columns = node.columns || [];
      const lineHeight = 14;
      const padding = 8;
      const headerHeight = 20;
      const nodeHeight = headerHeight + getRenderedColumnRowCount(columns) * lineHeight + padding * 2;

      // Use the larger dimension as collision radius (in pixels)
      const size = Math.max(NODE_WIDTH / 2, nodeHeight / 2);

      return {
        id: node.id,
        labels: ['Table'],
        color: theme === 'light' ? '#60a5fa' : '#3b82f6',
        visible: true,
        size,
        data: {
          name: node.name,
          description: node.description,
          columns: node.columns
        }
      };
    });

    const links = data.links.map((link, index) => {
      return {
        id: index + 1,
        relationship: 'REFERENCES',
        color: theme === 'light' ? '#64748b' : '#94a3b8',
        visible: true,
        source: link.source,
        target: link.target,
        data: {
          sourceColumn: link.source_column,
          targetColumn: link.target_column
        }
      };
    });

    return { nodes, links };
  }, [theme]);

  // Re-run the auto-layout and fit on demand (discards the manual arrangement).
  const handleResetLayout = useCallback(() => {
    const canvas = canvasRef.current;
    if (canvas && schemaData) {
      canvas.setData(convertToCanvasData(schemaData));
      window.setTimeout(() => canvasRef.current?.zoomToFit(), 600);
    }
  }, [schemaData, convertToCanvasData]);

  // Set up canvas configuration and data - MUST be in single effect to ensure proper order
  useEffect(() => {
    const canvas = canvasRef.current;

    if (!canvas || !canvasLoaded || !schemaData) return;

    const nodeCanvasObject = (node: GraphNode, ctx: CanvasRenderingContext2D) => {
      const lineHeight = 14;
      const padding = 8;
      const headerHeight = 20;
      const fontSize = 12;

      // Theme-aware colors
      const isLight = theme === 'light';
      const textColor = isLight ? '#111' : '#f5f5f5';
      const fillColor = isLight ? '#ffffff' : '#191919';
      const strokeColor = isLight ? '#d1d5db' : '#374151';
      const columnTextColor = isLight ? '#111' : '#e5e7eb';
      const typeTextColor = isLight ? '#6b7280' : '#9ca3af';

      // Find the original schema node to get columns
      const schemaNode = schemaData.nodesMap.get(node.id);

      if (!schemaNode) return;

      const columns = schemaNode.columns || [];
      const renderedColumns = columns.slice(0, MAX_RENDERED_COLUMNS);
      const nodeHeight = headerHeight + getRenderedColumnRowCount(columns) * lineHeight + padding * 2;

      ctx.fillStyle = fillColor;
      ctx.strokeStyle = strokeColor;
      ctx.lineWidth = 1;
      ctx.fillRect(
        (node.x || 0) - NODE_WIDTH / 2,
        (node.y || 0) - nodeHeight / 2,
        NODE_WIDTH,
        nodeHeight
      );
      ctx.strokeRect(
        (node.x || 0) - NODE_WIDTH / 2,
        (node.y || 0) - nodeHeight / 2,
        NODE_WIDTH,
        nodeHeight
      );

      ctx.fillStyle = textColor;
      ctx.font = `bold ${fontSize}px Arial`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(
        trimCanvasText(ctx, schemaNode.name, NODE_WIDTH - padding * 2),
        node.x || 0,
        (node.y || 0) - nodeHeight / 2 + headerHeight / 2 + padding / 2
      );

      ctx.font = `${fontSize - 2}px Arial`;
      ctx.textAlign = 'left';
      const startX = (node.x || 0) - NODE_WIDTH / 2 + padding;
      let colY = (node.y || 0) - nodeHeight / 2 + headerHeight + padding;

      renderedColumns.forEach((col: string | SchemaColumn) => {
        const name = getColumnName(col);
        const type = getColumnType(col);
        const hasOutgoingFk = typeof col === 'object' && Boolean(col.references?.length);
        const hasIncomingFk = typeof col === 'object' && Boolean(col.referenced_by?.length);
        const marker = hasOutgoingFk ? 'FK ' : hasIncomingFk ? 'REF ' : '';

        ctx.textAlign = 'left';
        ctx.fillStyle = hasOutgoingFk ? (isLight ? '#1d4ed8' : '#93c5fd') : columnTextColor;
        const typeReservedWidth = type ? 52 : 0;
        const visibleName = trimCanvasText(
          ctx,
          `${marker}${name}`,
          NODE_WIDTH - padding * 2 - typeReservedWidth
        );
        ctx.fillText(visibleName, startX, colY);

        if (type) {
          ctx.fillStyle = typeTextColor;
          const nameWidth = ctx.measureText(visibleName).width;
          const available = NODE_WIDTH - padding * 2 - nameWidth - 8;
          let typeText = String(type);
          if (available > 0) {
            if (ctx.measureText(typeText).width > available) {
              while (
                typeText.length > 0 &&
                ctx.measureText(typeText + '…').width > available
              ) {
                typeText = typeText.slice(0, -1);
              }
              typeText = typeText + '…';
            }
            ctx.textAlign = 'right';
            ctx.fillText(typeText, (node.x || 0) + NODE_WIDTH / 2 - padding, colY);
          }
          ctx.fillStyle = columnTextColor;
          ctx.textAlign = 'left';
        }

        colY += lineHeight;
      });

      if (columns.length > MAX_RENDERED_COLUMNS) {
        ctx.fillStyle = typeTextColor;
        ctx.textAlign = 'left';
        ctx.fillText(
          `... ${columns.length - MAX_RENDERED_COLUMNS} more columns`,
          startX,
          colY
        );
      }
    };

    const nodePointerAreaPaint = (node: GraphNode, color: string, ctx: CanvasRenderingContext2D) => {
      const schemaNode = schemaData.nodesMap.get(node.id);

      if (!schemaNode) return;

      const columns = schemaNode.columns || [];
      const lineHeight = 14;
      const padding = 8;
      const headerHeight = 20;
      const nodeHeight = headerHeight + getRenderedColumnRowCount(columns) * lineHeight + padding * 2;

      ctx.fillStyle = color;
      const areaPadding = 5;
      ctx.fillRect(
        (node.x || 0) - NODE_WIDTH / 2 - areaPadding,
        (node.y || 0) - nodeHeight / 2 - areaPadding,
        NODE_WIDTH + areaPadding * 2,
        nodeHeight + areaPadding * 2
      );
    };

    const linkCanvasObject = (link: GraphLink, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const source = link.source;
      const target = link.target;
      if (
        source.x === undefined ||
        source.y === undefined ||
        target.x === undefined ||
        target.y === undefined
      ) {
        return;
      }

      const isLight = theme === 'light';
      const color = isLight ? '#475569' : '#cbd5e1';
      const dx = target.x - source.x;
      const dy = target.y - source.y;
      const distance = Math.sqrt(dx * dx + dy * dy);
      if (distance === 0) return;

      const unitX = dx / distance;
      const unitY = dy / distance;
      const startPad = Math.min(source.size || 0, 80);
      const endPad = Math.min(target.size || 0, 80);
      const startX = source.x + unitX * startPad;
      const startY = source.y + unitY * startPad;
      const endX = target.x - unitX * endPad;
      const endY = target.y - unitY * endPad;

      ctx.save();
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      ctx.lineWidth = Math.max(1.2 / globalScale, 0.7);
      ctx.globalAlpha = 0.9;
      ctx.beginPath();
      ctx.moveTo(startX, startY);
      ctx.lineTo(endX, endY);
      ctx.stroke();

      const arrowSize = Math.max(5 / globalScale, 3);
      const angle = Math.atan2(endY - startY, endX - startX);
      ctx.beginPath();
      ctx.moveTo(endX, endY);
      ctx.lineTo(
        endX - arrowSize * Math.cos(angle - Math.PI / 6),
        endY - arrowSize * Math.sin(angle - Math.PI / 6)
      );
      ctx.lineTo(
        endX - arrowSize * Math.cos(angle + Math.PI / 6),
        endY - arrowSize * Math.sin(angle + Math.PI / 6)
      );
      ctx.closePath();
      ctx.fill();
      ctx.restore();
    };

    const linkPointerAreaPaint = (link: GraphLink, color: string, ctx: CanvasRenderingContext2D) => {
      const source = link.source;
      const target = link.target;
      if (
        source.x === undefined ||
        source.y === undefined ||
        target.x === undefined ||
        target.y === undefined
      ) {
        return;
      }

      ctx.save();
      ctx.strokeStyle = color;
      ctx.lineWidth = 8;
      ctx.beginPath();
      ctx.moveTo(source.x, source.y);
      ctx.lineTo(target.x, target.y);
      ctx.stroke();
      ctx.restore();
    };


    const canvasData = convertToCanvasData(schemaData);

    canvas.setConfig({
      // Let the layout settle and STOP so a user's manual node placement sticks
      // (otherwise the force engine keeps running and re-arranges everything).
      autoStopOnSettle: true,
      onNodeHover: (node: GraphNode | null) => {
        if (!node) {
          scheduleHoverClear();
          return;
        }
        cancelHoverClear();
        setHoveredNode(schemaData.nodesMap.get(node.id) || null);
        setHoveredLink(null);
      },
      onLinkHover: (link: GraphLink | null) => {
        if (!link) {
          scheduleHoverClear();
          return;
        }
        cancelHoverClear();
        setHoveredNode(null);
        setHoveredLink({
          source: String(link.source.data?.name || link.source.id),
          target: String(link.target.data?.name || link.target.id),
          sourceColumn: link.data?.sourceColumn,
          targetColumn: link.data?.targetColumn,
        });
      },
      node: {
        nodeCanvasObject,
        nodePointerAreaPaint,
      },
      link: {
        linkCanvasObject,
        linkPointerAreaPaint,
      }
    });
    
    canvas.setBackgroundColor(theme === 'light' ? '#ffffff' : '#191919');
    canvas.setForegroundColor(theme === 'light' ? '#111' : '#f5f5f5');
    canvas.setData(canvasData);
  }, [schemaData, theme, canvasLoaded, convertToCanvasData]);

  if (!isOpen) return null;

  return (
    <>
      {/* Mobile overlay backdrop */}
      <div
        className="fixed inset-0 bg-black/50 z-40 md:hidden"
        onClick={onClose}
      />

      {/* Schema Viewer */}
      <div
        data-testid="schema-panel"
        className={`fixed top-0 h-full bg-background border-r border-border flex flex-col transition-all duration-300
          ${isOpen ? 'translate-x-0' : '-translate-x-full pointer-events-none'}
          md:z-30 z-50
          w-[80vw] max-w-[400px] md:max-w-none
        `}
        style={{
          ...(window.innerWidth >= 768 ? {
            left: `${sidebarWidth}px`,
            width: `${width}px`
          } : {})
        }}
      >
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-border">
          <h2 className="text-lg font-semibold text-foreground">Database Schema</h2>
          <Button
            variant="ghost"
            size="sm"
            onClick={onClose}
            className="h-8 w-8 p-0 text-muted-foreground hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </Button>
        </div>

        {/* Controls */}
        <div className="flex gap-2 p-2 border-b border-border">
          <Button
            variant="outline"
            size="sm"
            onClick={handleZoomIn}
            className="h-8 w-8 p-0 bg-card border-border text-muted-foreground hover:bg-foreground"
            title="Zoom In"
          >
            <ZoomIn className="h-4 w-4" />
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={handleZoomOut}
            className="h-8 w-8 p-0 bg-card border-border text-muted-foreground hover:bg-foreground"
            title="Zoom Out"
          >
            <ZoomOut className="h-4 w-4" />
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={handleCenter}
            className="h-8 w-8 p-0 bg-card border-border text-muted-foreground hover:bg-foreground"
            title="Fit to view"
          >
            <Locate className="h-4 w-4" />
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={handleResetLayout}
            className="h-8 w-8 p-0 bg-card border-border text-muted-foreground hover:bg-foreground"
            title="Reset layout"
          >
            <RotateCcw className="h-4 w-4" />
          </Button>
        </div>

        {/* Graph Container */}
        <div className="h-[calc(100%-8rem)] w-full bg-background relative">
          {loading && (
            <div className="flex items-center justify-center h-full">
              <div className="text-muted-foreground">Loading schema...</div>
            </div>
          )}
          {!loading && canvasLoaded && schemaData && schemaData.nodes.length > 0 && (
            <falkordb-canvas ref={canvasRef} node-mode='replace' link-mode='replace' />
          )}
          {!loading && hoveredNode && (
            <div
              className="absolute right-3 top-3 z-10 w-[22rem] max-w-[calc(100%-1.5rem)] max-h-[52vh] overflow-auto rounded-md border border-border bg-card p-2.5 shadow-xl"
              onMouseEnter={() => {
                infoPanelHoveredRef.current = true;
                cancelHoverClear();
              }}
              onMouseLeave={() => {
                infoPanelHoveredRef.current = false;
                scheduleHoverClear();
              }}
            >
              <div className="truncate text-[11px] font-semibold text-foreground" title={hoveredNode.name}>
                {hoveredNode.name}
              </div>
              {hoveredNode.description && (
                <div className="mt-1 line-clamp-2 text-[10px] leading-snug text-muted-foreground" title={hoveredNode.description}>
                  {hoveredNode.description}
                </div>
              )}
              <div className="mt-1.5 text-[10px] text-muted-foreground">
                {hoveredNode.columns.length} columns
              </div>
              <div className="mt-1.5 space-y-1.5">
                {hoveredNode.columns.slice(0, MAX_PANEL_COLUMNS).map((column) => {
                  const columnName = getColumnName(column);
                  const columnType = getColumnType(column);
                  const description = typeof column === 'object' ? column.description : '';
                  const references = typeof column === 'object' ? column.references || [] : [];
                  const referencedBy = typeof column === 'object' ? column.referenced_by || [] : [];
                  const referenceText = references.map(formatReference).filter(Boolean).join(', ');
                  const referencedByText = referencedBy.map(formatReference).filter(Boolean).join(', ');
                  return (
                    <div key={columnName} className="border-t border-border/60 pt-1.5 first:border-t-0 first:pt-0">
                      <div className="flex min-w-0 items-baseline gap-1.5">
                        <span className="truncate text-[10px] font-medium text-foreground" title={columnName}>
                          {columnName}
                        </span>
                        {columnType && (
                          <span className="shrink-0 text-[9px] text-muted-foreground" title={columnType}>
                            {columnType}
                          </span>
                        )}
                        {references.length > 0 && (
                          <span className="shrink-0 text-[9px] font-medium text-blue-500">FK</span>
                        )}
                      </div>
                      {description && (
                        <div className="mt-0.5 line-clamp-2 text-[9px] leading-snug text-muted-foreground" title={description}>
                          {description}
                        </div>
                      )}
                      {referenceText && (
                        <div className="mt-0.5 truncate text-[9px] text-blue-500" title={`FK to ${referenceText}`}>
                          FK to {referenceText}
                        </div>
                      )}
                      {referencedByText && (
                        <div className="mt-0.5 truncate text-[9px] text-muted-foreground" title={`Referenced by ${referencedByText}`}>
                          Referenced by {referencedByText}
                        </div>
                      )}
                    </div>
                  );
                })}
                {hoveredNode.columns.length > MAX_PANEL_COLUMNS && (
                  <div className="border-t border-border/60 pt-1.5 text-[9px] text-muted-foreground">
                    ... {hoveredNode.columns.length - MAX_PANEL_COLUMNS} more columns
                  </div>
                )}
              </div>
            </div>
          )}
          {!loading && !hoveredNode && hoveredLink && (
            <div
              className="absolute right-3 top-3 z-10 w-[22rem] max-w-[calc(100%-1.5rem)] rounded-md border border-border bg-card p-2.5 shadow-xl"
              onMouseEnter={() => {
                infoPanelHoveredRef.current = true;
                cancelHoverClear();
              }}
              onMouseLeave={() => {
                infoPanelHoveredRef.current = false;
                scheduleHoverClear();
              }}
            >
              <div className="text-[11px] font-semibold text-foreground">Foreign key</div>
              <div
                className="mt-1 truncate text-[10px] text-muted-foreground"
                title={`${hoveredLink.source}.${hoveredLink.sourceColumn || '?'} -> ${hoveredLink.target}.${hoveredLink.targetColumn || '?'}`}
              >
                {hoveredLink.source}.{hoveredLink.sourceColumn || '?'}{' -> '}{hoveredLink.target}.{hoveredLink.targetColumn || '?'}
              </div>
            </div>
          )}
          {!loading && (!schemaData || schemaData.nodes.length === 0) && (
            <div className="flex items-center justify-center h-full">
              <div className="text-center text-muted-foreground">
                <p>No schema data available</p>
                <p className="text-sm mt-2">
                  {!selectedGraph ? 'Select a database first' : 'This database has no schema data'}
                </p>
              </div>
            </div>
          )}
        </div>

        {/* Resize Handle */}
        <div
          ref={resizeRef}
          className="absolute right-0 top-0 w-1 h-full cursor-ew-resize hover:bg-primary transition-colors z-50"
          onMouseDown={(e) => {
            e.preventDefault();
            e.stopPropagation();
            setIsResizing(true);
          }}
        >
          <div className="absolute right-0 top-1/2 -translate-y-1/2 -translate-x-1/2">
            <GripVertical className="h-4 w-4 text-border" />
          </div>
        </div>
      </div>
    </>
  );
};

export default SchemaViewer;
