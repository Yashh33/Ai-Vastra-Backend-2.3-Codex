export type AdminSession = {
  apiBaseUrl: string;
  adminSecret: string;
};

export type AdminShopRow = {
  id: string;
  name: string;
  header_display_text?: string | null;
  carousel_mode_default?: boolean;
  logo_path?: string | null;
  is_suspended?: boolean;
  created_at?: string;
  updated_at?: string;
  owner_auth_user_id?: string | null;
  credits_balance?: number;
  whatsapp_phone?: string | null;
  owner_email?: string | null;
  channel?: string | null;
  status?: string | null;
  whatsapp_multifabric_enabled?: boolean;
  screen_slug?: string | null;
  /** Active garment types (GET /admin/shops only). */
  garments_count?: number;
};

export type AdminCreateShopResponse = {
  shop_id: string;
  shop_name: string;
  auth_user_id: string;
  email: string;
  role: string;
  opening_credits: number;
  balance_before: number;
  balance_after: number;
};

export type AdminSetDefaultHeroRequest = {
  default_hero_image_id: string | null;
};

export type AdminFolderRow = {
  id: string;
  shop_id: string;
  name: string;
  prompt_template: string;
  default_hero_image_id?: string | null;
  is_active: boolean;
  created_at: string;
  updated_at: string;
  show_in_whatsapp_menu?: boolean;
  look_prompt?: string | null;
  tryon_prompt?: string | null;
  look_tryon_prompt?: string | null;
  category?: string;
  // Prompt library (sql/2026-10-28_prompt_library.sql). prompt_set null = own prompts.
  prompt_set?: PromptSetKey | null;
  prompt_set_version?: number | null;
  design_text?: string | null;
  keep_text?: string | null;
  part1_name?: string | null;
  part1_sam_word?: string | null;
  part2_name?: string | null;
  part2_sam_word?: string | null;
  part2_default?: "white" | "black" | null;
};

export type PromptVersion = {
  id: string;
  garment_type_id: string;
  shop_id: string;
  look_prompt: string | null;
  tryon_prompt: string | null;
  look_tryon_prompt?: string | null;
  note: string | null;
  created_at: string;
};

export type AdminHeroImageRow = {
  id: string;
  shop_id: string;
  folder_id: string;
  storage_path: string;
  original_filename: string | null;
  mime_type: string | null;
  file_size_bytes: number | null;
  width: number | null;
  height: number | null;
  signed_url?: string | null;
  thumb_signed_url?: string | null;
  created_at: string;
};

export type LookUploadStatus = "uploaded" | "duplicate" | "failed";

export type LookUploadItem = {
  filename: string;
  barcode: string | null;
  status: LookUploadStatus;
  generation_id?: string;
  size_kb?: number;
  error?: string;
};

export type LookUploadResponse = {
  shop_id: string;
  folder_id: string;
  uploaded: number;
  duplicates: number;
  failed: number;
  items: LookUploadItem[];
};

export type AdminFabricSlotRow = {
  id: string;
  folder_id: string;
  shop_id: string;
  label: string;
  apply_to: string;
  sort_order: number;
  created_at?: string;
};

export type GenerationInspect = {
  id: string;
  status: string;
  generation_type: string | null;
  model_used: string | null;
  prompt_used: string | null;
  hero_image_id: string | null;
  fabric_image_id: string | null;
  folder_id: string | null;
  output_path: string | null;
  created_at: string;
  garment_name: string | null;
  output_signed_url: string | null;
  hero_signed_url: string | null;
  fabric_signed_url: string | null;
  is_hero: boolean;
};


// --- Operations: generation logs ------------------------------------------

export type AttemptType = "look" | "tryon" | "look_tryon";
export type AttemptStatus = "started" | "running" | "succeeded" | "failed" | "timed_out";

