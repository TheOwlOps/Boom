/**
 * First-run gate: new users see the setup wizard, everyone else sees the app.
 *
 * Completion is tracked by the `boom.setupComplete` localStorage flag (set
 * by `useSetupWizard.saveAndComplete()`). The flag is read once on mount;
 * after the wizard's `onComplete` fires, the gate flips to in-memory state
 * so the app renders without a reload.
 */
import { useCallback, useState, type ReactNode } from "react";
import { SetupWizard } from "./SetupWizard";
import { SETUP_COMPLETE_KEY } from "../hooks/useSetupWizard";

function hasCompletedSetup(): boolean {
  try {
    return window.localStorage.getItem(SETUP_COMPLETE_KEY) === "true";
  } catch {
    // Storage unavailable (private mode): fall back to showing the wizard;
    // completing it still works in-memory for this session.
    return false;
  }
}

export default function WelcomeGate({ children }: { children: ReactNode }) {
  const [completed, setCompleted] = useState<boolean>(hasCompletedSetup);

  const handleComplete = useCallback(() => {
    setCompleted(true);
  }, []);

  if (!completed) {
    return <SetupWizard onComplete={handleComplete} />;
  }
  return <>{children}</>;
}
