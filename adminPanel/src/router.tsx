import { Link, Navigate, Route, Routes, useLocation } from "react-router-dom";

import { useAdminAuth } from "./lib/auth";
import { AdminDashboardPage } from "./pages/AdminDashboardPage";
import { AdminLoginPage } from "./pages/AdminLoginPage";
import { AdminShopDetailPage } from "./pages/AdminShopDetailPage";
import { OperationsDetailPage } from "./pages/OperationsDetailPage";
import { OperationsPage } from "./pages/OperationsPage";

/** Top tabs: Setup = shops and catalog (unchanged pages), Operations = generation logs. */
function TopNav() {
  const { pathname } = useLocation();
  const inOperations = pathname.startsWith("/operations");
  return (
    <nav className="top-nav" aria-label="Sections">
      <div className="top-nav-inner">
        <Link className={inOperations ? "tab" : "tab tab-active"} to="/">
          Setup
        </Link>
        <Link className={inOperations ? "tab tab-active" : "tab"} to="/operations">
          Operations
        </Link>
      </div>
    </nav>
  );
}

function RequireAdminAuth({ children }: { children: React.ReactNode }) {
  const { isAuthenticated } = useAdminAuth();
  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }
  return (
    <>
      <TopNav />
      {children}
    </>
  );
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

      <Route
        path="/operations"
        element={
          <RequireAdminAuth>
            <OperationsPage />
          </RequireAdminAuth>
        }
      />

      <Route
        path="/operations/:attemptId"
        element={
          <RequireAdminAuth>
            <OperationsDetailPage />
          </RequireAdminAuth>
        }
      />

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
