/* global window, React */
/* tweaks-shim.jsx — minimal stand-ins for the Claude Design editor harness.
 *
 * The original tweaks-panel.jsx wired the prototype to the design tool's iframe
 * host (postMessage protocol, draggable floating panel). The real web app has no
 * such host, so useTweaks just holds local state and TweaksPanel renders nothing.
 * App.jsx still calls useTweaks() and <window.TweaksPanel>, so we keep the API.
 */

function useTweaks(defaults) {
  const [values, setValues] = React.useState(defaults);
  const setTweak = React.useCallback((keyOrEdits, val) => {
    const edits =
      typeof keyOrEdits === "object" && keyOrEdits !== null
        ? keyOrEdits
        : { [keyOrEdits]: val };
    setValues((prev) => ({ ...prev, ...edits }));
  }, []);
  return [values, setTweak];
}

// All panel controls become no-op renderers.
const Noop = () => null;

Object.assign(window, {
  useTweaks,
  TweaksPanel: Noop,
  TweakSection: Noop,
  TweakRow: Noop,
  TweakSlider: Noop,
  TweakToggle: Noop,
  TweakRadio: Noop,
  TweakSelect: Noop,
  TweakText: Noop,
  TweakNumber: Noop,
  TweakColor: Noop,
  TweakButton: Noop,
});
