// Applies the saved theme before first paint to avoid a flash. An external file, because the CSP forbids inline scripts.
try { if (localStorage.getItem('ci.theme') === 'dark') document.documentElement.dataset.theme = 'dark' } catch (e) {}
