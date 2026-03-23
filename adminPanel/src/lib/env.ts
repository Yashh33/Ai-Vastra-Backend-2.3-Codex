const rawApiBaseUrl = import.meta.env.VITE_ADMIN_API_BASE_URL || "http://localhost:8000";

export const ADMIN_ENV = {
  apiBaseUrl: String(rawApiBaseUrl),
};
