// API Types and Interfaces

// User types
export interface User {
  id: string;
  email: string;
  name?: string;
  picture?: string;
  provider?: 'google' | 'github';
}

// Authentication types
export interface AuthStatus {
  authenticated: boolean;
  user?: User;
}

// Graph/Database types
export interface Graph {
  id: string;
  name: string;
  description?: string;
  created_at: string;
  updated_at: string;
  table_count?: number;
  schema?: any;
}

export interface GraphUploadResponse {
  graph_id: string;
  message: string;
  tables?: string[];
}

// Chat message types
export interface ChatRequest {
  query: string;
  database: string;
  history?: ConversationMessage[];
  customApiKey?: string;
  customModel?: string;
  customVendor?: 'openai' | 'google' | 'anthropic';
  use_user_rules?: boolean; // If true, backend fetches rules from database
  use_knowledge?: boolean; // If true, backend fetches DB-specific knowledge
  use_memory?: boolean;
  sessionContext?: SessionContext | null; // Prior-turn plan echoed for follow-up refinement
}

export interface ConversationMessage {
  role: 'user' | 'assistant';
  content: string;
}

// Prior-turn plan the client echoes back so a follow-up refines the same JSON.
export interface SessionContext {
  db_id: string;
  prior_question: string;
  prior_sql: string;
  selected_columns: Array<{ table?: string; column?: string; role?: string; reason?: string }>;
}

// Streaming response types
export type StreamMessageType = 
  | 'reasoning'
  | 'reasoning_step'  // Backend sends this for step updates
  | 'sql'
  | 'sql_query'       // Backend sends this for SQL queries
  | 'result'
  | 'query_result'    // Backend sends this for query results
  | 'ai_response'     // Backend sends this for AI-generated responses
  | 'error'
  | 'followup'
  | 'followup_questions' // Backend sends this when query needs clarification
  | 'healing_success' // Backend sends this when a failed SQL was repaired and executed
  | 'healing_failed'
  | 'confirmation'
  | 'destructive_confirmation' // Backend sends this for destructive operations
  | 'schema_refresh'  // Backend sends this after schema modifications
  | 'status';

export interface StreamMessage {
  type: StreamMessageType;
  content?: string;
  message?: string;    // Some backend messages use 'message' instead of 'content'
  data?: any;
  step?: string;
  require_confirmation?: boolean;
  confirmation_id?: string;
  final_response?: boolean;
  conf?: number;       // Confidence score
  miss?: string;       // Missing information
  amb?: string;        // Ambiguities
  exp?: string;        // Explanation
  is_valid?: boolean;
  sql_commented?: string;       // SQL with per-column justifications as inline comments
  column_evidence?: Array<{ table?: string; column?: string; role?: string; reason?: string }>;
  evidence_issues?: Array<{ check?: string; severity?: string; column?: string; message?: string }>;
  missing_information?: string; // For followup_questions
  ambiguities?: string;         // For followup_questions
  sql_query?: string;           // For destructive_confirmation
  healed_sql?: string;          // For healing_success
  attempts?: number;            // For healing_success/healing_failed
  final_error?: string;         // For healing_failed
  error?: string;
  error_detail?: string;
  error_class?: string;
  database_type?: string;
  operation_type?: string;      // For destructive_confirmation
  refresh_status?: string;      // For schema_refresh
}

// Confirmation types
export interface ConfirmRequest {
  sql_query: string;      // The SQL query to execute
  confirmation: string;   // "CONFIRM" or "" (empty for cancel)
  chat: string[];         // Conversation history
  use_user_rules?: boolean; // If true, backend fetches rules from database
  use_knowledge?: boolean;  // If true, backend fetches DB-specific knowledge
  use_memory?: boolean;
  custom_api_key?: string;
  custom_model?: string;
}

// Upload types
export interface SchemaUploadRequest {
  file?: File;
  files?: File[];
  database_name?: string;
  schema?: string;
  execute_url?: string;
  replace?: boolean;
  description?: string;
}

// API Error
export interface ApiError {
  error: string;
  detail?: string;
  status?: number;
}
