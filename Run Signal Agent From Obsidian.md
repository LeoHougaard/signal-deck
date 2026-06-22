# Run Signal Agent From Obsidian

Install an Obsidian shell-command community plugin, then create a command with this PowerShell target:

```powershell
powershell.exe -ExecutionPolicy Bypass -File "C:\Users\Leo\OneDrive\Projects\Home Bases\scripts\run_signal_agent.ps1" -Vault "C:\Users\Leo\OneDrive\Projects\Home Bases"
```

Use it whenever you want to import `Idea Inbox` notes, refresh categories, rebuild related links, and update Signal Deck.
