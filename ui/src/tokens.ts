import { css } from 'lit';

// Warm "ZenMaster" palette (orange + teal, Fredoka/Nunito). The theme-dependent
// custom properties (--zm-bg, --zm-card, --zm-primary, ...) are defined at the
// document level in index.html on html[data-zm-theme] / html[data-zm-lead] and
// inherit into every component's shadow root. Only the theme-agnostic constants
// and shared keyframes live here. ECharts canvas cannot read CSS vars, so chart
// colours are computed in JS (see chartColors in charts.ts).
export const tokens = css`
  :host {
    --zm-font-display: 'Fredoka', system-ui, sans-serif;
    --zm-font-body: 'Nunito Sans', system-ui, sans-serif;
    /* the three text sizes of the whole panel: labels and units, names and text, the main values */
    --zm-fs-s: 10px;
    --zm-fs-m: 13px;
    --zm-fs-l: 17px;
    --zm-radius: 12px;
    --zm-radius-lg: 20px;
    --zm-radius-pill: 999px;
    --zm-dur: 180ms;
    --zm-ease: cubic-bezier(0.2, 0.7, 0.3, 1);
  }
  * { box-sizing: border-box; }
  /* the list a select opens is drawn by the browser: tell it the theme, and give the options the card colours */
  select { color-scheme: var(--zm-scheme, light); }
  option { background: var(--zm-card); color: var(--zm-text); }
  @keyframes zm-rise {
    from { opacity: 0; transform: translateY(10px); }
    to { opacity: 1; transform: none; }
  }
  @keyframes zm-spin { to { transform: rotate(360deg); } }
  .reveal { animation: zm-rise 0.4s var(--zm-ease) both; }
  @media (prefers-reduced-motion: reduce) { .reveal { animation: none !important; } }

  .card {
    background: var(--zm-card);
    border: 1px solid var(--zm-border);
    border-radius: var(--zm-radius-lg);
    box-shadow: var(--zm-shadow);
  }
  .h-title {
    font-family: var(--zm-font-display);
    font-weight: 600;
    color: var(--zm-text);
  }
`;
