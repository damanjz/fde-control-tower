---
name: Tarangini control tower
description: Evidence on paper, the decision on the machine side. One incident at a time, on a paper sheet over indigo.
colors:
  ground: "#16184a"
  card: "#24276b"
  paper: "#f3f4f8"
  slip: "#fafbfd"
  ink-on-indigo: "#eef0fb"
  soft-on-indigo: "#b3b8e0"
  muted-on-indigo: "#a9afdb"
  line-on-indigo: "#33367a"
  edge-on-indigo: "#6a70b8"
  marigold: "#ffb84d"
  mint: "#7fe0b5"
  deadline-on-indigo: "#ff9486"
  marigold-wash: "rgba(255, 184, 77, .16)"
  ink: "#14172b"
  soft: "#363b58"
  muted: "#565c78"
  line: "#d5d8e6"
  edge: "#8b90ab"
  agent: "#2b2f9e"
  amber-on-paper: "#a84f00"
  saved: "#0f7a58"
  deadline: "#c4262b"
  amber-wash: "#fbe6cf"
  map-surface: "#e4e6f0"
  map-coast: "#c4c8da"
typography:
  display-lg:
    fontFamily: "Anek Latin Variable, Anek Latin, Hanken Grotesk Variable, system-ui, sans-serif"
    fontSize: "2.95rem"
    fontWeight: 600
    lineHeight: 1.1
    fontVariation: "wdth 112"
  display-md:
    fontFamily: "Anek Latin Variable, Anek Latin, Hanken Grotesk Variable, system-ui, sans-serif"
    fontSize: "1.5rem"
    fontWeight: 600
    lineHeight: 1.25
    fontVariation: "wdth 112"
  title:
    fontFamily: "Anek Latin Variable, Anek Latin, Hanken Grotesk Variable, system-ui, sans-serif"
    fontSize: "1.15rem"
    fontWeight: 600
    lineHeight: 1.5
    fontVariation: "wdth 112"
  figure:
    fontFamily: "Anek Latin Variable, Anek Latin, Hanken Grotesk Variable, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 600
    lineHeight: 1
    fontVariation: "wdth 90"
  body:
    fontFamily: "Hanken Grotesk Variable, Hanken Grotesk, system-ui, sans-serif"
    fontSize: "1.15rem"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "Hanken Grotesk Variable, Hanken Grotesk, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.5
rounded:
  xs: ".2rem"
  sm: ".4rem"
  md: ".53rem"
  pill: ".93rem"
spacing:
  frame: "1.5rem"
  gap: "2.25rem"
  rail-w: "21.5rem"
  sheet-pad-x: "2.13rem"
  sheet-pad-top: "1.47rem"
components:
  button-primary:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ground}"
    typography: "{typography.title}"
    rounded: "{rounded.md}"
    padding: "0 1.33rem"
    height: "2.93rem"
  button-primary-hover:
    backgroundColor: "#ffffff"
  button-primary-disabled:
    backgroundColor: "transparent"
    textColor: "{colors.muted-on-indigo}"
  button-secondary:
    backgroundColor: "transparent"
    textColor: "{colors.ink-on-indigo}"
    typography: "{typography.label}"
    rounded: "{rounded.md}"
    padding: "0 1.07rem"
    height: "2.67rem"
  incident-tab:
    backgroundColor: "transparent"
    textColor: "{colors.ink-on-indigo}"
    rounded: "{rounded.sm}"
    padding: ".67rem .67rem .8rem"
  incident-tab-selected:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
  incident-tab-hover:
    backgroundColor: "{colors.card}"
  sheet:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "1.47rem 2.13rem 2.13rem"
  clause-slip:
    backgroundColor: "{colors.slip}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: ".67rem 1.2rem"
  fix-option:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    rounded: "{rounded.md}"
    padding: ".67rem .93rem"
  fix-option-checked:
    backgroundColor: "{colors.slip}"
  segmented:
    backgroundColor: "transparent"
    textColor: "{colors.muted-on-indigo}"
    rounded: "{rounded.md}"
    padding: ".13rem"
  segmented-checked:
    backgroundColor: "{colors.card}"
    textColor: "{colors.ink-on-indigo}"
  tooltip:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.paper}"
    rounded: "{rounded.sm}"
    padding: ".53rem .67rem"
---

# Design System: Tarangini control tower

## Overview

**Creative North Star: "Sheet on indigo"**

