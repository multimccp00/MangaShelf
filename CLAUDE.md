# MangaShelf — working rules

## UI / styling work
- **Always run the `impeccable` skill** (via the Skill tool) before building or restyling any UI: new screens, buttons, menus, settings rows, popovers, cards. Don't style by eye.
- Reuse the app's existing patterns first (`.btn`, `.btn.lg`, `.btn.primary`, `.dd-menu` / `.dd-item`, `.settings-toggle-row`, `.settings-lib-row`, `.card-action-btn`, `Toggle`, `Dropdown`). A new control that doesn't look like a sibling control is a bug.
- Interactive rows must look like buttons (surface, border, hover state), not bare text.
- After UI changes: `cd web && npm run build`, bump the `?v=` token in `web/static/index.html`, and look at the rendered result.
