/**
 * SetupWizard — first-run onboarding for Boom desktop.
 *
 * A full-screen, 4-step wizard:
 *   1. Welcome        — logo, greeting, "Get Started"
 *   2. Provider       — pick one of 7 providers (Ollama renders <OllamaSetup />)
 *   3. API key        — enter + test the key (skipped for Ollama, no key needed)
 *   4. Done           — "You're ready!" + "Start using Boom"
 *
 * Dark theme, indigo accent, animations from ../styles/effects.css
 * (mesh-bg, tab-fade--once, glow-hover, msg-enter).
 *
 * NOTE: ./OllamaSetup is authored by a sibling worker; the import below
 * resolves to apps/web/src/components/OllamaSetup.tsx.
 */
import { useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  CheckCircle2,
  ExternalLink,
  Eye,
  EyeOff,
  KeyRound,
  Loader2,
  Sparkles,
  XCircle,
} from "lucide-react";
import { OllamaSetup } from "./OllamaSetup";
import { useSetupWizard } from "../hooks/useSetupWizard";
import "../styles/effects.css";

/** One selectable provider card in the wizard. */
interface WizardProvider {
  /** Backend provider id (POST /api/providers/config, /test). */
  id: string;
  /** Display name. */
  name: string;
  /** Card icon (emoji, per design spec). */
  icon: string;
  /** Price tag shown on the card. */
  priceTag: "Paid" | "Free local";
  /** One-line description. */
  blurb: string;
  /** Whether an API key is required for this provider. */
  needsKey: boolean;
  /** Where to obtain an API key (opens in a new tab); null hides the link. */
  keyUrl: string | null;
  /** Suggested models — empty means the user types one freely. */
  models: string[];
  /** Whether the key step shows an extra Base URL field. */
  customBaseUrl: boolean;
  /** Shows the "Recommended for beginners" badge. */
  recommended?: boolean;
}

const PROVIDERS: WizardProvider[] = [
  {
    id: "openai",
    name: "OpenAI",
    icon: "🤖",
    priceTag: "Paid",
    blurb: "GPT-4o and the o-series reasoning models.",
    needsKey: true,
    keyUrl: "https://platform.openai.com/api-keys",
    models: ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini", "gpt-4.1"],
    customBaseUrl: false,
  },
  {
    id: "anthropic",
    name: "Anthropic",
    icon: "🧠",
    priceTag: "Paid",
    blurb: "Claude — strong reasoning and coding.",
    needsKey: true,
    keyUrl: "https://console.anthropic.com/settings/keys",
    models: ["claude-sonnet-4-5", "claude-haiku-4-5", "claude-opus-4-1"],
    customBaseUrl: false,
  },
  {
    id: "google",
    name: "Google (Gemini)",
    icon: "✨",
    priceTag: "Paid",
    blurb: "Gemini — generous free tier, fast responses.",
    needsKey: true,
    keyUrl: "https://aistudio.google.com/apikey",
    models: ["gemini-2.5-flash", "gemini-2.5-pro"],
    customBaseUrl: false,
  },
  {
    id: "xai",
    name: "xAI (Grok)",
    icon: "𝕏",
    priceTag: "Paid",
    blurb: "Grok — real-time answers with attitude.",
    needsKey: true,
    keyUrl: "https://console.x.ai",
    models: ["grok-4", "grok-3", "grok-3-mini"],
    customBaseUrl: false,
  },
  {
    id: "deepseek",
    name: "DeepSeek",
    icon: "🐋",
    priceTag: "Paid",
    blurb: "Deep reasoning at a fraction of the price.",
    needsKey: true,
    keyUrl: "https://platform.deepseek.com/api_keys",
    models: ["deepseek-chat", "deepseek-reasoner"],
    customBaseUrl: false,
  },
  {
    id: "ollama",
    name: "Ollama",
    icon: "🦙",
    priceTag: "Free local",
    blurb: "Run open models on your own machine — no key needed.",
    needsKey: false,
    keyUrl: null,
    models: [],
    customBaseUrl: false,
    recommended: true,
  },
  {
    id: "custom",
    name: "Custom",
    icon: "🔌",
    priceTag: "Paid",
    blurb: "Any OpenAI-compatible endpoint of your choice.",
    needsKey: true,
    keyUrl: null,
    models: [],
    customBaseUrl: true,
  },
];

type ConnState = "idle" | "testing" | "ok" | "fail";

/** Shared input styling (mirrors ProviderSettings). */
const inputCls =
  "w-full rounded-lg border border-neutral-700 bg-neutral-900 px-3 py-2.5 text-sm text-neutral-100 placeholder:text-neutral-500 focus:border-indigo-500 focus:outline-none";
