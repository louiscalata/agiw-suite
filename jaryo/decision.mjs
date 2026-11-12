/** Owner decision, 2026-11-11. Change only with a new recorded owner decision. */
export const CHOICE_FILE = {
  "name": "jaryo-lab-choices.json",
  "sha256": "6490e5b15b624b82703d2ce897054c2e9a40f6c8dcc5d389de45e86d7004be2d"
};
export const CHOSEN = {
  "F01": "F01.01",
  "F02": "F02.04",
  "F03": "F03.03",
  "F04": "F04.01",
  "F05": "F05.02",
  "F06": "F06.02",
  "F07": "F07.01",
  "F08": "F08.10",
  "F09": "F09.01",
  "F10": "F10.01",
  "F11": "F11.01",
  "F12": "F12.10",
  "F13": "F13.02",
  "F14": "F14.02",
  "F15": "F15.01",
  "F16": "F16.01",
  "F17": "F17.01"
};
export const OVERRIDES = {
  "F17": {
    "choice": "F17.01",
    "date": "2026-11-11",
    "reason": "overridden by owner: booklet red",
    "light": "#C21A27",
    "dark": "#C21A27",
    "forcedColors": "system colors (unchanged)",
    "accentInk": "#FFFFFF",
    "scope": "accent only; preserve the remaining F17 color tokens"
  }
};
export const IMPLEMENTATION = {
  "palette": {
    "dark": {
      "bg": "#0B1012",
      "surface": "#141A1C",
      "text": "#F4F1EA",
      "muted": "#9AA3A8",
      "border": "#5A676C",
      "focus_ring": "#E8D7A8",
      "selected_bg": "#F4F1EA",
      "selected_fg": "#0B1012",
      "accent_identity": "#C21A27",
      "accent_ink": "#FFFFFF",
      "status_ok": "#8FC4A0",
      "status_run": "#8FB4D6",
      "status_blocked": "#FFD2C8",
      "status_blocked_bg": "#3A2020",
      "status_unknown": "#9AA3A8",
      "sel_sub": "#3B4448",
      "sel_blocked": "#6B2A1E",
      "sel_run": "#1F4A72"
    },
    "light": {
      "bg": "#FFFFFF",
      "surface": "#FFFFFF",
      "text": "#000000",
      "muted": "#000000",
      "border": "#000000",
      "focus_ring": "#000000",
      "selected_bg": "#000000",
      "selected_fg": "#FFFFFF",
      "accent_identity": "#C21A27",
      "accent_ink": "#FFFFFF",
      "status_ok": "#000000",
      "status_run": "#000000",
      "status_blocked": "#000000",
      "status_blocked_bg": "#FFFFFF",
      "status_unknown": "#000000",
      "sel_sub": "#FFFFFF",
      "sel_blocked": "#FFFFFF",
      "sel_run": "#FFFFFF"
    }
  },
  "patterns": {
    "layout": "main-bus-spine",
    "naming": "human-label-technical-subline",
    "navigation": "dependency-step-pager",
    "selection": "invert-and-declared-neighbours",
    "inputs": "port-socket-and-binding-list",
    "container": "ported-box",
    "table": "mono-ledger",
    "unknown": "dashed-frame-hatch-word",
    "state": "word-and-glyph",
    "empty": "reason-and-one-next-step",
    "inspector": "reserved-column-or-modal-sheet",
    "hidden": "underground-dash-auto-open-attention",
    "activity": "event-tick-reset-per-run",
    "responsive": "thumb-bar",
    "hero": "install-line-and-gui-connect",
    "commands": "capability-gated-combobox",
    "accentPlacement": "brand-and-one-primary-action"
  },
  "interaction": {
    "phoneMax": "700px",
    "desktopMin": "1100px",
    "inspectorMin": "340px",
    "inspectorMax": "420px",
    "thumbBar": "56px",
    "targetMin": "44px",
    "unknownHatch": "12px",
    "focusWidth": "3px",
    "focusOffset": "3px",
    "tickCapacity": 8,
    "motion": "0ms"
  },
  "familyOptions": {
    "F01": "Main Bus Spine",
    "F02": "Human Label + Technical Subline",
    "F03": "Step Pager",
    "F04": "Step Focus + Neighbours",
    "F05": "Port Socket",
    "F06": "Ported Box",
    "F07": "Mono Ledger Table",
    "F08": "Unknown-aware Graph",
    "F09": "Word+Glyph State",
    "F10": "Honest Empty",
    "F11": "Side Inspector",
    "F12": "Underground Dash",
    "F13": "Event Tick",
    "F14": "Thumb Bar",
    "F15": "Install Line Hero",
    "F16": "Command Palette",
    "F17": "Copper Workshop"
  },
  "choiceScope": "jaryo-kit-and-full-observer-web",
  "motion": "event-only"
};
