type KnowledgeEntry = {
  id?: unknown;
  knowledge?: unknown;
  name?: unknown;
  description?: unknown;
  definition?: unknown;
};

const cleanValue = (value: unknown): string => {
  if (value === null || value === undefined) return "";
  return String(value).split(/\s+/).join(" ").trim();
};

const isRecord = (value: unknown): value is Record<string, unknown> => (
  typeof value === "object" && value !== null && !Array.isArray(value)
);

const isKnowledgeEntry = (value: unknown): value is KnowledgeEntry => {
  if (!isRecord(value)) return false;
  return (
    "id" in value ||
    "knowledge" in value ||
    "name" in value ||
    "description" in value ||
    "definition" in value
  );
};

const sortEntries = (entries: KnowledgeEntry[]): KnowledgeEntry[] => (
  [...entries].sort((left, right) => {
    const leftId = Number(left.id);
    const rightId = Number(right.id);

    if (Number.isFinite(leftId) && Number.isFinite(rightId)) {
      return leftId - rightId;
    }

    return cleanValue(left.id).localeCompare(cleanValue(right.id));
  })
);

const renderEntries = (
  databaseName: string,
  fileName: string,
  entries: KnowledgeEntry[],
): string => {
  const lines = [
    `Domain rules loaded from \`${fileName}\` for database \`${databaseName}\`.`,
    "Use these rules only for business/domain logic. The database schema is authoritative for table and column names.",
    "",
    "Concepts:",
  ];

  for (const entry of sortEntries(entries)) {
    const entryId = cleanValue(entry.id) || "?";
    const name = cleanValue(entry.knowledge) || cleanValue(entry.name) || "?";
    const description = cleanValue(entry.description);
    const definition = cleanValue(entry.definition);

    lines.push(`- [${entryId}] ${name}`);
    if (description) lines.push(`  Description: ${description}`);
    if (definition) lines.push(`  Definition: ${definition}`);
  }

  return `${lines.join("\n").trim()}\n`;
};

const parseJsonlEntries = (content: string, fileName: string): KnowledgeEntry[] | null => {
  const lines = content.split(/\r?\n/).filter((line) => line.trim());
  if (lines.length === 0) return [];

  const entries: KnowledgeEntry[] = [];

  for (const [index, line] of lines.entries()) {
    try {
      const parsed = JSON.parse(line) as unknown;
      if (!isKnowledgeEntry(parsed)) return null;
      entries.push(parsed);
    } catch {
      if (fileName.toLowerCase().endsWith(".jsonl")) {
        throw new Error(`Invalid JSONL on line ${index + 1}`);
      }
      return null;
    }
  }

  return entries;
};

const parseJsonEntries = (content: string, fileName: string): KnowledgeEntry[] | null => {
  try {
    const parsed = JSON.parse(content) as unknown;
    const values = Array.isArray(parsed)
      ? parsed
      : isRecord(parsed) && Array.isArray(parsed.entries)
        ? parsed.entries
        : null;

    if (!values) return null;
    if (!values.every(isKnowledgeEntry)) return null;
    return values;
  } catch {
    if (fileName.toLowerCase().endsWith(".json")) {
      throw new Error("Invalid JSON knowledge file");
    }
    return null;
  }
};

export const renderKnowledgeFileContent = (
  databaseName: string,
  fileName: string,
  content: string,
): string => {
  const trimmed = content.trim();
  if (!trimmed) {
    throw new Error("Knowledge file is empty");
  }

  const entries = parseJsonlEntries(trimmed, fileName) ?? parseJsonEntries(trimmed, fileName);
  if (entries && entries.length > 0) {
    return renderEntries(databaseName, fileName, entries);
  }

  return `${trimmed}\n`;
};