One paper sheet carries the evidence; the machine side (header, incident strip, decision rail, a slim frame) sits on a deep indigo ground. The sheet is the only lit surface and holds the only shadow. Inside it, structure is lines, not boxes. The recruiter reads one incident top to bottom (caught, priced, contract, decision), then approves in the rail.

Decisions, all by Daman (owner):
- 2026-11-17: layout A, "single case", picked over a ledger (`b-ledger`) and a map-led layout (`c-map`).
- 2026-11-18: the world "sheet on indigo", picked over "dusk room" and "morning desk".
- First review of the build: "too much indigo, zoomed out, small text, no interactivity". It led to fluid rem scaling (root = 1/96 of the viewport), a slim indigo frame over a deeper ground (`#16184a`), and five interactions (see Interactions).
- Seed key in the direction contract (`web/app/layout.tsx`): "daman-pick-A3". Approved mockup: `.design/v2/a-indigo.html`.

**Key Characteristics:**
- Paper dominates; indigo is header, strip, rail and a slim frame (`--frame` 1.5rem).
- One shadow in the whole UI (the sheet). Everything inside is hairlines.
- Money is the loudest type: marigold on the indigo, tabular figures, Indian grouping (Rs 1,73,622).
- Honest machine: elapsed times and step counts are shown as recorded, never faked.

## Colors

Two token sets with the same names. The `:root` set is for text on the indigo; `.paper` redefines them for text on the sheet. In the frontmatter, unsuffixed ink, soft, muted, line, edge, agent, saved and deadline keys are the paper values; `-on-indigo` keys are the root values.

### Primary
- **Deep Indigo Ground** (`#16184a`): page background, theme-color, the phone bottom bar, text on the primary button.
- **Raised Indigo** (`#24276b`): hover and selected fills on the indigo (incident tab hover, selected "View as" segment).
- **Agent Indigo** (`#2b2f9e`, on paper): the agent's mark: checked radio, focus ring, DC highlight on the map. On the indigo, `--agent` is the ink colour `#eef0fb`.

### Secondary
- **Marigold** (`#ffb84d`, on the indigo): rupees at risk, the pending status icon, risk bars, the word "same" in the preview. On paper it darkens to Amber (`#a84f00`).
- **Mint** (`#7fe0b5` on indigo, `#0f7a58` on paper): money saved, done state, "Matches the PDF", the won figure.
- **Deadline** (`#ff9486` on indigo, `#c4262b` on paper): a running deadline only.

### Neutral
- **Paper** (`#f3f4f8`): the sheet, the selected tab, the primary button fill.
- **Slip** (`#fafbfd`): one step whiter than the sheet: the quoted clause, hover rows, the checked option, records on data health.
- **Ink / Soft / Muted** (paper: `#14172b` / `#363b58` / `#565c78`; indigo: `#eef0fb` / `#b3b8e0` / `#a9afdb`): text ladder.
- **Line / Edge** (paper: `#d5d8e6` / `#8b90ab`; indigo: `#33367a` / `#6a70b8`): hairlines and stronger borders.
- **Lit** (`rgba(255,184,77,.16)` on indigo, `#fbe6cf` on paper): the linked-evidence highlight.
- **Map surface / coast** (`#e4e6f0` / `#c4c8da`): the locator map only.

### Named Rules
**The Red Is a Clock Rule.** Red (`--deadline`) appears only for a running deadline: the rail countdown, the strip's clock icon, the phone bar subline. When the clock closes it goes muted; it never marks errors or emphasis.

**The Marigold Money Rule.** Marigold on the rail figure and the urgent incident only (the selected tab and a tab whose notify deadline is running), plus money at risk under a hover or an active highlight; it turns mint when the money is saved. Bars, map marks and healthy-status dots are neutral. On paper the same roles use the darker Amber and Mint.

**The Two Sets Rule.** Components read the ink and line tokens instead of hard-coding colours, so they work on both the indigo and the paper.

## Typography

**Display Font:** Anek Latin Variable (wdth axis; falls back to Hanken Grotesk, system-ui)
**Body Font:** Hanken Grotesk Variable (weight axis; falls back to system-ui)
Both are self-hosted via `@fontsource-variable`; no runtime font requests.

**Character:** a wide, confident display (width 112%) for headings and the big figure, a plain humanist sans for reading. Figures use a narrower width (90%).

