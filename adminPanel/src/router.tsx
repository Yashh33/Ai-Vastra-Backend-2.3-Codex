import { Navigate, Route, Routes } from "react-router-dom";

import { useAdminAuth } from "./lib/auth";
import { AdminDashboardPage } from "./pages/AdminDashboardPage";
import { AdminLoginPage } from "./pages/AdminLoginPage";
import { AdminShopDetailPage } from "./pages/AdminShopDetailPage";

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
        path="/"
        element={
          <RequireAdminAuth>
            <AdminDashboardPage />
          </RequireAdminAuth>
        }
      />

      <Route
        path="/shops/:shopId"
        element={
          <RequireAdminAuth>
            <AdminShopDetailPage />
          </RequireAdminAuth>
        }
      />

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
