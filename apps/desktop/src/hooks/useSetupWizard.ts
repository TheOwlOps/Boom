/**
 * First-run setup wizard state machine.
 *
 * Owns the 2-step onboarding flow (welcome -> done): step navigation and the
 * final save that marks setup complete in localStorage. Provider/API-key
 * configuration is intentionally NOT part of the wizard — users add keys any
 * time in Settings, and the chat view shows a gentle inline prompt when a
 * message is sent without a configured provider (see ChatView).
 */
import { useCallback, useState } from "react";

/** localStorage flag marking the first-run setup as done. */
export const SETUP_COMPLETE_KEY = "boom.setupComplete";

export const SETUP_FIRST_STEP = 1;
export const SETUP_LAST_STEP = 2;

export interface UseSetupWizardApi {
  step: number;
  saving: boolean;
  next: () => void;
  back: () => void;
  saveAndComplete: () => Promise<boolean>;
  reset: () => void;
}

export function useSetupWizard(): UseSetupWizardApi {
  const [step, setStep] = useState<number>(SETUP_FIRST_STEP);
  const [saving, setSaving] = useState<boolean>(false);

  const next = useCallback(() => {
    setStep((s) => Math.min(SETUP_LAST_STEP, s + 1));
  }, []);

  const back = useCallback(() => {
    setStep((s) => Math.max(SETUP_FIRST_STEP, s - 1));
  }, []);

  /**
   * Mark setup complete and return true. The wizard collects nothing, so
   * there is no backend call — the completion flag is the only write.
   * When storage is unavailable (private mode) the flag is skipped but the
   * wizard still completes in-memory via WelcomeGate's onComplete.
   */
  const saveAndComplete = useCallback(async (): Promise<boolean> => {
    if (saving) return false;
    setSaving(true);
    try {
      window.localStorage.setItem(SETUP_COMPLETE_KEY, "true");
    } catch {
      // Storage unavailable: WelcomeGate flips to in-memory state on
      // onComplete, so this session still proceeds.
    }
    setSaving(false);
    return true;
  }, [saving]);

  const reset = useCallback(() => {
    setStep(SETUP_FIRST_STEP);
    setSaving(false);
  }, []);

  return { step, saving, next, back, saveAndComplete, reset };
}