### Hierarchy
Sizes are rem; at 1440 wide, 1rem = 15 px.
- **Figure** (600, `--t4` 2.95rem = 44 px, 1.1): the rail rupee figure, Anek wdth 112, tabular.
- **Headline** (600, `--t3` 1.5rem = 22.5 px, 1.25): incident title, big shipment count, feed percentages, clock digits.
- **Title** (600, `--t2` 1.15rem = 17 px, 1.5): stage names, section headings, group summaries, button text.
- **Body** (400, `--t2` 1.15rem, 1.5): the agent's statements and quoted clauses.
- **Label** (400/600, `--t1` 1rem = 15 px): metadata, tables, tooltips, gates, notes. This is the floor.
- **Figure-small** (600, 1rem, wdth 90): strip amounts, stage numbers, log timings.

### Named Rules
**The Text Floor Rule.** No text under `--t1`: 15 px at 1440 and above (it scales up), 14 px on phones.

**The Tabular Rupee Rule.** Every rupee figure, time, count and duration uses tabular, lining numerals (`.num` or the explicit font-variant). Rupees use Indian grouping and a non-breaking space after "Rs" (`web/lib/format.ts`: `Rs 1,73,622`, `Rs 1.74 L`, `Rs 13.87 Cr`; under a lakh stays in full).

## Layout

**Scaling rule.** `html { font-size: clamp(15px, 100vw / 96, 24px) }`. The root is 1/96 of the viewport: 15 px at 1440, 20 px at 1920, capped at 24 px (2304 and up). Every size is in rem, so 1920 and 2560 render the 1440 composition, only larger. Phones (760 px and under) fix the root at 14 px.

- Shell: `.wrap` max 96rem, side padding `--frame` (1.5rem; .86rem on phones).
- Case grid: left column (strip above sheet) and a right rail of `--rail-w` 21.5rem, column gap `--gap` 2.25rem. The rail is sticky (top 1.6rem) and sits straight on the indigo, with no box.
- Strip: six equal columns, largest incident first. The selected tab is paper and runs 2 px into the sheet with concave SVG fillets, so there is no seam; the first and last tab drop the outer fillet.
- Sheet: four stages in an 8.8rem label column plus content, separated by top hairlines. Stages not yet reached in a replay dim.
- Breakpoints: 960 px (header wraps) and 760 px (phone). On phones the grid is one column, the strip is 2 columns, stages stack, and the rail moves below the sheet. A fixed bottom bar carries the figure, countdown and Approve while the rail's own Approve is out of view.
- Rhythm: steps of about .13rem; common values .27, .4, .53, .67, .8, .93, 1.33rem.

## Elevation & Depth

Flat by default, with exactly one shadow: the sheet (`0 1.47rem 2.93rem -1.2rem rgba(4,5,24,.7)`). Depth elsewhere is tonal: indigo ground, raised indigo, paper, slip. The tooltip is a solid ink block, no shadow.

### Named Rules
**The One Shadow Rule.** Only the sheet casts a shadow. Inside the sheet, depth is a hairline plus the slip tone.

## Shapes

Small radii: .2rem (inline highlights), .4rem (tabs, sheet, cards, map, tooltips), .53rem (buttons, options, segmented control), .93rem (switch). Bars are .2rem high with rounded ends. A selected option is a 1px agent border plus a matching 1px inset outline, not a thicker border.

## Components

### Buttons
- **Primary** (Approve): paper fill, ground text, `.53rem` radius, 2.93rem high, full width in the rail. Hover goes white with a faint `0 0 0 .2rem rgba(238,240,251,.18)` ring. Disabled: transparent, line border, muted text, `not-allowed`.
- **Secondary** ("Watch the agent work", Skip, replay and discard actions): transparent, edge border, ink text, 2.67rem high. Hover: ink border, 12% edge tint.
- **Quiet link**: underlined muted text with a line-colour underline; hover or current page goes ink with a full underline; current page is 600.

### Incident strip tab
Name, status icon, amount and a risk bar. Status icon: clock (deadline), hourglass (pending approval), check (done). Done turns amount and bar mint. Hover: raised indigo fill. Selected: paper, square bottom, filleted joins.

