# MangaShelf — working rules

## UI / styling work
- **Always run the `impeccable` skill** (via the Skill tool) before building or restyling any UI: new screens, buttons, menus, settings rows, popovers, cards. Don't style by eye.
- Reuse the app's existing patterns first (`.btn`, `.btn.lg`, `.btn.primary`, `.dd-menu` / `.dd-item`, `.settings-toggle-row`, `.settings-lib-row`, `.card-action-btn`, `Toggle`, `Dropdown`). A new control that doesn't look like a sibling control is a bug.
- Interactive rows must look like buttons (surface, border, hover state), not bare text.
- After UI changes: `cd web && npm run build`, bump the `?v=` token in `web/static/index.html`, and look at the rendered result.

## Tablet is a first-class target
- The owner reads on a Galaxy Tab S2 (MangaShelf wrapper app, Android WebView). Its CSS viewport is **768×1024 portrait / 1024×768 landscape** (1536×2048 at density 2) — neither "phone" (≤640px) nor "desktop" (≥1280px). Touch, no hover, no keyboard.
- Every UI change must be checked at **375 (phone), 768 and 1024 (tablet), 1440 (desktop)**, on every screen the change touches (Home, Library, Favorites, Downloaded, Detail, Profile). Measure, don't eyeball: no element clipped past the viewport edge, no horizontal scroll (`scrollWidth <= clientWidth`), controls reachable.
- The 641–1240px band is the tablet band. Desktop-only layouts (the top bar is ~1200px wide) must wrap or collapse there; phone rules (`max-width: 640px`) must not be the only responsive rule. Put tablet rules in an explicit `@media (max-width: 1240px) and (min-width: 641px)` block.
- Tap targets ≥ 40px on touch; don't hide controls behind hover.
- Test with the isolated server (port 8001, temp HOME, fake `hiddenTags` in `.mangashelf/web_settings.json`) — the real library is password-locked. Reset the viewport afterwards.
