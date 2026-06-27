import type { AIVendor } from '@/contexts/SettingsContext';

export interface VendorConfig {
  value: AIVendor;
  label: string;
  keyPrefix: string;
  exampleModel: string;
}

export const AI_VENDORS: VendorConfig[] = [
  { value: 'openai', label: 'OpenAI-compatible', keyPrefix: '', exampleModel: 'openai/google/gemma-4-26b-a4b-qat' },
  { value: 'google', label: 'Google', keyPrefix: '', exampleModel: 'gemini-2.5-pro' },
  { value: 'anthropic', label: 'Anthropic', keyPrefix: 'sk-ant-', exampleModel: 'claude-sonnet-4-5' },
];

// Default falls back to the local LM Studio model that install.sh configures;
// the real model in use always comes from the saved endpoint settings.
export const DEFAULT_MODEL = 'openai/google/gemma-4-26b-a4b-qat';

export const CURRENT_SERVER_MODELS = {
  completion: 'openai/google/gemma-4-26b-a4b-qat',
  memoryCompletion: 'openai/google/gemma-4-26b-a4b-qat',
  embedding: 'Qwen3-Embedding',
};

export const CURRENT_COMPLETION_MODELS = [
  'openai/google/gemma-4-26b-a4b-qat',
  'openai/qwen',
  'openai/gpt-oss-20b',
];

export const CURRENT_EMBEDDING_MODELS = [
  'Qwen3-Embedding',
];

const VENDOR_PREFIX_MAP: Record<AIVendor, string> = {
  openai: 'openai',
  google: 'gemini',
  anthropic: 'anthropic',
};

export function getVendorPrefix(vendor: AIVendor): string {
  return VENDOR_PREFIX_MAP[vendor] ?? vendor;
}

export function getVendorConfig(vendor: AIVendor): VendorConfig | undefined {
  return AI_VENDORS.find(v => v.value === vendor);
}

export function validateApiKeyFormat(key: string, vendor: AIVendor): string | null {
  if (!key.trim()) {
    return 'Please enter an API key';
  }

  const config = getVendorConfig(vendor);
  if (config?.keyPrefix && !key.startsWith(config.keyPrefix)) {
    return `Invalid API key format. ${config.label} keys should start with "${config.keyPrefix}"`;
  }

  return null;
}
