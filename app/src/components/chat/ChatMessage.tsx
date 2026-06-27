import React, { useState } from 'react';
import { Database, Search, Code, MessageSquare, AlertTriangle, Copy, Check } from 'lucide-react';
import { Avatar, AvatarFallback, AvatarImage } from '@/components/ui/avatar';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Progress } from '@/components/ui/progress';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import type { User as UserType } from '@/types/api';
import { copyText } from '@/lib/clipboard';

interface Step {
  icon: 'search' | 'database' | 'code' | 'message';
  text: string;
}

interface ChatMessageProps {
  type: 'user' | 'ai' | 'ai-steps' | 'sql-query' | 'query-result' | 'confirmation';
  content: string;
  steps?: Step[];
  queryData?: any[]; // For table data
  analysisInfo?: {
    confidence?: number;
    missing?: string;
    ambiguities?: string;
    explanation?: string;
    isValid?: boolean;
  };
  sqlCommented?: string; // SQL with per-column justifications as inline comments
  columnEvidence?: Array<{ table?: string; column?: string; role?: string; reason?: string }>;
  confirmationData?: {
    sqlQuery: string;
    operationType: string;
    message: string;
  };
  errorDetails?: {
    title?: string;
    message?: string;
    detail?: string;
    sqlQuery?: string;
    stage?: string;
    databaseType?: string;
    errorClass?: string;
    raw?: unknown;
  };
  progress?: number; // Progress percentage for AI steps
  user?: UserType | null; // User info for avatar
  onConfirm?: () => void;
  onCancel?: () => void;
}

