// Browser-only helpers for Google Identity Services (GIS) sign-in.

const GIS_SRC = "https://accounts.google.com/gsi/client";

function toHex(bytes: Uint8Array): string {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

/**
 * Supabase's `signInWithIdToken` needs the raw nonce; Google must receive its SHA-256 hex, which
 * it embeds in the ID token so Supabase can compare the two.
 */
export async function createNonce(): Promise<{ raw: string; hashed: string }> {
  const raw = toHex(crypto.getRandomValues(new Uint8Array(32)));
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(raw));
  return { raw, hashed: toHex(new Uint8Array(digest)) };
}

let scriptPromise: Promise<void> | undefined;

/** Injects the GIS script once; repeated calls share one promise. A failed load can be retried. */
export function loadGoogleScript(): Promise<void> {
  if (scriptPromise) return scriptPromise;
  scriptPromise = new Promise<void>((resolve, reject) => {
    const script = document.createElement("script");
    script.src = GIS_SRC;
    script.async = true;
    script.defer = true;
    script.onload = () => resolve();
    script.onerror = () => {
      script.remove();
      scriptPromise = undefined;
      reject(new Error("Could not load Google sign-in."));
    };
    document.head.appendChild(script);
  });
  return scriptPromise;
}

export type GoogleCredentialResponse = { credential: string; select_by?: string };

declare global {
  interface Window {
    google?: {
      accounts: {
        id: {
          initialize(config: {
            client_id: string;
            nonce?: string;
            callback: (response: GoogleCredentialResponse) => void;
            use_fedcm_for_prompt?: boolean;
          }): void;
          renderButton(
            parent: HTMLElement,
            options: {
              type?: "standard" | "icon";
              theme?: "outline" | "filled_blue" | "filled_black";
              size?: "large" | "medium" | "small";
              text?: "signin_with" | "signup_with" | "continue_with" | "signin";
              width?: number;
            },
          ): void;
          cancel(): void;
        };
      };
    };
  }
}
