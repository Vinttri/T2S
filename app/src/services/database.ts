import { API_CONFIG, buildApiUrl } from '@/config/api';
import { csrfHeaders } from '@/lib/csrf';
import type { Graph, GraphUploadResponse, SchemaUploadRequest } from '@/types/api';

/**
 * Database/Graph Management Service
 * Handles database schema uploads and graph management
 */

export class DatabaseService {
  /**
   * Get all graphs/databases for the current user
   */
  static async getGraphs(): Promise<Graph[]> {
    try {
      const url = buildApiUrl(API_CONFIG.ENDPOINTS.GRAPHS);
      console.log('Fetching graphs from:', url);
      
      const response = await fetch(url, {
        credentials: 'include',
      });

      console.log('Graphs response status:', response.status);

      // 401/403 = Not authenticated - this is normal if user hasn't signed in
      if (response.status === 401 || response.status === 403) {
        console.log('Not authenticated - sign in to access saved databases');
        return [];
      }

      if (!response.ok) {
        const errorText = await response.text();
        console.error('Failed to fetch graphs:', response.status, errorText);
        throw new Error('Failed to fetch graphs');
      }

      const data = await response.json();
      console.log('Graphs data received:', data);
      
      // Backend returns array of strings like ["northwind", "chinook"]
      // Transform to Graph objects
      const graphNames = data.graphs || data || [];
      
      if (Array.isArray(graphNames) && graphNames.length > 0 && typeof graphNames[0] === 'string') {
        // Transform string array to Graph objects
        return graphNames.map((name: string) => ({
          id: name,
          name: name,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
        }));
      }
      
      // If already objects, return as is
      return graphNames;
    } catch (error) {
      // Backend not available - return empty array for demo mode
      console.log('Backend not available for graphs - using demo mode', error);
      return [];
    }
  }

  /**
   * Get a specific graph by ID
   */
  static async getGraph(id: string): Promise<Graph> {
    try {
      const response = await fetch(
        buildApiUrl(API_CONFIG.ENDPOINTS.GRAPH_BY_ID(id)),
        {
          credentials: 'include',
        }
      );

      if (!response.ok) {
        throw new Error('Failed to fetch graph');
      }

      const data = await response.json();
      return data;
    } catch (error) {
      console.error('Failed to get graph:', error);
      throw error;
    }
  }

  /**
   * Get graph data (nodes and links) for schema visualization
   */
  static async getGraphData(id: string): Promise<{ nodes: any[]; links: any[] }> {
    try {
      const response = await fetch(
        buildApiUrl(`/graphs/${encodeURIComponent(id)}/data`),
        {
          credentials: 'include',
        }
      );

      if (!response.ok) {
        throw new Error('Failed to fetch graph data');
      }

      const data = await response.json();
      return data;
    } catch (error) {
      console.error('Failed to get graph data:', error);
      throw error;
    }
  }

  /**
   * Upload database schema file
   * Accepts SQL files, CSV files, or JSON schema definitions
   */
  static async uploadSchema(request: SchemaUploadRequest): Promise<GraphUploadResponse> {
    try {
      const formData = new FormData();
      const yamlFiles = request.files || (request.file ? [request.file] : []);
      const isYamlUpload = yamlFiles.some((file) => /\.(ya?ml)$/i.test(file.name));

      if (isYamlUpload) {
        yamlFiles.forEach((file) => formData.append('files', file));
      } else if (request.file) {
        formData.append('file', request.file);
      }
      
      if (request.database_name) {
        formData.append('database', request.database_name);
      }

      if (request.schema) {
        formData.append('schema', request.schema);
      }

      if (request.execute_url) {
        formData.append('execute_url', request.execute_url);
      }

      if (request.replace !== undefined) {
        formData.append('replace', String(request.replace));
      }
      
      if (request.description) {
        formData.append('description', request.description);
      }

      const response = await fetch(
        buildApiUrl(isYamlUpload ? '/database/yaml' : API_CONFIG.ENDPOINTS.UPLOAD_SCHEMA),
        {
        method: 'POST',
        body: formData,
        credentials: 'include',
        headers: {
          ...csrfHeaders(),
        },
        }
      );

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.error || 'Failed to upload schema');
      }

      if (isYamlUpload) {
        return {
          graph_id: request.database_name || '',
          message: await response.text(),
        };
      }

