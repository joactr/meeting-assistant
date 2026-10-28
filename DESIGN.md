# Design: Meeting Assistant

The locked system for the desktop UI (`apps/desktop`). Every Hallmark run reads this first and screens share it. Change it on purpose; the file is the rule.

## System
- Genre: atmospheric, set as a utilitarian tool
- Shape: app screen (top bar, one midnight ground per route)
- Theme: Midnight (deal 1/3)
- Axes: dark/grotesk-sans/cool-azure · posture: restrained · modes: dark by default, a light colourway for the Light setting and for Match Windows on a light system (`data-theme`)

## Tokens
`apps/desktop/src/styles/tokens.css` is the source of truth. The roles:
```css
:root {
  --color-paper: oklch(14% 0.048 264);  --color-paper-2: oklch(18.5% 0.052 264);  --color-paper-3: oklch(23% 0.055 264);
  --color-sheet: var(--color-paper-2);  --color-sheet-2: var(--color-paper-3);
  --color-ink: oklch(95% 0.016 250);  --color-ink-2: oklch(88% 0.05 245);  --color-muted: oklch(73% 0.04 256);
  --color-rule: oklch(29% 0.058 264);  --color-rule-2: oklch(40% 0.06 262);
  --color-accent: oklch(80% 0.11 230);  --color-accent-ink: oklch(16% 0.05 264);  --color-focus: oklch(84% 0.12 228);
  --color-error, --color-success, --color-warning: state text only, never a fill;
  --font-display, --font-body, --font-outlier: "Mona Sans Variable" (one family, two widths; no mono);
  --radius-card: 0;  --radius-pill: 0;  --rule-hair: 1px;  --shadow-card: lifted surfaces only;
  --display-weight: 300;  --display-stretch: 125%;
  --container: 1200px;  --control: 36px;  --bar-height: 56px;
}
```
Spacing, type scale (1.2 from 16px), easings and durations live in `tokens.css`. No literal colour, font or length outside it.

## Surfaces
- One midnight `paper` ground to every edge. The top bar, banners and page content share one column edge (`--edge` in `shell.module.css`) at every window width.
- A `sheet` is one lightness step up: the transcript pane, dialogs, menus. `sheet-2` is the step above it: selected rows, notices, hover. Dialogs and menus take `--shadow-card`; nothing is outlined.
- Hairline `rule` parts rows; one hairline `rule-2` under a page head or a table head. No boxed cards, no card inside a card.
- Page and section heads (`h1`, `h2`) in Mona Sans 300 at 125% width. Item titles, speakers and `h3` at 500, normal width. Reading text at 400, 16px or larger. Labels and table heads 500 a step smaller, muted. Times and counts tabular.

## Controls
- Primary: azure fill, accent-ink text, square, 36px tall. One per screen; dialogs count as their own screen.
- Secondary: transparent, 1px `rule-2` outline, square. Quiet: no outline, muted text. Segmented: square, the chosen option filled ink.
- Inputs sit on the ground with a 1px muted edge, square.
- Danger: error outline and text; never filled.
- Accent is only for the primary action, the selected tab or nav link, focus, a link's hover and the live recording state.

## Layout
- Every screen reflows with the window: rows wrap their actions under the text, filter bars wrap, settings and grids fall to one column, the notes margin column drops under the record below 64rem, the brand hides below 48rem.

## Copy
- Titles only: no subtitles, eyebrows or helper paragraphs under headings. Short plain English, sentence case.
- No dashes, no dot-joined meta, no internal names (cues, superseded, final_analysis). State appears as one plain word or line, and only when the user must act or wait.

## Motion
- Still. Only state changes move: colour steps at 120ms, the disclosure chevron at 220ms. Reduced motion ends everything at once.

## Exports
`tokens.css` in this project. Tailwind, DTCG or shadcn blocks are added here on request.
