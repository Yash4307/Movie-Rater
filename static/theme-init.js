/* Applies a logged-out visitor's saved theme before first paint (kept as a file so the CSP can forbid inline scripts). */
try { document.documentElement.dataset.theme = localStorage.getItem('cr-theme-guest') || 'dark'; } catch (e) { /* private mode */ }