export type AttemptRow = {
  id: string;
  request_id: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  shop_id: string | null;
  shop_name: string | null;
  user_email: string | null;
  user_label: string | null;
  attempt_type: AttemptType;
  garment_type_name: string | null;
  fabric_mode: "single" | "multi" | null;
  /** Shown status: open rows older than 5 minutes read as timed_out. */
  status: AttemptStatus;
  raw_status: AttemptStatus;
  failed_step: string | null;
  credits_charged: number | null;
  credits_refunded: number | null;
  backfilled: boolean;
  device_label: string | null;
  has_result: boolean;
  thumb_url: string | null;
};

export type AttemptSummary = {
  attempts: number;
  succeeded: number;
  /** Failed + timed out. */
  failed: number;
  running: number;
  /** Succeeded / finished, in percent; null when nothing finished. */
  success_rate: number | null;
};

export type AttemptListResponse = {
  items: AttemptRow[];
  total: number;
  page: number;
  page_size: number;
  summary: AttemptSummary;
};

export type AttemptFabricInput = {
  slot_label: string | null;
  apply_to: string | null;
  sort_order: number | null;
  storage_path: string | null;
  colour_corrected: boolean | null;
  url: string | null;
  // Built-in plain fabric used for an empty optional slot ("white" | "black"); no storage path.
  default_fabric?: string | null;
  fabric_label?: string | null;
};

export type AttemptDetail = Omit<AttemptRow, "has_result" | "thumb_url"> & {
  client_request_id: string | null;
  user_id: string | null;
  garment_type_id: string | null;
  hero_path: string | null;
  hero_colour_corrected: boolean | null;
  source_generation_id: string | null;
  fabric_inputs: AttemptFabricInput[];
  prompt_text: string | null;
  prompt_version_id: string | null;
  prompt_version_label: string | null;
  model_id: string | null;
  user_message: string | null;
  error_detail: string | null;
  http_status: number | null;
  ai_finish_reason: string | null;
  ai_safety: unknown;
  generation_id: string | null;
  output_path: string | null;
  tryon_result_path: string | null;
  ai_input_tokens: number | null;
  ai_output_tokens: number | null;
  ai_cost_inr: number | string | null;
  ai_started_at: string | null;
  ai_finished_at: string | null;
  user_agent: string | null;
  endpoint: string | null;
  hero_url: string | null;
  output_url: string | null;
  tryon_result_url: string | null;
};

export type ShopNameRow = { id: string; name: string | null };


// --- Prompt library ---------------------------------------------------------

export type PromptSetKey = "single" | "two";

export type PromptSetVersion = {
  id: string;
  set_key: PromptSetKey;
  version: number;
  look_prompt: string;
  tryon_prompt: string;
  look_tryon_prompt: string | null;
  note: string | null;
  created_at: string;
  garments_count: number;
};

export type PromptSet = {
  key: PromptSetKey;
  label: string;
  blanks: string[];
  garments_count: number;
  latest_version: number | null;
  /** Newest first. */
  versions: PromptSetVersion[];
};

export type PromptLibraryResponse = { sets: PromptSet[] };

export type MoveAllResponse = {
  set_key: PromptSetKey;
  version: number;
  moved: number;
  unchanged: number;
  failed: { garment_id: string; shop_id: string; name: string | null; error: string }[];
};

export type GarmentOverviewRow = AdminFolderRow & {
  fabric_slots: AdminFabricSlotRow[];
  hero_thumb_url: string | null;
};

export type GarmentsOverviewResponse = {
  garments: GarmentOverviewRow[];
  latest_versions: Record<PromptSetKey, number | null>;
};

export type SkillFields = {
  name?: string;
  category?: string;
  prompt_set?: PromptSetKey;
  part1_name?: string;
  part1_sam_word?: string;
  part2_name?: string;
  part2_sam_word?: string;
  part2_default?: "white" | "black";
  design_text?: string;
  keep_text?: string;
};

export type SkillParseResponse = { fields: SkillFields; filled: string[] };