      return await response.json();
    } catch (error) {
      console.log('Backend not available for schema upload - demo mode only');
      // Check if it's a network error (backend not running)
      if (error instanceof TypeError && error.message === 'Failed to fetch') {
        throw new Error('Backend server is not running. Please start the T2S backend to upload schemas.');
      }
      throw error;
    }
  }

  /**
   * Delete a graph/database
   */
  static async deleteGraph(id: string): Promise<void> {
    try {
      const response = await fetch(
        buildApiUrl(API_CONFIG.ENDPOINTS.DELETE_GRAPH(id)),
        {
          method: 'DELETE',
          credentials: 'include',
          headers: {
            ...csrfHeaders(),
          },
        }
      );

      if (!response.ok) {
        throw new Error('Failed to delete graph');
      }
    } catch (error) {
      console.error('Failed to delete graph:', error);
      throw error;
    }
  }

  /**
   * Connect to an external database using connection URL
   * Format: postgresql://user:pass@host:port/database or mysql://user:pass@host:port/database
   */
  static async connectDatabaseUrl(config: {
    type: string;
    connectionUrl: string;
  }): Promise<GraphUploadResponse> {
    try {
      const response = await fetch(buildApiUrl('/database'), {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...csrfHeaders(),
        },
        body: JSON.stringify({
          url: config.connectionUrl,
        }),
        credentials: 'include',
      });

      if (!response.ok) {
        const errorMessages: Record<number, string> = {
          401: 'Not authenticated. Please sign in to connect databases.',
          403: 'Access denied. You do not have permission to connect databases.',
          500: 'Server error. Please try again later.',
        };

        // For 400, try to get server error message first
        if (response.status === 400) {
          const errorData = await response.json().catch(() => ({}));
          throw new Error(errorData.error || 'Invalid database connection URL.');
        }

        // Try to get error from response body, fallback to status message
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.error || errorMessages[response.status] || `Failed to connect to database (${response.status})`);
      }

      const data = await response.json();
      return data;
    } catch (error) {
      console.log('Backend not available for database connection - demo mode only');
      // Check if it's a network error (backend not running)
      if (error instanceof TypeError && error.message === 'Failed to fetch') {
        throw new Error('Backend server is not running. Please start the T2S backend to connect to databases.');
      }
      throw error;
    }
  }

  /**
   * Connect to an external database using individual parameters
   * This would require backend implementation for direct database connections
   */
  static async connectDatabase(config: {
    type: string;
    host: string;
    port: number;
    database: string;
    username: string;
    password: string;
  }): Promise<GraphUploadResponse> {
    try {
      // Build connection URL from individual parameters
      const protocol = config.type === 'mysql' ? 'mysql' : 'postgresql';
      const connectionUrl = `${protocol}://${encodeURIComponent(config.username)}:${encodeURIComponent(config.password)}@${config.host}:${config.port}/${encodeURIComponent(config.database)}`;
      
      const response = await fetch(buildApiUrl('/database'), {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...csrfHeaders(),
        },
        body: JSON.stringify({
          url: connectionUrl,
        }),
        credentials: 'include',
      });

      if (!response.ok) {
        const errorMessages: Record<number, string> = {
          401: 'Not authenticated. Please sign in to connect databases.',
          403: 'Access denied. You do not have permission to connect databases.',
          500: 'Server error. Please try again later.',
        };

        // For 400, try to get server error message first
        if (response.status === 400) {
          const errorData = await response.json().catch(() => ({}));
          throw new Error(errorData.error || 'Invalid database connection URL.');
        }

        // Try to get error from response body, fallback to status message
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.error || errorMessages[response.status] || `Failed to connect to database (${response.status})`);
      }

      const data = await response.json();
      return data;
    } catch (error) {
      console.log('Backend not available for database connection - demo mode only');
      // Check if it's a network error (backend not running)
      if (error instanceof TypeError && error.message === 'Failed to fetch') {
        throw new Error('Backend server is not running. Please start the T2S backend to connect to databases.');
      }
      throw error;
    }
  }

  /**
   * Get user rules for a specific database
   */
  static async getUserRules(graphId: string): Promise<string> {
    try {
      const url = buildApiUrl(`${API_CONFIG.ENDPOINTS.GRAPHS}/${graphId}/user-rules`);
      
      const response = await fetch(url, {
        credentials: 'include',
      });

      if (!response.ok) {
        if (response.status === 401 || response.status === 403) {
          return '';
        }
        throw new Error('Failed to fetch user rules');
      }

      const data = await response.json();
      return data.user_rules || '';
    } catch (error) {
      console.error('Error fetching user rules:', error);
      return '';
    }
  }

  /**
   * Update user rules for a specific database
   */
  static async updateUserRules(graphId: string, userRules: string): Promise<void> {
    try {
      console.log('Updating user rules for graph:', graphId, 'Length:', userRules.length);
      const url = buildApiUrl(`${API_CONFIG.ENDPOINTS.GRAPHS}/${graphId}/user-rules`);
      console.log('PUT request to:', url);
      
      const response = await fetch(url, {
        method: 'PUT',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
          ...csrfHeaders(),
        },
        body: JSON.stringify({ user_rules: userRules }),
      });

      console.log('Update user rules response status:', response.status);
      
      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        console.error('Failed to update user rules:', errorData);
        throw new Error(errorData.error || 'Failed to update user rules');
      }
      
      const result = await response.json();
      console.log('User rules updated successfully:', result);
    } catch (error) {
      console.error('Error updating user rules:', error);
      throw error;
    }
  }

  /**
   * Get DB-specific knowledge for a specific database.
   */
  static async getKnowledge(graphId: string): Promise<string> {
    try {
      const url = buildApiUrl(`${API_CONFIG.ENDPOINTS.GRAPHS}/${graphId}/knowledge`);

      const response = await fetch(url, {
        credentials: 'include',
      });

      if (!response.ok) {
        if (response.status === 401 || response.status === 403) {
          return '';
        }
        throw new Error('Failed to fetch knowledge');
      }

      const data = await response.json();
      return data.knowledge || '';
    } catch (error) {
      console.error('Error fetching knowledge:', error);
      return '';
    }
  }

  /**
   * Update DB-specific knowledge for a specific database.
   */
  static async updateKnowledge(graphId: string, knowledge: string): Promise<void> {
    try {
      console.log('Updating knowledge for graph:', graphId, 'Length:', knowledge.length);
      const url = buildApiUrl(`${API_CONFIG.ENDPOINTS.GRAPHS}/${graphId}/knowledge`);

      const response = await fetch(url, {
        method: 'PUT',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
          ...csrfHeaders(),
        },
        body: JSON.stringify({ knowledge }),
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        console.error('Failed to update knowledge:', errorData);
        throw new Error(errorData.error || 'Failed to update knowledge');
      }
    } catch (error) {
      console.error('Error updating knowledge:', error);
      throw error;
    }
  }

  /**
   * List what is loaded into a graph: business knowledge (one merged blob) and
   * uploaded schema documents (per source/filename).
   */
  static async getLoadedFiles(graphId: string): Promise<{
    knowledge: { present: boolean; chars: number };
    documents: Array<{ source: string; chars: number }>;
  }> {
    const url = buildApiUrl(`${API_CONFIG.ENDPOINTS.GRAPHS}/${graphId}/loaded-files`);
    const response = await fetch(url, { credentials: 'include' });
    if (!response.ok) throw new Error('Failed to list loaded files');
    return response.json();
  }

  /** Fetch the stored content of one uploaded schema document (for download). */
  static async getDocument(graphId: string, source: string): Promise<string> {
    const url = buildApiUrl(
      `${API_CONFIG.ENDPOINTS.GRAPHS}/${graphId}/document?source=${encodeURIComponent(source)}`,
    );
    const response = await fetch(url, { credentials: 'include' });
    if (!response.ok) throw new Error('Failed to fetch document');
    const data = await response.json();
    return data.content || '';
  }

  /** Delete one uploaded schema document from the graph; returns chunks removed. */
  static async deleteDocument(graphId: string, source: string): Promise<number> {
    const url = buildApiUrl(
      `${API_CONFIG.ENDPOINTS.GRAPHS}/${graphId}/document?source=${encodeURIComponent(source)}`,
    );
    const response = await fetch(url, {
      method: 'DELETE',
      credentials: 'include',
      headers: { ...csrfHeaders() },
    });
    if (!response.ok) {
      const e = await response.json().catch(() => ({}));
      throw new Error(e.error || 'Failed to delete document');
    }
    const data = await response.json().catch(() => ({}));
    return data.removed || 0;
  }

  /**
   * Enrich an existing DB-built graph via the load-time agent.
   * Streams reasoning steps from POST /database/enrich. The agent reads the
   * uploaded documents and/or knowledge against the live graph, additively
   * vector-indexes the text for retrieval (RAG), and merges grounded
   * description/relationship enrichments. It NEVER creates or replaces schema —
   * the base graph always comes from the database itself.
   */
  static async enrichDatabase(params: {
    database: string;
    files?: File[];
    knowledge?: string;
    userRules?: string;
    onStep?: (message: string, status: 'pending' | 'success' | 'error') => void;
  }): Promise<void> {
    const { database, files = [], knowledge, userRules, onStep } = params;

    const formData = new FormData();
    files.forEach((file) => formData.append('files', file));
    formData.append('database', database);
    if (knowledge) formData.append('knowledge', knowledge);
    if (userRules) formData.append('user_rules', userRules);

    const response = await fetch(buildApiUrl('/database/enrich'), {
      method: 'POST',
      body: formData,
      credentials: 'include',
      headers: {
        ...csrfHeaders(),
      },
    });

    if (!response.ok) {
      const errorData = await response.json().catch(() => ({}));
      throw new Error(errorData.error || `Failed to enrich database (${response.status})`);
    }
    if (!response.body) {
      throw new Error('Streaming response has no body');
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    const delimiter = API_CONFIG.STREAM_BOUNDARY;
    let lastError: string | null = null;

    const processChunk = (text: string) => {
      if (!text.trim()) return;
      let obj: { type?: string; message?: string; success?: boolean };
      try {
        obj = JSON.parse(text);
      } catch {
        return;
      }
      if (obj.type === 'reasoning_step') {
        onStep?.(obj.message || 'Working...', 'pending');
      } else if (obj.type === 'final_result') {
        onStep?.(obj.message || 'Completed', obj.success ? 'success' : 'error');
        if (obj.success === false) lastError = obj.message || 'Enrichment failed';
      } else if (obj.type === 'error') {
        lastError = obj.message || 'Enrichment failed';
        onStep?.(lastError, 'error');
      }
    };

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split(delimiter);
      buffer = parts.pop() || '';
      for (const part of parts) processChunk(part);
    }
    if (buffer.trim()) processChunk(buffer);
    if (lastError) throw new Error(lastError);
  }
}

export const databaseService = DatabaseService;
