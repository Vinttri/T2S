import { createRoot } from "react-dom/client";
import App from "./App.tsx";
import "./index.css";

// Initialize theme on page load
try {
  const savedTheme = localStorage.getItem("theme");
  // Normalize: only accept "light" or "dark", default to "dark"
  const theme = (savedTheme === "light" || savedTheme === "dark") ? savedTheme : "dark";
  document.documentElement.setAttribute("data-theme", theme);
  // Update localStorage if we normalized the value
  if (savedTheme !== theme) {
    localStorage.setItem("theme", theme);
  }
} catch {
  document.documentElement.setAttribute("data-theme", "dark");
}

// One-time reset of legacy settings keys so the new defaults apply (Memory
// Context OFF by default, rules from the database). Bumped to v3 to also clear
// any persisted memory=true from the old ON-by-default builds.
try {
  if (localStorage.getItem("t2s_settings_migrated_v3") !== "1") {
    localStorage.removeItem("t2s_use_memory");
    localStorage.removeItem("t2s_use_rules_from_database");
    localStorage.setItem("t2s_settings_migrated_v3", "1");
  }
} catch {
  /* ignore storage errors */
}

const rootElement = document.getElementById("root");
if (!rootElement) {
  throw new Error("Failed to find the root element. Make sure index.html contains a div with id='root'.");
}
createRoot(rootElement).render(<App />);