const labelCls =
  "mb-1.5 block text-xs font-medium uppercase tracking-wide text-neutral-400";

export function SetupWizard({ onComplete }: { onComplete: () => void }) {
  // All wizard state lives in the shared hook (single source of truth).
  const wz = useSetupWizard();
  // Hook steps are 1-based; the UI below renders them 0-based.
  const step = wz.step - 1;

  const provider: WizardProvider | null =
    PROVIDERS.find((p) => p.id === wz.provider) ?? null;

  // "Show key" is pure UI chrome — it stays local to this component.
  const [showKey, setShowKey] = useState(false);

  // Map the hook's async state onto the pill/button states the UI renders.
  const conn: ConnState = wz.testing
    ? "testing"
    : wz.testResult === "ok"
      ? "ok"
      : wz.testResult === "error"
        ? "fail"
        : "idle";
  const connMessage: string | null = wz.testMessage;
  const error: string | null = wz.testError ?? wz.saveError;

  const { apiKey, model, baseUrl } = wz;

  const stepNames = [
    "Welcome",
    "Provider",
    provider?.id === "ollama" ? "Local setup" : "API key",
    "Done",
  ];

  /** Pick a provider and reset the key step to a clean slate. */
  function selectProvider(p: WizardProvider): void {
    wz.selectProvider(p.id);
    wz.setApiKey("");
    wz.setModel(p.models[0] ?? "");
    wz.setBaseUrl("");
    setShowKey(false);
  }

  /** Save the key to the backend, then test the saved config. */
  function handleTest(): void {
    void wz.testConnection();
  }

  async function handleFinish(): Promise<void> {
    const ok = await wz.saveAndComplete();
    if (ok) onComplete();
  }

  return (
    <div className="relative flex min-h-screen items-center justify-center overflow-y-auto bg-neutral-950 px-4 py-10">
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
            <span className="text-xs font-medium uppercase tracking-widest text-neutral-500">
              Step {step + 1} of 4
            </span>
            <span className="text-xs font-medium text-indigo-300">
              {stepNames[step]}
            </span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-neutral-800">
            <div
              className="h-full rounded-full bg-gradient-to-r from-indigo-500 via-sky-500 to-emerald-500 transition-all duration-500 ease-out"
              style={{ width: `${((step + 1) / 4) * 100}%` }}
            />
          </div>
          <div className="mt-3 flex justify-between">
            {stepNames.map((name, i) => (
              <span
                key={name}
                className={`text-[11px] font-medium transition-colors duration-300 ${
                  i < step
                    ? "text-emerald-400"
                    : i === step
                      ? "text-neutral-100"
                      : "text-neutral-600"
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
          className="tab-fade--once rounded-2xl border border-neutral-800 bg-neutral-900/90 p-8 shadow-2xl shadow-black/50 backdrop-blur"
        >
          {step === 0 && <WelcomeStep onStart={() => wz.next()} />}

          {step === 1 && (
            <div>
              <h2 className="mb-1 text-2xl font-bold text-neutral-100">
                Choose your provider
              </h2>
              <p className="mb-6 text-sm text-neutral-400">
                Pick where Boom gets its intelligence. You can change this
                later in Settings.
              </p>
              <div className="grid gap-3 sm:grid-cols-2">
                {PROVIDERS.map((p) => {
                  const selected = p.id === wz.provider;
                  return (
                    <button
                      key={p.id}
                      type="button"
                      onClick={() => selectProvider(p)}
                      className={`glow-hover relative rounded-xl border p-4 text-left transition-colors ${
                        selected
                          ? "border-indigo-500 bg-indigo-950/40"
                          : "border-neutral-800 bg-neutral-900 hover:border-neutral-600"
                      }`}
                    >
                      {selected && (
                        <span className="absolute right-3 top-3 flex h-5 w-5 items-center justify-center rounded-full bg-indigo-500">
                          <Check className="h-3.5 w-3.5 text-white" />
                        </span>
                      )}
                      <div className="mb-2 flex items-center gap-2">
                        <span className="text-2xl" aria-hidden="true">
                          {p.icon}
                        </span>
                        <span className="font-semibold text-neutral-100">
                          {p.name}
                        </span>
                      </div>
                      <p className="mb-3 min-h-10 text-xs text-neutral-400">
                        {p.blurb}
                      </p>
                      <div className="flex flex-wrap gap-1.5">
                        <span
                          className={`rounded-full border px-2 py-0.5 text-[11px] font-medium ${
                            p.priceTag === "Free local"
                              ? "border-emerald-800 bg-emerald-950 text-emerald-300"
                              : "border-neutral-700 bg-neutral-800 text-neutral-400"
                          }`}
                        >
                          {p.priceTag}
                        </span>
                        {p.recommended && (
                          <span className="rounded-full border border-amber-800 bg-amber-950 px-2 py-0.5 text-[11px] font-medium text-amber-300">
                            Recommended for beginners
                          </span>
                        )}
                      </div>
                    </button>
                  );
                })}
              </div>
            </div>
          )}

          {step === 2 && provider?.id === "ollama" && (
            <div>
              <h2 className="mb-1 text-2xl font-bold text-neutral-100">
                Local setup
              </h2>
              <p className="mb-6 text-sm text-neutral-400">
                Ollama runs models on your machine — no API key needed.
              </p>
              <OllamaSetup />
            </div>
          )}

          {step === 2 && provider && provider.id !== "ollama" && (
            <div>
              <h2 className="mb-1 text-2xl font-bold text-neutral-100">
                Connect {provider.name}
              </h2>
              <p className="mb-6 text-sm text-neutral-400">
                Paste your API key below. It is stored only on this machine.
              </p>

              <div className="mb-4">
                <label className={labelCls} htmlFor="wizard-key">
                  API key
                </label>
                <div className="relative">
                  <KeyRound className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-neutral-500" />
                  <input
                    id="wizard-key"
                    type={showKey ? "text" : "password"}
                    value={apiKey}
                    onChange={(e) => wz.setApiKey(e.target.value)}
                    placeholder="Paste your API key"
                    autoComplete="off"
                    spellCheck={false}
                    className={`${inputCls} pl-9 pr-10`}
                  />
                  <button
                    type="button"
                    onClick={() => setShowKey((v) => !v)}
                    className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-neutral-500 hover:text-neutral-300"
                    aria-label={showKey ? "Hide API key" : "Show API key"}
                  >
                    {showKey ? (
                      <EyeOff className="h-4 w-4" />
                    ) : (
                      <Eye className="h-4 w-4" />
                    )}
                  </button>
                </div>
                {provider.keyUrl && (
                  <a
                    href={provider.keyUrl}
                    target="_blank"
                    rel="noreferrer"
                    className="mt-2 inline-flex items-center gap-1 text-xs text-indigo-400 hover:text-indigo-300"
                  >
                    Where to get a key?
                    <ExternalLink className="h-3 w-3" />
                  </a>
                )}
              </div>

              {provider.models.length > 0 && (
                <div className="mb-4">
                  <label className={labelCls} htmlFor="wizard-model">
                    Model
                  </label>
                  <select
                    id="wizard-model"
                    value={model}
                    onChange={(e) => wz.setModel(e.target.value)}
                    className={inputCls}
                  >
                    {provider.models.map((m) => (
                      <option key={m} value={m}>
                        {m}
                      </option>
                    ))}
                  </select>
                </div>
              )}
              {provider.models.length === 0 && (
                <div className="mb-4">
                  <label className={labelCls} htmlFor="wizard-model">
                    Model
                  </label>
                  <input
                    id="wizard-model"
                    type="text"
                    value={model}
                    onChange={(e) => wz.setModel(e.target.value)}
                    placeholder="Model name (e.g. openai/gpt-4o-mini)"
                    autoComplete="off"
                    spellCheck={false}
                    className={inputCls}
                  />
                </div>
              )}

              {provider.customBaseUrl && (
                <div className="mb-4">
                  <label className={labelCls} htmlFor="wizard-base-url">
                    Base URL
                  </label>
                  <input
                    id="wizard-base-url"
                    type="text"
                    value={baseUrl}
                    onChange={(e) => wz.setBaseUrl(e.target.value)}
                    placeholder="https://your-endpoint/v1"
                    autoComplete="off"
                    spellCheck={false}
                    className={inputCls}
                  />
                </div>
              )}

              {connMessage && conn !== "idle" && conn !== "testing" && (
                <p
                  className={`mb-3 text-xs ${
                    conn === "ok" ? "text-emerald-400" : "text-red-400"
                  }`}
                >
                  {connMessage}
                </p>
              )}
              {error && (
                <p className="mb-3 rounded-lg border border-red-900 bg-red-950 px-3 py-2 text-xs text-red-300">
                  {error}
                </p>
              )}

              <div className="flex items-center gap-3">
                <button
                  type="button"
                  onClick={() => void handleTest()}
                  disabled={
                    conn === "testing" ||
                    (provider.needsKey && apiKey.trim().length === 0)
                  }
                  className="glow-hover inline-flex items-center gap-2 rounded-lg border border-neutral-700 bg-neutral-800 px-4 py-2.5 text-sm font-medium text-neutral-100 hover:bg-neutral-700 disabled:opacity-50 disabled:hover:bg-neutral-800"
                >
                  {conn === "testing" ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : conn === "ok" ? (
                    <CheckCircle2 className="h-4 w-4 text-emerald-400" />
                  ) : conn === "fail" ? (
                    <XCircle className="h-4 w-4 text-red-400" />
                  ) : null}
                  Test connection
                </button>
                {conn === "ok" && (
                  <span className="msg-enter inline-flex items-center gap-1.5 rounded-full border border-emerald-800 bg-emerald-950 px-2.5 py-1 text-xs text-emerald-300">
                    <CheckCircle2 className="h-3.5 w-3.5" />
                    Connected
                  </span>
                )}
                {conn === "fail" && (
                  <span className="msg-enter inline-flex items-center gap-1.5 rounded-full border border-red-800 bg-red-950 px-2.5 py-1 text-xs text-red-300">
                    <XCircle className="h-3.5 w-3.5" />
                    Failed
                  </span>
                )}
              </div>
            </div>
          )}

          {step === 3 && (
            <div className="py-6 text-center">
              <div className="mx-auto mb-5 flex h-20 w-20 items-center justify-center rounded-3xl bg-gradient-to-br from-indigo-500 via-sky-500 to-emerald-500 text-4xl shadow-lg shadow-indigo-950/60">
                🎉
              </div>
              <h2 className="mb-2 text-3xl font-bold text-neutral-100">
                You&apos;re ready!
              </h2>
              <p className="mx-auto mb-8 max-w-sm text-sm text-neutral-400">
                {provider
                  ? `Boom is connected to ${provider.name}${
                      provider.needsKey && model ? ` (${model})` : ""
                    }. Time to put it to work.`
                  : "Boom is set up. Time to put it to work."}
              </p>
              <button
                type="button"
                onClick={() => void handleFinish()}
                disabled={wz.saving}
                className="glow-hover inline-flex items-center gap-2 rounded-xl bg-gradient-to-r from-indigo-600 to-sky-600 px-8 py-3 text-base font-semibold text-white"
              >
                Start using Boom
                <ArrowRight className="h-5 w-5" />
              </button>
            </div>
          )}
        </div>

        {/* ---- Nav buttons ---- */}
        {(step === 1 || step === 2) && (
          <div className="mt-6 flex items-center justify-between">
            <button
              type="button"
              onClick={() => wz.back()}
              className="inline-flex items-center gap-2 rounded-lg border border-neutral-700 bg-neutral-900 px-4 py-2.5 text-sm font-medium text-neutral-300 hover:bg-neutral-800"
            >
              <ArrowLeft className="h-4 w-4" />
              Back
            </button>
            <button
              type="button"
              onClick={() => wz.next()}
              disabled={!wz.canGoNext}
              className="glow-hover inline-flex items-center gap-2 rounded-lg bg-gradient-to-r from-indigo-600 to-sky-600 px-6 py-2.5 text-sm font-semibold text-white disabled:cursor-not-allowed disabled:from-neutral-700 disabled:to-neutral-700 disabled:opacity-50"
            >
              Next
              <ArrowRight className="h-4 w-4" />
            </button>
          </div>
        )}
        {step === 3 && (
          <div className="mt-6 flex justify-start">
            <button
              type="button"
              onClick={() => wz.back()}
              className="inline-flex items-center gap-2 rounded-lg border border-neutral-700 bg-neutral-900 px-4 py-2.5 text-sm font-medium text-neutral-300 hover:bg-neutral-800"
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
      <div className="mx-auto mb-6 flex h-20 w-20 items-center justify-center rounded-3xl bg-gradient-to-br from-indigo-500 via-sky-500 to-emerald-500 shadow-lg shadow-indigo-950/60">
        <Sparkles className="h-10 w-10 text-white" />
      </div>
      <h1 className="mb-2 bg-gradient-to-r from-indigo-300 via-sky-300 to-emerald-300 bg-clip-text text-4xl font-extrabold text-transparent">
        Welcome to Boom
      </h1>
      <p className="mx-auto mb-8 max-w-sm text-sm text-neutral-400">
        Your autonomous multi-channel AI agent — let&apos;s connect it to a
        model in under a minute.
      </p>
      <button
        type="button"
        onClick={onStart}
        className="glow-hover inline-flex items-center gap-2 rounded-xl bg-gradient-to-r from-indigo-600 to-sky-600 px-8 py-3 text-base font-semibold text-white"
      >
        Get Started
        <ArrowRight className="h-5 w-5" />
      </button>
    </div>
  );
}
