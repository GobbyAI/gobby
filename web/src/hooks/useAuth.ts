import { useState, useEffect, useCallback } from "react";

const BASE_URL = import.meta.env.VITE_API_BASE_URL || "";

interface AuthState {
  authenticated: boolean;
  loading: boolean;
}

const FAIL_CLOSED_STATE: AuthState = {
  authenticated: false,
  loading: false,
};

export function useAuth() {
  const [state, setState] = useState<AuthState>({
    authenticated: false,
    loading: true,
  });

  const checkStatus = useCallback(async () => {
    try {
      const res = await fetch(`${BASE_URL}/api/auth/status`, {
        credentials: "include",
      });
      if (!res.ok) {
        setState(FAIL_CLOSED_STATE);
        return;
      }
      const data = await res.json();
      setState({
        authenticated: data.authenticated ?? false,
        loading: false,
      });
    } catch {
      setState(FAIL_CLOSED_STATE);
    }
  }, []);

  useEffect(() => {
    checkStatus();
  }, [checkStatus]);

  const login = useCallback(
    async (
      email: string,
      password: string,
      rememberMe: boolean,
    ): Promise<string | null> => {
      try {
        const res = await fetch(`${BASE_URL}/api/auth/login`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify({ email, password, remember_me: rememberMe }),
        });
        const data = await res.json();
        if (res.ok && data.ok) {
          setState((prev) => ({ ...prev, authenticated: true }));
          return null;
        }
        return data.error || "Login failed";
      } catch {
        return "Network error — is the daemon running?";
      }
    },
    [],
  );

  const logout = useCallback(async () => {
    try {
      await fetch(`${BASE_URL}/api/auth/logout`, {
        method: "POST",
        credentials: "include",
      });
    } catch {
      // Best-effort
    }
    setState((prev) => ({ ...prev, authenticated: false }));
  }, []);

  return { ...state, login, logout };
}
