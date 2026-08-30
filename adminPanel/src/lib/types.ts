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
  use_custom_prompt?: boolean;
  custom_look_prompt?: string | null;
  custom_tryon_prompt?: string | null;
  category?: string;
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
  created_at: string;
};

export type AdminCatalogImageRow = {
  id: string;
  shop_id: string;
  folder_id: string;
  storage_path: string;
  original_filename: string | null;
  mime_type: string | null;
  file_size_bytes: number | null;
  width: number | null;
  height: number | null;
  sort_order: number;
  is_active: boolean;
  created_at: string;
  updated_at: string;
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
};

