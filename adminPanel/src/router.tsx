import { Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "./components/AppShell";
import { useAdminAuth } from "./lib/auth";
import { AdminLoginPage } from "./pages/AdminLoginPage";
import { OperationsDetailPage } from "./pages/OperationsDetailPage";
import { OperationsPage } from "./pages/OperationsPage";
import { PromptLibraryPage } from "./pages/PromptLibraryPage";
import { ShopsPage } from "./pages/ShopsPage";
import { BulkLooksPage } from "./pages/shop/BulkLooksPage";
import { CreditsPage } from "./pages/shop/CreditsPage";
import { GarmentsPage } from "./pages/shop/GarmentsPage";
import { ShopLayout } from "./pages/shop/ShopLayout";
import { ShopSettingsPage } from "./pages/shop/ShopSettingsPage";

function RequireAdminAuth({ children }: { children: React.ReactNode }) {
  const { isAuthenticated } = useAdminAuth();
  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }
  return <>{children}</>;
}

function LoginRoute() {
  const { isAuthenticated } = useAdminAuth();
  if (isAuthenticated) {
    return <Navigate to="/" replace />;
  }
  return <AdminLoginPage />;
}

export function AppRouter() {
  return (
    <Routes>
      <Route path="/login" element={<LoginRoute />} />

      <Route
        element={
          <RequireAdminAuth>
            <AppShell />
          </RequireAdminAuth>
        }
      >
        <Route index element={<ShopsPage />} />

        <Route path="shops/:shopId" element={<ShopLayout />}>
          <Route index element={<Navigate to="garments" replace />} />
          <Route path="garments" element={<GarmentsPage />} />
          <Route path="bulk-looks" element={<BulkLooksPage />} />
          <Route path="settings" element={<ShopSettingsPage />} />
          <Route path="credits" element={<CreditsPage />} />
          <Route path="*" element={<Navigate to="garments" replace />} />
        </Route>

        <Route path="prompt-library" element={<PromptLibraryPage />} />
        <Route path="operations" element={<OperationsPage />} />
        <Route path="operations/:attemptId" element={<OperationsDetailPage />} />
      </Route>

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
