/**
 * First-run setup wizard state machine.
 *
 * Owns the 4-step provider onboarding flow (welcome -> choose provider ->
 * enter key & test -> done): step navigation, the provider/API-key/model/
 * base-URL draft, connection testing, and the final save that marks setup
 * complete in localStorage. The actual wizard UI lives in
 * `../components/SetupWizard` (owned by another worker); this hook only
 * carries state and side effects.
 *
 * Backend note: `POST /api/providers/test` probes the SAVED config
 * (see `test_connection` in agent/src/providers/manager.py), so
 * `testConnection()` persists the current draft before testing.
 */
import { useCallback, useMemo, useState } from "react";
import {
  saveProviderConfig,
  testProviderConnection,
} from "../lib/api";

/** localStorage flag marking the first-run setup as done. */
export const SETUP_COMPLETE_KEY = "boom.setupComplete";

export const SETUP_FIRST_STEP = 1;
export const SETUP_LAST_STEP = 4;

/** Provider ids that need no API key (local runtimes). */
const KEYLESS_PROVIDERS: ReadonlySet<string> = new Set(["ollama"]);

/** Connection-test outcome shown on step 3. */
export type SetupTestResult = "idle" | "ok" | "error";

export interface UseSetupWizardApi {
  /* Draft state */
  step: number;
  provider: string | null;
  apiKey: string;
  model: string;
  baseUrl: string;
  /* Async state */
  testing: boolean;
  testResult: SetupTestResult;
  testError: string | null;
  /** Backend-provided message from the last test (shown on ok and error). */
  testMessage: string | null;
  saving: boolean;
  saveError: string | null;
  /* Derived */
  canGoNext: boolean;
  /* Actions */
  next: () => void;
  back: () => void;
  selectProvider: (id: string) => void;
  setApiKey: (key: string) => void;
  setModel: (model: string) => void;
  setBaseUrl: (url: string) => void;
  testConnection: () => Promise<void>;
  saveAndComplete: () => Promise<boolean>;
  reset: () => void;
}

/** Whether the current provider needs an API key at all. */
function providerNeedsKey(provider: string | null): boolean {
  return provider !== null && !KEYLESS_PROVIDERS.has(provider);
}

export function useSetupWizard(): UseSetupWizardApi {
  const [step, setStep] = useState<number>(SETUP_FIRST_STEP);
  const [provider, setProvider] = useState<string | null>(null);
  const [apiKey, setApiKeyState] = useState<string>("");
  const [model, setModelState] = useState<string>("");
  const [baseUrl, setBaseUrlState] = useState<string>("");

  const [testing, setTesting] = useState<boolean>(false);
  const [testResult, setTestResult] = useState<SetupTestResult>("idle");
  const [testError, setTestError] = useState<string | null>(null);
  const [testMessage, setTestMessage] = useState<string | null>(null);

  const [saving, setSaving] = useState<boolean>(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const next = useCallback(() => {
    setStep((s) => Math.min(SETUP_LAST_STEP, s + 1));
  }, []);

  const back = useCallback(() => {
    setStep((s) => Math.max(SETUP_FIRST_STEP, s - 1));
  }, []);

  /** A previous test result is stale once the provider changes. */
  const resetTestState = useCallback(() => {
    setTestResult("idle");
    setTestError(null);
    setTestMessage(null);
  }, []);

  const selectProvider = useCallback(
    (id: string) => {
      setProvider(id);
      resetTestState();
    },
    [resetTestState],
  );

  /** A previous test result is stale once the key changes. */
  const setApiKey = useCallback(
    (key: string) => {
      setApiKeyState(key);
      resetTestState();
    },
    [resetTestState],
  );

  const setModel = useCallback((nextModel: string) => {
    setModelState(nextModel);
  }, []);

  const setBaseUrl = useCallback((url: string) => {
    setBaseUrlState(url);
  }, []);

  /**
   * Persist the current draft, then probe the connection. The backend can
   * only test what it has stored, so the save-then-test order is required.
   * Sets `testResult`/`testError` for the UI to render.
   */
  const testConnection = useCallback(async (): Promise<void> => {
    if (provider === null) {
      setTestResult("error");
      setTestError("Select a provider first.");
      return;
    }
    if (testing) return;
    setTesting(true);
    resetTestState();
    try {
      await saveProviderConfig(provider, {
        apiKey: apiKey || undefined,
        model: model || undefined,
        baseUrl: baseUrl || undefined,
      });
      const { ok, message } = await testProviderConnection(provider);
      setTestResult(ok ? "ok" : "error");
      setTestError(ok ? null : (message ?? "Connection test failed."));
      setTestMessage(message ?? null);
    } catch (err) {
      setTestResult("error");
      setTestError(
        err instanceof Error ? err.message : "Connection test failed.",
      );
    } finally {
      setTesting(false);
    }
  }, [provider, apiKey, model, baseUrl, testing, resetTestState]);

  /**
   * Persist the draft and mark setup complete. Returns false (and sets
   * `saveError`) when the backend save fails — the completion flag is only
   * written on success so a failed save re-opens the wizard next launch.
   */
  const saveAndComplete = useCallback(async (): Promise<boolean> => {
    if (provider === null || saving) return false;
    setSaving(true);
    setSaveError(null);
    try {
      await saveProviderConfig(provider, {
        apiKey: apiKey || undefined,
        model: model || undefined,
        baseUrl: baseUrl || undefined,
      });
    } catch (err) {
      setSaveError(
        err instanceof Error ? err.message : "Failed to save provider config.",
      );
      setSaving(false);
      return false;
    }
    setSaving(false);
    try {
      window.localStorage.setItem(SETUP_COMPLETE_KEY, "true");
    } catch {
      // Storage unavailable (private mode): the wizard still completes for
      // this session; WelcomeGate falls back to in-memory state.
    }
    return true;
  }, [provider, apiKey, model, baseUrl, saving]);

  const reset = useCallback(() => {
    setStep(SETUP_FIRST_STEP);
    setProvider(null);
    setApiKeyState("");
    setModelState("");
    setBaseUrlState("");
    setTesting(false);
    setTestMessage(null);
    resetTestState();
    setSaving(false);
    setSaveError(null);
  }, [resetTestState]);

  /**
   * Step gates:
   *  1 (welcome) — always passable.
   *  2 (provider) — a provider must be chosen.
   *  3 (key & test) — keyless providers (Ollama) pass immediately;
   *     keyed providers require a successful connection test.
   *  4 (done) — always passable.
   */
  const canGoNext = useMemo((): boolean => {
    switch (step) {
      case 1:
        return true;
      case 2:
        return provider !== null;
      case 3:
        return (
          provider !== null &&
          (!providerNeedsKey(provider) || testResult === "ok")
        );
      case 4:
        return true;
      default:
        return false;
    }
  }, [step, provider, testResult]);

  return {
    step,
    provider,
    apiKey,
    model,
    baseUrl,
    testing,
    testResult,
    testError,
    testMessage,
    saving,
    saveError,
    canGoNext,
    next,
    back,
    selectProvider,
    setApiKey,
    setModel,
    setBaseUrl,
    testConnection,
    saveAndComplete,
    reset,
  };
}
