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

