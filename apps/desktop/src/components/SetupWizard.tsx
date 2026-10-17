/**
 * SetupWizard — first-run onboarding for Boom desktop.
 *
 * A full-screen, 2-step wizard:
 *   1. Welcome — logo, greeting, "Get Started"
 *   2. Done    — "Ready!" + how to add an API key later in Settings
 *
 * Provider/API-key configuration is intentionally NOT part of onboarding:
 * users add keys any time in Settings, and the chat view shows a gentle
 * inline prompt when a message is sent without a configured provider.
 *
 * Dark theme, indigo accent, animations from ../styles/effects.css
 * (mesh-bg, tab-fade--once, glow-hover).
 */
import { ArrowLeft, ArrowRight, Sparkles } from "lucide-react";
import { useSetupWizard } from "../hooks/useSetupWizard";
import "../styles/effects.css";

export function SetupWizard({ onComplete }: { onComplete: () => void }) {
  // All wizard state lives in the shared hook (single source of truth).
  const wz = useSetupWizard();
  // Hook steps are 1-based; the UI below renders them 0-based.
  const step = wz.step - 1;

  const stepNames = ["Welcome", "Done"];

  async function handleFinish(): Promise<void> {
    const ok = await wz.saveAndComplete();
    if (ok) onComplete();
  }

  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-y-auto bg-[var(--bg)] px-4 py-10">
      {/* Gradient mesh background (effects.css, compositor-only). */}
      <div className="mesh-bg" aria-hidden="true">
        <span className="mesh-blob mesh-blob--a"></span>
        <span className="mesh-blob mesh-blob--b"></span>
        <span className="mesh-blob mesh-blob--c"></span>
      </div>

      <div className="relative z-10 w-full max-w-2xl">
        {/* ---- Progress header ---- */}
        <div className="mb-6">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-xs font-medium uppercase tracking-widest text-[var(--muted)]">
              Step {step + 1} of 2
            </span>
            <span className="text-xs font-medium text-[var(--text)]">
              {stepNames[step]}
            </span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-[var(--surface-2)]">
            <div
              className="h-full rounded-full bg-[var(--accent)] transition-all duration-500 ease-out"
              style={{ width: `${((step + 1) / 2) * 100}%` }}
            />
          </div>
          <div className="mt-3 flex justify-between">
            {stepNames.map((name, i) => (
              <span
                key={name}
                className={`text-[11px] font-medium transition-colors duration-300 ${
                  i < step
                    ? "text-[var(--muted)]"
                    : i === step
                      ? "text-[var(--text)]"
                      : "text-[var(--muted)]"
                }`}
              >
                {name}
              </span>
            ))}
          </div>
        </div>

        {/* ---- Step card ---- */}
        <div
          key={step}
          className="tab-fade--once rounded-2xl border border-[var(--border)] bg-[color-mix(in_srgb,var(--surface)_90%,transparent)] p-8 shadow-2xl shadow-black/50 backdrop-blur"
        >
          {step === 0 && <WelcomeStep onStart={() => wz.next()} />}

          {step === 1 && <DoneStep onFinish={() => void handleFinish()} saving={wz.saving} />}
        </div>

        {/* ---- Nav buttons ---- */}
        {step === 1 && (
          <div className="mt-6 flex justify-start">
            <button
              type="button"
              onClick={() => wz.back()}
              className="inline-flex items-center gap-2 rounded-lg border border-[var(--border)] bg-[var(--surface)] px-4 py-2.5 text-sm font-medium text-[var(--text)] hover:bg-[var(--surface-2)]"
            >
              <ArrowLeft className="h-4 w-4" />
              Back
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

/** Step 1: logo, greeting, one-line description, "Get Started" CTA. */
function WelcomeStep({ onStart }: { onStart: () => void }) {
  return (
    <div className="py-8 text-center">
      <div className="mx-auto mb-6 flex h-20 w-20 items-center justify-center rounded-3xl bg-[var(--surface-2)] shadow-lg shadow-black/50">
        <Sparkles className="h-10 w-10 text-[var(--text)]" />
      </div>
      <h1 className="mb-2 text-4xl font-extrabold text-[var(--text)]">
        Welcome to Boom
      </h1>
      <p className="mx-auto mb-8 max-w-sm text-sm text-[var(--muted)]">
        Your autonomous multi-channel AI agent — add an API key in Settings
        any time to connect it to a model.
      </p>
      <button
        type="button"
        onClick={onStart}
        className="glow-hover inline-flex items-center gap-2 rounded-xl bg-[var(--accent)] px-8 py-3 text-base font-semibold text-[var(--bg)]"
      >
        Get Started
        <ArrowRight className="h-5 w-5" />
      </button>
    </div>
  );
}

/** Step 2: ready state — no provider step, keys are added later in Settings. */
function DoneStep({
  onFinish,
  saving,
}: {
  onFinish: () => void;
  saving: boolean;
}) {
  return (
    <div className="py-6 text-center">
      <div className="mx-auto mb-5 flex h-20 w-20 items-center justify-center rounded-3xl bg-[var(--surface-2)] text-4xl shadow-lg shadow-black/50">
        🎉
      </div>
      <h2 className="mb-2 text-3xl font-bold text-[var(--text)]">You&apos;re all set!</h2>
      <p className="mx-auto mb-8 max-w-sm text-sm text-[var(--muted)]">
        Add an API key any time in Settings so Boom can start chatting with AI.
      </p>
      <button
        type="button"
        onClick={onFinish}
        disabled={saving}
        className="glow-hover inline-flex items-center gap-2 rounded-xl bg-[var(--accent)] px-8 py-3 text-base font-semibold text-[var(--bg)] disabled:opacity-50"
      >
        Start using Boom
        <ArrowRight className="h-5 w-5" />
      </button>
    </div>
  );
}
