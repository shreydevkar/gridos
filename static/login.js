// Login page logic. Imports Supabase JS client from esm.sh as an ES module so
// we don't need a bundler. Handles sign-in, sign-up, password reset, and
// Google OAuth — Supabase stores the session in localStorage and refreshes
// access tokens automatically; app.js reads it on bootstrap.
import { createClient } from "https://esm.sh/@supabase/supabase-js@2?bundle";

const loadingState = document.getElementById("loading-state");
const authForm = document.getElementById("auth-form");
const formTitle = document.getElementById("form-title");
const formSub = document.getElementById("form-sub");
const modeTogglePrompt = document.getElementById("mode-toggle-prompt");
const modeToggleLink = document.getElementById("mode-toggle-link");
const primaryBtn = document.getElementById("primary-btn");
const googleBtn = document.getElementById("google-btn");
const msgEl = document.getElementById("msg");
const emailInput = document.getElementById("email");
const passwordInput = document.getElementById("password");
const emailForm = document.getElementById("email-form");
const forgotLink = document.getElementById("forgot-link");

let mode = "signin";  // "signin" | "signup"
let supabase = null;

function setMsg(kind, text) {
    msgEl.className = `msg ${kind}`;
    msgEl.textContent = text;
}

function clearMsg() {
    msgEl.className = "msg";
    msgEl.textContent = "";
}

function setMode(next) {
    mode = next;
    if (mode === "signin") {
        formTitle.textContent = "Welcome back";
        formSub.textContent = "Sign in to continue.";
        primaryBtn.textContent = "Sign in";
        modeTogglePrompt.textContent = "New to GridOS?";
        modeToggleLink.textContent = "Create an account";
        passwordInput.autocomplete = "current-password";
        forgotLink.style.display = "";
    } else {
        formTitle.textContent = "Create your account";
        formSub.textContent = "Six characters minimum.";
        primaryBtn.textContent = "Create account";
        modeTogglePrompt.textContent = "Already have an account?";
        modeToggleLink.textContent = "Sign in";
        passwordInput.autocomplete = "new-password";
        forgotLink.style.display = "none";
    }
    clearMsg();
}

async function bootstrap() {
    let status;
    try {
        const res = await fetch("/cloud/status");
        status = await res.json();
    } catch (e) {
        loadingState.textContent = "Could not reach the server.";
        return;
    }

    if (status.mode !== "saas") {
        // Running against an OSS build; no login required. Go home.
        window.location.replace("/");
        return;
    }

    const cfg = status.client_config || {};
    if (!cfg.supabase_url || !cfg.supabase_anon_key) {
        loadingState.textContent =
            "Supabase is not fully configured on the server. Missing SUPABASE_URL or SUPABASE_ANON_KEY.";
        return;
    }

    supabase = createClient(cfg.supabase_url, cfg.supabase_anon_key, {
        auth: {
            persistSession: true,
            autoRefreshToken: true,
            detectSessionInUrl: true,   // picks up tokens from OAuth redirect fragment
        },
    });

    // If Supabase already has a live session (maybe from a prior tab, or from
    // an in-flight OAuth callback), redirect home immediately.
    const { data: { session } } = await supabase.auth.getSession();
    if (session) {
        window.location.replace("/");
        return;
    }

    loadingState.style.display = "none";
    authForm.style.display = "block";
    emailInput.focus();
}

modeToggleLink.addEventListener("click", (e) => {
    e.preventDefault();
    setMode(mode === "signin" ? "signup" : "signin");
});

emailForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!supabase) return;
    clearMsg();
    primaryBtn.disabled = true;
    primaryBtn.textContent = mode === "signin" ? "Signing in…" : "Creating account…";

    const email = emailInput.value.trim();
    const password = passwordInput.value;

    try {
        if (mode === "signin") {
            const { error } = await supabase.auth.signInWithPassword({ email, password });
            if (error) throw error;
            window.location.replace("/");
        } else {
            const { data, error } = await supabase.auth.signUp({
                email,
                password,
                options: { emailRedirectTo: `${window.location.origin}/login` },
            });
            if (error) throw error;
            if (data.session) {
                window.location.replace("/");
            } else {
                // Email confirmation required — Supabase default.
                setMsg("success", "Check your inbox for a confirmation link to finish signing up.");
                primaryBtn.disabled = false;
                primaryBtn.textContent = "Create account";
            }
        }
    } catch (err) {
        setMsg("error", err?.message || "Sign-in failed.");
        primaryBtn.disabled = false;
        primaryBtn.textContent = mode === "signin" ? "Sign in" : "Create account";
    }
});

googleBtn.addEventListener("click", async () => {
    if (!supabase) return;
    clearMsg();
    googleBtn.disabled = true;
    try {
        const { error } = await supabase.auth.signInWithOAuth({
            provider: "google",
            options: { redirectTo: `${window.location.origin}/login` },
        });
        if (error) throw error;
        // Supabase handles the redirect; page will navigate away.
    } catch (err) {
        setMsg("error", err?.message || "Google sign-in failed. Is the provider enabled in Supabase?");
        googleBtn.disabled = false;
    }
});

forgotLink.addEventListener("click", async (e) => {
    e.preventDefault();
    if (!supabase) return;
    const email = emailInput.value.trim();
    if (!email) {
        setMsg("error", "Enter your email first, then click Forgot password.");
        emailInput.focus();
        return;
    }
    clearMsg();
    try {
        const { error } = await supabase.auth.resetPasswordForEmail(email, {
            redirectTo: `${window.location.origin}/login`,
        });
        if (error) throw error;
        setMsg("success", `Password reset link sent to ${email}.`);
    } catch (err) {
        setMsg("error", err?.message || "Could not send reset link.");
    }
});

bootstrap();
