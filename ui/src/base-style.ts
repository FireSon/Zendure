// The font-face + theme custom-property layer that index.html inlines for the standalone app.
// Inside HA there is no index.html, so the panel entry injects the same CSS into the document,
// pointing the @font-face urls at the integration's static path. Mirror of index.html <style>.

function css(fontsBase: string): string {
  const face = (family: string, weight: number, file: string) =>
    `@font-face{font-family:'${family}';font-style:normal;font-weight:${weight};font-display:swap;src:url('${fontsBase}/${file}') format('woff2');}`;
  return [
    face('Fredoka', 400, 'fredoka-400.woff2'),
    face('Fredoka', 500, 'fredoka-500.woff2'),
    face('Fredoka', 600, 'fredoka-600.woff2'),
    face('Fredoka', 700, 'fredoka-700.woff2'),
    face('Nunito Sans', 400, 'nunito-sans-400.woff2'),
    face('Nunito Sans', 600, 'nunito-sans-600.woff2'),
    face('Nunito Sans', 700, 'nunito-sans-700.woff2'),
    `html[data-zm-theme="light"]{--zm-bg:var(--primary-background-color,#f4f1ea);--zm-card:var(--card-background-color,#ffffff);--zm-card-2:#fbf9f4;--zm-border:#ece6db;--zm-border-2:#f1ece3;--zm-text:var(--primary-text-color,#34322d);--zm-text-dim:var(--secondary-text-color,#8d877b);--zm-text-faint:#b3ada1;--zm-orange-soft:#fceadb;--zm-teal-soft:#dff3ee;--zm-amber-soft:#fdf0d9;--zm-blue-soft:#e1f1f8;--zm-tabbar:#f5f1e8;--zm-scheme:light;--zm-zebra:#f8f5ef;--zm-zebra-tile:#f1ebe0;--zm-ctl-a:#eaf7f3;--zm-ctl-b:#d4efe6;--zm-ctl-border:#c9e9df;--zm-shadow:0 1px 2px rgba(70,52,24,0.05),0 10px 30px rgba(70,52,24,0.07);--zm-shadow-sm:0 1px 2px rgba(70,52,24,0.05),0 4px 14px rgba(70,52,24,0.05);}`,
    `html[data-zm-theme="dark"]{--zm-bg:var(--primary-background-color,#1b1916);--zm-card:var(--card-background-color,#262320);--zm-card-2:#2d2925;--zm-border:rgba(255,255,255,0.075);--zm-border-2:rgba(255,255,255,0.05);--zm-text:var(--primary-text-color,#f2eee7);--zm-text-dim:var(--secondary-text-color,#a79e90);--zm-text-faint:#6d655a;--zm-orange-soft:rgba(244,122,32,0.16);--zm-teal-soft:rgba(31,184,154,0.16);--zm-amber-soft:rgba(245,166,35,0.15);--zm-blue-soft:rgba(58,160,201,0.16);--zm-tabbar:#221f1c;--zm-scheme:dark;--zm-zebra:#2b2824;--zm-zebra-tile:#34302b;--zm-ctl-a:#262320;--zm-ctl-b:#21302b;--zm-ctl-border:rgba(31,184,154,0.15);--zm-shadow:0 1px 2px rgba(0,0,0,0.3),0 12px 30px rgba(0,0,0,0.4);--zm-shadow-sm:0 1px 2px rgba(0,0,0,0.25),0 5px 16px rgba(0,0,0,0.3);}`,
    `html[data-zm-lead="balanced"]{--zm-primary:#f47a20;--zm-primary-soft:var(--zm-orange-soft);--zm-secondary:#1fb89a;--zm-secondary-soft:var(--zm-teal-soft);}`,
    `html[data-zm-lead="orange"]{--zm-primary:#f47a20;--zm-primary-soft:var(--zm-orange-soft);--zm-secondary:#e8964a;--zm-secondary-soft:var(--zm-amber-soft);}`,
    `html[data-zm-lead="teal"]{--zm-primary:#1fb89a;--zm-primary-soft:var(--zm-teal-soft);--zm-secondary:#f47a20;--zm-secondary-soft:var(--zm-orange-soft);}`,
  ].join('');
}

export function installBaseStyle(fontsBase: string): void {
  if (document.getElementById('zm-base-style')) return;
  const el = document.createElement('style');
  el.id = 'zm-base-style';
  el.textContent = css(fontsBase);
  document.head.appendChild(el);
}
