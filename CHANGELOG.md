# Changelog

This log starts with the open source documentation and branding work. Earlier
feature milestones remain in [future-plan.md](future-plan.md), and the complete
commit history records earlier changes. Versions follow the repository rule:
each focused change advances the runtime version by exactly `0.1`.

## v4.2 — 2026-10-21

- Extracted reusable HTML report styles and chart code into tracked
  `leak_reports/frames/` assets, preserving self-contained offline reports.
- Kept generated and local fixture/demo artifacts ignored.

## v4.1 — 2026-10-21

- Filtered the interactive program list on each typed character and
  Backspace, with Enter selecting a highlighted name match or exact PID.
- Preserved arrow navigation within matches, the 15-row cap, explicit
  refresh/commands, and the non-terminal line-input workflow.

## v4.0 — 2026-10-21

- Added live-query parsing for names, PIDs, and explicit searches while
  keeping incomplete picker commands separate from the active filter.

## v3.9 — 2026-10-21

- Kept keyboard-picker input and notices on single lines so long pasted
  text or an invalid PID cannot hide the prompt in small terminals.

## v3.8 — 2026-10-21

- Enabled Up/Down navigation and Enter selection in the interactive program
  list, with automatic page changes, visible focus, and typed PID/search input.
- Added Escape cancellation and preserved the line-input fallback; arrow
  and PID selection share the same process identity checks.

## v3.7 — 2026-10-21

- Added immediate Windows picker-key decoding for arrows, Enter, text,
  backspace, and Escape, with non-terminal input detection and EOF handling.

## v3.6 — 2026-10-21

- Limited program-list pages to 15 PIDs and added an active-row rendering
  marker for keyboard navigation, retaining smaller pages in short terminals.

## v3.5 — 2026-10-21

- Kept process rows visible in small terminals with compact table/help
  output and single-line search labels.

## v3.4 — 2026-10-21

- Reset supported psutil inventory caches before refreshing so a reused
  PID cannot keep displaying the previous program's name and identity.

## v3.3 — 2026-10-21

- Replaced Hook's manual PID prompt with the searchable program list and
  documented search, paging, refresh, and selection behavior.
- Kept direct CLI/headless commands and safe detach behavior unchanged.

## v3.2 — 2026-10-21

- Added keyboard search, PID selection, paging, refresh, and clear/cancel
  commands to the process picker, including stale-process recovery.

## v3.1 — 2026-10-21

- Added name/PID filtering and bounded process-list pages with literal,
  encoding-safe labels and clear empty-result states.

## v3.0 — 2026-10-21

- Added read-only running-process discovery with sorted names/PIDs and
  creation-time checks for stale or reused selections.

## v2.9 — 2026-10-21

- Connected startup to the Open a program / Hook to a running program
  menu, using the existing file picker and safe PID attachment paths.
- Preserved direct CLI/headless workflows and documented startup choices.

## v2.8 — 2026-10-21

- Added the running-program PID prompt with Task Manager guidance,
  positive-ID validation, cancellation, and detach behavior explained.

## v2.7 — 2026-10-21

- Added the startup choice component for opening a program or hooking to
  a running program, with keyboard validation and cancellation handling.

## v2.6 — 2026-10-21

- Appended a phased roadmap with actionable tasks and completion criteria
  for reliable checks, accessible workflows, developer integrations, and
  optional local and team services.
- Preserved delivered roadmap history; runtime behavior is unchanged.

## v2.5 — 2026-10-21

- Revised the README masthead to show blue water escaping from a crack in a
  straight pipe, matching the compact identity.
- Updated the banner edit brief, accessible description, and license badge color.

## v2.4 — 2026-10-21

- Revised every compact logo, avatar, wordmark, and social card to show a
  straight industrial pipe leaking blue liquid through a visible crack.
- Updated light/dark SVG and PNG variants and the brand palette guidance.

## v2.3 — 2026-10-21

- Rebuilt the README with the new identity, a real dashboard capture, a source
  setup path, concise workflows, and links to the full documentation.
- Added this changelog. Runtime behavior is unchanged by this documentation work.

## v2.2 — 2026-10-21

- Added the illustrated README masthead and its generation brief.
- Documented the visual identity, palette, typography, asset formats, and usage.

## v2.1 — 2026-10-21

- Expanded the existing working-principle reference and added a developer guide.
- Clarified bounded report history, warm-up peaks, and configuration details.
- Preserved delivered roadmap milestones and added discussion topics.

## v2.0 — 2026-10-21

- Introduced the pipe, droplet, and tracing-reticle identity.
- Added editable SVG and PNG marks, light/dark wordmarks, avatars, and a social card.

## v1.9 — 2026-10-21

- Added dedicated usage, configuration, and report/comparison guides.
- Documented process ownership, output handling, headless outcomes, and limitations.

## v1.8 — 2026-10-21

- Added contribution, support, security, and conduct documentation.
- Added GitHub bug/feature forms and a pull request template.