const ChatMessage = ({ type, content, steps, queryData, analysisInfo, sqlCommented, columnEvidence, confirmationData, errorDetails, progress, user, onConfirm, onCancel }: ChatMessageProps) => {
  const [copied, setCopied] = useState(false);

  const formatSqlForDisplay = (sql: string): string => {
    const normalized = sql.trim().replace(/\s+/g, ' ');
    if (!normalized) return normalized;

    const protectStrings = (text: string): { text: string; literals: string[] } => {
      const literals: string[] = [];
      let result = '';
      let current = '';
      let inString = false;

      for (let i = 0; i < text.length; i += 1) {
        const char = text[i];
        current += char;
        if (char === "'") {
          if (inString && text[i + 1] === "'") {
            current += text[i + 1];
            i += 1;
            continue;
          }
          inString = !inString;
          if (!inString) {
            const marker = `__SQL_LITERAL_${literals.length}__`;
            literals.push(current);
            result += marker;
            current = '';
          }
        } else if (!inString) {
          result += current;
          current = '';
        }
      }

      return { text: result + current, literals };
    };

    const restoreStrings = (text: string, literals: string[]): string => (
      literals.reduce((acc, literal, index) => acc.replace(`__SQL_LITERAL_${index}__`, literal), text)
    );

    const { text, literals } = protectStrings(normalized);
    let formatted = text
      .replace(/\bWITH\b/gi, 'WITH')
      .replace(/\bSELECT\b/gi, '\nSELECT')
      .replace(/\bFROM\b/gi, '\nFROM')
      .replace(/\b(INNER|LEFT|RIGHT|FULL|CROSS)\s+JOIN\b/gi, '\n$1 JOIN')
      .replace(/\bJOIN\b/gi, '\nJOIN')
      .replace(/\bWHERE\b/gi, '\nWHERE')
      .replace(/\bGROUP\s+BY\b/gi, '\nGROUP BY')
      .replace(/\bHAVING\b/gi, '\nHAVING')
      .replace(/\bORDER\s+BY\b/gi, '\nORDER BY')
      .replace(/\bLIMIT\b/gi, '\nLIMIT')
      .replace(/\bUNION\b/gi, '\nUNION')
      .replace(/\bAND\b/gi, '\n  AND')
      .replace(/\bOR\b/gi, '\n  OR')
      .replace(/\s*,\s*/g, ',\n  ')
      .replace(/\(\s*SELECT/gi, '(\n  SELECT')
      .replace(/\)\s*,\s*/g, '),\n')
      .replace(/\n{2,}/g, '\n');

    formatted = restoreStrings(formatted, literals)
      .split('\n')
      .map(line => line.trimEnd())
      .join('\n')
      .trim();

    return formatted;
  };

  const sqlContent = type === 'sql-query' ? formatSqlForDisplay(content) : content;
  // The commented copy is already pretty-printed by sqlglot — render it as-is
  // (running the keyword formatter over /* ... */ comments would mangle them).
  const hasCommented = type === 'sql-query' && !!sqlCommented && sqlCommented.trim().length > 0;
  const displaySql = hasCommented ? sqlCommented!.trim() : sqlContent;

  const detailsText = errorDetails ? [
    `Title: ${errorDetails.title || 'Error'}`,
    `Message: ${errorDetails.message || content}`,
    errorDetails.stage ? `Stage: ${errorDetails.stage}` : null,
    errorDetails.databaseType ? `Database type: ${errorDetails.databaseType}` : null,
    errorDetails.errorClass ? `Error class: ${errorDetails.errorClass}` : null,
    errorDetails.sqlQuery ? `SQL:\n${errorDetails.sqlQuery}` : null,
    errorDetails.detail ? `Detail:\n${errorDetails.detail}` : null,
    errorDetails.raw ? `Raw event:\n${JSON.stringify(errorDetails.raw, null, 2)}` : null,
  ].filter(Boolean).join('\n\n') : '';

  const handleCopyQuery = async () => {
    if (await copyText(displaySql)) {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } else {
      console.error('Failed to copy SQL to clipboard');
    }
  };

  const handleCopyDetails = async () => {
    if (!detailsText) return;
    if (await copyText(detailsText)) {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } else {
      console.error('Failed to copy error details to clipboard');
    }
  };

  if (type === 'confirmation') {
    const operationType = (confirmationData?.operationType ?? 'UNKNOWN').toUpperCase();
    const isHighRisk = ['DELETE', 'DROP', 'TRUNCATE'].includes(operationType);

    return (
      <div className="px-6" data-testid="confirmation-message">
        <div className="flex gap-3 mb-6 items-start">
          <Avatar className="w-8 h-8 flex-shrink-0">
            <AvatarFallback className="bg-primary text-primary-foreground text-xs font-bold">
              T2S
            </AvatarFallback>
          </Avatar>
          <div className="flex-1 min-w-0">
            <Card className={`${isHighRisk ? 'border-error/50 bg-error/5' : 'border-warning/50 bg-warning/5'}`}>
              <CardContent className="p-4">
                <div className="flex items-center gap-2 mb-3">
                  <AlertTriangle className={`w-5 h-5 ${isHighRisk ? 'text-error' : 'text-warning'}`} />
                  <span className={`text-base font-semibold ${isHighRisk ? 'text-error' : 'text-warning'}`}>
                    Destructive Operation Detected
                  </span>
                </div>

                <div className="space-y-3">
                  <div>
                    <p className="text-foreground text-sm mb-2">
                      This operation will perform a <span className={`font-semibold ${isHighRisk ? 'text-error' : 'text-warning'}`}>{operationType}</span> query:
                    </p>
                    {confirmationData?.sqlQuery && (
                      <div className="bg-background border border-border rounded p-3 overflow-x-auto">
                        <pre className="text-sm font-mono text-foreground whitespace-pre-wrap break-words overflow-wrap-anywhere">
                      <code className="language-sql">{formatSqlForDisplay(confirmationData.sqlQuery)}</code>
                        </pre>
                      </div>
                    )}
                  </div>

                  <div className={`${isHighRisk ? 'bg-error/10 border-error/50' : 'bg-warning/10 border-warning/50'} border rounded p-3`}>
                    <p className="text-sm text-foreground">
                      {isHighRisk ? (
                        <>
                          <span className="font-semibold text-error">⚠️ WARNING:</span> This operation may be irreversible and will permanently modify your database.
                        </>
                      ) : (
                        <>This operation will make changes to your database. Please review carefully before confirming.</>
                      )}
                    </p>
                  </div>

                  <div className="flex gap-2 pt-2">
                    <Button
                      variant="outline"
                      onClick={onCancel}
                      className="flex-1 bg-card border-border text-muted-foreground hover:bg-muted"
                      data-testid="confirmation-cancel-button"
                    >
                      Cancel
                    </Button>
                    <Button
                      variant="destructive"
                      onClick={onConfirm}
                      className={`flex-1 ${isHighRisk ? 'bg-error hover:bg-error/90' : 'bg-warning hover:bg-warning/90'} text-white font-semibold`}
                      data-testid="confirmation-confirm-button"
                    >
                      Confirm {operationType}
                    </Button>
                  </div>
                </div>
              </CardContent>
            </Card>
          </div>
        </div>
      </div>
    );
  }

  if (type === 'user') {
    return (
      <div className="px-6" data-testid="user-message">
        <div className="flex justify-end gap-3 mb-6">
          <div className="flex-1 max-w-xl">
            <Card className="bg-muted border-border inline-block float-right">
              <CardContent className="p-3">
                <p className="text-foreground text-base leading-relaxed">{content}</p>
              </CardContent>
            </Card>
          </div>
          <Avatar className="h-10 w-10 border-2 border-primary flex-shrink-0">
            <AvatarImage src={user?.picture} alt={user?.name || user?.email} />
            <AvatarFallback className="bg-primary text-primary-foreground font-medium">
              {(user?.name || user?.email || 'U').charAt(0).toUpperCase()}
            </AvatarFallback>
          </Avatar>
        </div>
      </div>
    );
  }

  if (type === 'sql-query') {
    const hasSQL = content && content.trim().length > 0;
    const isValid = analysisInfo?.isValid !== false; // Default to true if not specified

    return (
      <div className="px-6" data-testid="sql-query-message">
        <div className="flex gap-3 mb-6 items-start">
          <Avatar className="w-8 h-8 flex-shrink-0">
              <AvatarFallback className="bg-primary text-primary-foreground text-xs font-bold">
                T2S
              </AvatarFallback>
          </Avatar>
          <div className="flex-1 min-w-0">
          <Card className={`bg-card ${isValid ? 'border-primary/30' : 'border-warning/30'}`}>
            <CardContent className="p-4">
              <div className="flex items-center gap-2 mb-2">
                <Code className={`w-4 h-4 ${isValid ? 'text-primary' : 'text-warning'}`} />
                <span className={`text-base font-semibold ${isValid ? 'text-primary' : 'text-warning'}`}>
                  {hasSQL ? 'Generated SQL Query' : 'Query Analysis'}
                </span>
              </div>

              {hasSQL && (
                <div className="overflow-x-auto -mx-2 px-2">
                  <div className="relative">
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={handleCopyQuery}
                      className="absolute top-2 right-2 z-10 h-8 w-8 p-0 hover:bg-muted"
                      title={copied ? "Copied!" : "Copy query"}
                    >
                      {copied ? (
                        <Check className="w-4 h-4 text-success" />
                      ) : (
                        <Copy className="w-4 h-4 text-muted-foreground" />
                      )}
                    </Button>
                    <pre className="bg-background text-foreground p-3 rounded text-sm mb-3 w-fit min-w-full font-mono whitespace-pre-wrap break-words overflow-wrap-anywhere">
                      <code className="language-sql">{displaySql}</code>
                    </pre>
                  </div>
                  {columnEvidence && columnEvidence.length > 0 && (
                    <details className="mt-1 mb-2 text-xs text-muted-foreground">
                      <summary className="cursor-pointer select-none hover:text-foreground">
                        Why these columns ({columnEvidence.length})
                      </summary>
                      <ul className="mt-2 space-y-1">
                        {columnEvidence.map((ev, i) => (
                          <li key={i} className="leading-snug">
                            <span className="font-mono text-foreground">
                              {ev.table ? `${ev.table}.` : ''}{ev.column}
                            </span>
                            {ev.role && (
                              <span className="ml-1 uppercase tracking-wide text-[10px] text-primary">
                                {ev.role}
                              </span>
                            )}
                            {ev.reason && <span className="ml-1">— {ev.reason}</span>}
                          </li>
                        ))}
                      </ul>
                    </details>
                  )}
                </div>
              )}

              {!isValid && (
                <div className="space-y-2 text-sm">
                  {analysisInfo?.explanation && (
                    <div className="bg-background/50 p-2 rounded">
                      <span className="font-semibold text-warning">Explanation:</span>
                      <p className="text-foreground mt-1">{analysisInfo.explanation}</p>
                    </div>
                  )}
                  {analysisInfo?.missing && (
                    <div className="bg-background/50 p-2 rounded">
                      <span className="font-semibold text-warning">Missing Information:</span>
                      <p className="text-foreground mt-1">{analysisInfo.missing}</p>
                    </div>
                  )}
                  {analysisInfo?.ambiguities && (
                    <div className="bg-background/50 p-2 rounded">
                      <span className="font-semibold text-warning">Ambiguities:</span>
                      <p className="text-foreground mt-1">{analysisInfo.ambiguities}</p>
                    </div>
                  )}
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      </div>
      </div>
    );
  }

  if (type === 'query-result') {
    return (
      <div className="px-6" data-testid="query-results-message">
        <div className="flex gap-3 mb-6 items-start">
          <Avatar className="w-8 h-8 flex-shrink-0">
            <AvatarFallback className="bg-primary text-primary-foreground text-xs font-bold">
              T2S
            </AvatarFallback>
        </Avatar>
        <div className="flex-1 min-w-0 max-w-full overflow-hidden">
          <Card className="bg-card border-success/30 max-w-full">
            <CardContent className="p-4 max-w-full overflow-hidden">
              <div className="flex items-center gap-2 mb-3">
                <Database className="w-4 h-4 text-success" />
                <span className="text-base font-semibold text-success">Query Results</span>
                <Badge variant="outline" className="ml-auto text-sm">
                  {queryData?.length || 0} rows
                </Badge>
              </div>
              {queryData && queryData.length > 0 && (
                <div className="max-w-full overflow-hidden -mx-4 px-4">
                  <div className="overflow-x-auto overflow-y-auto max-h-96 border border-border rounded scrollbar-visible" style={{ maxWidth: '100%' }}>
                    <table className="text-sm border-collapse" data-testid="results-table" style={{ width: '100%', maxWidth: '100%', tableLayout: 'auto', display: 'table' }}>
                      <thead className="sticky top-0 bg-card z-10">
                        <tr className="border-b border-border">
                          {Object.keys(queryData[0]).map((column) => (
                            <th key={column} className="text-left px-3 py-2 text-muted-foreground font-semibold bg-card break-words" style={{ maxWidth: '300px', minWidth: '100px' }}>
                              {column}
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {queryData.map((row, index) => (
                          <tr key={index} className="border-b border-border hover:bg-muted">
                            {Object.values(row).map((value: any, cellIndex) => (
                              <td key={cellIndex} className="px-3 py-2 text-foreground break-words" style={{ maxWidth: '300px', minWidth: '100px' }}>
                                {String(value)}
                              </td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </CardContent>
          </Card>
        </div>
        </div>
      </div>
    );
  }

  if (type === 'ai') {
    return (
      <div className="px-6" data-testid="ai-message">
        <div className="flex gap-3 mb-6 items-start">
          <Avatar className="w-8 h-8 flex-shrink-0">
              <AvatarFallback className="bg-primary text-primary-foreground text-xs font-bold">
                T2S
              </AvatarFallback>
          </Avatar>
          <div className="flex-1 min-w-0">
            <div className="text-foreground text-base leading-relaxed whitespace-pre-line">
              {content}
            </div>
            {errorDetails && (
              <Dialog>
                <DialogTrigger asChild>
                  <Button
                    variant="outline"
                    size="sm"
                    className="mt-3 border-destructive/40 text-destructive hover:bg-destructive/10"
                  >
                    <AlertTriangle className="w-4 h-4" />
                    Details
                  </Button>
                </DialogTrigger>
                <DialogContent className="sm:max-w-3xl max-h-[85vh] overflow-hidden">
                  <DialogHeader>
                    <DialogTitle>{errorDetails.title || 'Error details'}</DialogTitle>
                    <DialogDescription>
                      Diagnostic details from the backend stream.
                    </DialogDescription>
                  </DialogHeader>
                  <div className="space-y-4 overflow-y-auto pr-1" style={{ maxHeight: '65vh' }}>
                    <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-sm">
                      {errorDetails.stage && (
                        <div className="rounded border border-border bg-muted/40 p-2">
                          <div className="text-xs text-muted-foreground">Stage</div>
                          <div className="font-mono break-all">{errorDetails.stage}</div>
                        </div>
                      )}
                      {errorDetails.databaseType && (
                        <div className="rounded border border-border bg-muted/40 p-2">
                          <div className="text-xs text-muted-foreground">Database</div>
                          <div className="font-mono break-all">{errorDetails.databaseType}</div>
                        </div>
                      )}
                      {errorDetails.errorClass && (
                        <div className="rounded border border-border bg-muted/40 p-2">
                          <div className="text-xs text-muted-foreground">Exception</div>
                          <div className="font-mono break-all">{errorDetails.errorClass}</div>
                        </div>
                      )}
                    </div>

                    {errorDetails.sqlQuery && (
                      <div className="space-y-2">
                        <div className="text-sm font-medium">SQL</div>
                        <pre className="rounded border border-border bg-muted/40 p-3 text-xs font-mono whitespace-pre-wrap break-words">
                          {formatSqlForDisplay(errorDetails.sqlQuery)}
                        </pre>
                      </div>
                    )}

                    <div className="space-y-2">
                      <div className="text-sm font-medium">Backend Detail</div>
                      <pre className="rounded border border-border bg-muted/40 p-3 text-xs font-mono whitespace-pre-wrap break-words">
                        {errorDetails.detail || errorDetails.message || content}
                      </pre>
                    </div>

                    {errorDetails.raw && (
                      <div className="space-y-2">
                        <div className="text-sm font-medium">Raw Event</div>
                        <pre className="rounded border border-border bg-muted/40 p-3 text-xs font-mono whitespace-pre-wrap break-words">
                          {JSON.stringify(errorDetails.raw, null, 2)}
                        </pre>
                      </div>
                    )}
                  </div>
                  <div className="flex justify-end">
                    <Button variant="outline" size="sm" onClick={handleCopyDetails}>
                      {copied ? <Check className="w-4 h-4 text-success" /> : <Copy className="w-4 h-4" />}
                      Copy
                    </Button>
                  </div>
                </DialogContent>
              </Dialog>
            )}
          </div>
        </div>
      </div>
    );
  }

  if (type === 'ai-steps') {
    return (
      <div className="px-6">
      <div className="flex gap-3 mb-6 items-start">
        <Avatar className="w-8 h-8 flex-shrink-0">
          <AvatarFallback className="bg-primary text-primary-foreground text-xs font-bold">
            T2S
          </AvatarFallback>
        </Avatar>
        <div className="flex-1 min-w-0">
          <Card className="bg-card border-primary/30 max-w-md">
            <CardContent className="p-4">
              <div className="space-y-3">
                {steps?.map((step, index) => (
                  <div key={index} className="flex items-center gap-3 text-sm text-foreground">
                    <Badge variant="outline" className="p-1 w-6 h-6 flex items-center justify-center border-primary">
                      {step.icon === 'search' && <Search className="w-3 h-3 text-primary" />}
                      {step.icon === 'database' && <Database className="w-3 h-3 text-primary" />}
                      {step.icon === 'code' && <Code className="w-3 h-3 text-primary" />}
                      {step.icon === 'message' && <MessageSquare className="w-3 h-3 text-primary" />}
                    </Badge>
                    <span>{step.text}</span>
                  </div>
                ))}
                {progress !== undefined && (
                  <div className="mt-4">
                    <Progress value={progress} className="h-2" />
                    <p className="text-xs text-muted-foreground mt-1">{progress}% complete</p>
                  </div>
                )}
              </div>
            </CardContent>
          </Card>
        </div>
      </div>
      </div>
    );
  }

  return null;
};

export default ChatMessage;
