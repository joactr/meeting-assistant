# Design: Meeting Assistant

The locked system for the desktop UI (`apps/desktop`). Every Hallmark run reads this first and screens share it. Change it on purpose; the file is the rule.

## System
- Genre: editorial, set as a utilitarian tool
- Shape: app screen (top bar, one sheet per route)
- Theme: Almanac (deal 2/3)
- Axes: mid/grotesk-sans/chromatic-magenta · posture: restrained · modes: light+dark (`data-theme`, system follows the OS)

## Tokens
`apps/desktop/src/styles/tokens.css` is the source of truth. The roles:
```css
:root {
  --color-paper: oklch(84% 0.012 245);  --color-paper-2: oklch(81% 0.012 245);  --color-paper-3: oklch(77% 0.012 245);
  --color-sheet: oklch(93.5% 0.008 245);  --color-sheet-2: oklch(90% 0.010 245);
  --color-ink: oklch(16% 0.020 245);  --color-ink-2: oklch(24% 0.018 245);  --color-muted: oklch(38% 0.016 245);
  --color-rule: oklch(80% 0.013 240);  --color-rule-2: oklch(32% 0.018 240);
  --color-accent: oklch(45% 0.180 350);  --color-accent-ink: oklch(97% 0.006 245);  --color-focus: oklch(46% 0.190 350);
  --color-error, --color-success, --color-warning: state text only, never a fill;
  --font-display: "Hanken Grotesk Variable";  --font-body: "Newsreader Variable";  --font-outlier: "IBM Plex Mono" (figures and times only);
  --radius-card: 0;  --radius-pill: 2px;  --rule-hair: 1px;  --rule-bar: 2px;  --shadow-card: none;
  --control: 36px;  --bar-height: 56px;
}
```
Spacing, type scale (1.2 from 16px), easings and durations live in `tokens.css`. No literal colour, font or length outside it.

## Surfaces
- Slate `paper` is the chrome (top bar). Each route renders on the pale `sheet`; a well is `sheet-2`. Nothing else is a surface.
- Hairline `rule` parts rows; one 2px `rule-2` line under a page head or a table head. No boxed cards, no shadows, no card inside a card.
- Heads in Hanken Grotesk 600, sentence case. Reading text (transcript, minutes, summaries) in Newsreader at 16px or larger. Labels and table heads in Newsreader 500 a step smaller, muted. Times and counts in Plex Mono, tabular.

## Controls
- Primary: magenta fill, accent-ink text, 2px radius, 36px tall. One per screen; dialogs count as their own screen.
- Secondary: transparent, 1px ink outline, same radius and height. Quiet: no outline, muted text.
- Danger: error outline and text; never filled.
- Accent is only for the primary action, the selected tab or nav link, focus and the live recording state.

## Copy
- Titles only: no subtitles, eyebrows or helper paragraphs under headings. Short plain English, sentence case.
- No dashes, no dot-joined meta, no internal names (cues, superseded, final_analysis). State appears as one plain word or line, and only when the user must act or wait.

## Motion
- Still. Only state changes move: colour steps at 120ms, the disclosure chevron at 220ms. Reduced motion ends everything at once.

## Exports
`tokens.css` in this project. Tailwind, DTCG or shadcn blocks are added here on request.