### Sheet stages
Stage label at left ("Caught", "Priced", "Contract", "Decision", each with the agent step's time), the agent's plain statement, then the artifact: locator map, customer breakdown, clause slip, option rows. Hairlines separate stages.

### Fix options
Radio rows (circle, title, "Saves Rs X, costs Rs Y"). Hover: edge border and slip fill. Checked: agent border, inset outline, slip fill. Disabled when locked (finished, busy or awaiting approval).

### Rail (decision)
Order: At risk figure (marigold, mint once won), deadline clock row, recommended fix, preview (before to after), the agent's reason, the gate sentence, Approve. After approval: a mint done line and a next-case button.

### View as (segmented radio group)
Asha, Kiran, Meera. The selected segment is raised indigo, 600. Arrow keys move and select; the choice is kept in a cookie and the page refreshes server-side. Busy state: `aria-busy` and 80% opacity.

### Tooltip
Ink block with paper text, max 17rem, placed above the point or flipped below, and shifted left or right near the edges.

### Switch, tables and rows
The Outage toggle is a pill switch (ink when on). Data health feed rows, dead-letter groups and audit log rows are hairline lists; hover fills with slip. The audit filter is a segmented control (checked is ink on paper).

## Interactions

All from the built code.
- **Replay of the real agent run.** "Watch the agent work" replays the recorded steps one per second, revealing stages as their step arrives; "Skip to the result" ends it. It plays once per case per browser session (sessionStorage; manual if storage is off) and not under reduced motion.
- **Linked evidence.** Hovering or focusing a customer lights the matching clause, lane and truck; hovering a clause lights the customers it drives. The highlight is the Lit token.
- **Interactive locator map.** Warehouse pins toggle their lanes (pressed state); trucks show a tooltip on hover and focus, with driver or shipment wording by role.
- **Shipment list and role masking.** The shipment count expands a table. Kiran sees masked phones; Meera sees no driver data and no desk number. Masked cells are muted.
- **Live fix preview.** Choosing another fix updates the rail: "Rs X at risk to Rs Y after this fix, costs Rs Z" (the "to" figure is mint when it saves, marigold when unchanged).
- **Approval gate.** The gate sentence states why Approve is on or off: over or under the Rs 5 L limit, role cannot act, outside the DC scope, or waiting for Asha.
- **Live data.** The tower is polled every 15 s while the tab is visible; the scenario clock ticks each second from the server's time.

## Motion
- Easing `--ease: cubic-bezier(.2, .8, .2, 1)`. Hover and colour changes 150-200 ms; chevrons rotate 90 degrees in 200 ms; stage reveal fades in 450 ms; money fades in 350 ms; rail figure colour change 500 ms.
- Replay log rows rise .27rem over 350 ms; the current step has a pulsing dot (1 s).
- `prefers-reduced-motion: reduce` sets every transition and animation to none and skips the auto replay.

## Accessibility
- Visible focus everywhere: `outline: 2px solid var(--focus); outline-offset: 2px` (ink on indigo, agent indigo on paper).
- Roles and state: the strip is a labelled nav with `aria-current`; View as is a radiogroup with roving tabindex; fix options are a radiogroup; disclosure buttons use `aria-expanded` and `aria-controls`; map pins are `role="button"` with `aria-pressed`; trucks are focusable images with labels; errors use `role="alert"`; replay and triage use `aria-live="polite"`. The phone bottom bar is `aria-hidden` because the rail's controls are the real ones.
- Status never relies on colour alone: clock, hourglass and check icons, plus words.
- Text floor 15 px desktop and 14 px phones. Screenshots checked at 375, 1440, 1920 and 2560 (`web/.shots/v5/`). Contrast targets AA per PRODUCT.md; this record did not re-measure ratios.
- Tooltips open on keyboard focus, not hover only.

## Do's and Don'ts

### Do:
- **Do** write every size in rem so the composition scales with the viewport.
- **Do** keep text at or above `--t1`.
- **Do** use red only for a running deadline, and marigold (on the indigo) only for money at risk.
- **Do** show the agent's real step counts and milliseconds, and state why a control is disabled.
- **Do** show a fix's result as before and after in the rail preview.
- **Do** keep one shadow (the sheet); use hairlines and the slip tone inside it.
- **Do** use the token variables for text and lines so a component works on both indigo and paper.

### Don't:
- **Don't** add a second shadow, a box around the rail, or a full-bleed map console.
- **Don't** use red for errors; errors are an ink note with an edge border.
- **Don't** shrink text below the floor to fit; reflow instead.
- **Don't** size geometry from raw viewport maths; anchor to content and rem.
- **Don't** request external fonts or scripts at runtime.
- **Don't** fake streaming or hide latency, fallbacks or role limits.

## Divergences carried by the build (not canonized)
- Map labels are sized in SVG viewBox units (15.4 px in the viewBox), so they scale with the map, not with rem. A defect, not a rule for new text.
- The selected incident tab uses literal paper-set hex values instead of tokens. Do not copy this elsewhere.
- The direction contract sits in a hidden element at the top of `<body>`; a build workaround, not a component.
