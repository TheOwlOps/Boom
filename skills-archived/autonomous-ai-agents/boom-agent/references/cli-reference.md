# Boom CLI Reference

Live sources when anything looks stale: `boom --help`, `boom <command> --help`,
https://boom-agent.nousresearch.com/docs/reference/cli-commands

### Global Flags

```
boom [flags] [command]        (no subcommand = interactive chat)

  --version, -V             Show version
  -z, --oneshot PROMPT      One-shot: print ONLY the final response (for scripts/pipes)
  -m MODEL  --provider P    Model/provider override for this invocation
  -t, --toolsets LIST       Comma-separated toolsets for this invocation
  --resume, -r SESSION      Resume session by ID or title
  --continue, -c [NAME]     Resume by name, or most recent session
  --worktree, -w            Isolated git worktree mode (parallel agents)
  --skills, -s SKILL        Preload skills (comma-separate or repeat)
  --profile, -p NAME        Use a named profile
  --yolo                    Skip dangerous command approval
  --tui / --cli             Force the Ink TUI / classic REPL
  --ignore-rules            Skip AGENTS.md/SOUL.md/memory/skill injection
  --safe-mode               Disable ALL customizations (troubleshooting)
  --pass-session-id         Include session ID in system prompt
```

### Chat

```
boom chat [flags]
  -q, --query TEXT          Single query, non-interactive
  --image PATH              Attach a local image to a single query
  -Q, --quiet               Suppress banner, spinner, tool previews
  --checkpoints             Enable filesystem checkpoints (/rollback)
  --max-turns N             Cap tool-calling iterations
  --source TAG              Session source tag (default: cli)
```
(plus the global flags above)

### Configuration

```
boom setup [section]      Wizard (model|tts|terminal|gateway|tools|agent)
boom model                Interactive model/provider picker
boom fallback [add|remove|list]  Fallback provider chain
boom config [show|edit|get|set|unset|path|env-path|check|migrate]
boom login / logout       OAuth sign-in / clear stored auth
boom doctor [--fix]       Check dependencies and config
boom status [--full]      Component summary (--full: every section)
```

### Tools & Skills

```
boom tools [list|enable NAME|disable NAME]   Per-platform toolsets (curses UI with no args)

boom skills list|browse|search QUERY|inspect ID
boom skills install ID    Hub identifier OR a direct https://…/SKILL.md URL
boom skills config        Enable/disable skills per platform
boom skills check|update|uninstall|publish PATH
boom skills tap add REPO  Add a GitHub repo as a skill source
boom bundles              Skill bundles (one /<name> alias loads several skills)
```

### MCP Servers

```
boom mcp add NAME (--url or --command) | remove | list | test NAME
boom mcp catalog | install NAME     Curated catalog install
boom mcp configure NAME             Toggle tool selection
boom mcp serve                      Run Boom as an MCP server
```
Details (transport, tool discovery, catalog): `references/native-mcp.md`.

### Gateway (Messaging Platforms)

```
boom gateway run|install|start|stop|restart|status|setup
```

20+ platforms: Telegram, Discord, Slack, WhatsApp (Baileys + Business Cloud API), iMessage (Photon — `boom photon setup`), Signal, Email, SMS, Matrix, Mattermost, Teams, LINE, SimpleX, ntfy, Google Chat, Home Assistant, DingTalk, Feishu, WeCom, Weixin, API Server, Webhooks. Open WebUI connects via the API Server adapter. Most adapters ship under `plugins/platforms/`.
Docs: https://boom-agent.nousresearch.com/docs/user-guide/messaging/

### Sessions

```
boom sessions list|browse|rename ID TITLE|delete ID|export OUT|prune|stats
```

### Cron / Webhooks

```
boom cron list|create SCHED|edit ID|pause|resume|run ID|remove|status
    Schedules: '30m', 'every 2h', '0 9 * * *', ISO timestamp
boom webhook subscribe NAME|list|remove NAME|test NAME
```
Webhook payloads/routes: `references/webhooks.md`.

### Profiles

```
boom profile list|create NAME (--clone|--clone-all|--clone-from)|use|show|delete
boom profile rename A B | alias NAME | export NAME | import FILE
boom profile migrate-identity A B   Retry a completed rename's session/routing identity migration
```

### Credentials & Pools

```
boom auth                 Interactive credential manager
boom auth add [PROVIDER]  Add OAuth or API-key credential (nous, openai-codex, qwen-oauth, …)
boom auth list|remove P IDX|reset PROVIDER|status
```
Multiple credentials per provider form a pool that rotates automatically and skips exhausted keys.

### Other

```
boom desktop / gui        Native desktop app
boom dashboard            Web admin panel + embedded chat (--stop / --status)
boom proxy                OpenAI-compatible local proxy backed by an OAuth provider
boom portal               Quick setup / sign in via Nous Portal
boom kanban <verb>        Multi-agent work-queue board
boom project              Named multi-folder workspaces
boom skin list|use|set    Switch/tweak skins (see references/themes.md)
boom pets <verb>          Pet mascots (see references/petdex.md)
boom memory setup|status|off|reset   Memory provider
boom secrets bitwarden|onepassword   External secret stores
boom moa                  Mixture-of-Agents slots
boom hooks / security / backup / import / checkpoints / console
boom logs [-f] [errors]   View agent/error logs
boom send                 One-off message through a gateway platform
boom pairing / plugins / insights / journey / computer-use
boom acp                  ACP server (IDE integration)
boom completion bash|zsh|fish
boom update / uninstall / claw migrate
```

Plugin- and provider-supplied subcommands (e.g. `boom photon setup`) only appear once their plugin is installed/active.

### Where to Find Things

| Looking for... | Location |
|---|---|
| Config options | `boom config edit` · [Configuration docs](https://boom-agent.nousresearch.com/docs/user-guide/configuration) |
| Tools / toolsets | `boom tools list` · [Tools reference](https://boom-agent.nousresearch.com/docs/reference/tools-reference) |
| Skills catalog | `boom skills browse` · [Skills catalog](https://boom-agent.nousresearch.com/docs/reference/skills-catalog) |
| Provider setup | `boom model` · [Providers guide](https://boom-agent.nousresearch.com/docs/integrations/providers) |
| Env variables | `boom config env-path` · [Env vars reference](https://boom-agent.nousresearch.com/docs/reference/environment-variables) |
| Gateway logs | `~/.boom/logs/gateway.log` (or `boom logs`) |
| Sessions | `boom sessions browse` (reads state.db) |
