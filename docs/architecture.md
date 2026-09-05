# Target architecture

The product has two independent clients built on the same application core.

1. The personal Telegram bot is an owner-operated client. It scans sources,
   sends review drafts to the owner's Telegram account, and publishes approved
   posts to the owner's channel.
2. The public web application is a future multi-user SaaS. A user can review
   drafts in the browser without Telegram. Telegram and X are optional user
   integrations, not global application dependencies.

## Intended boundaries

```text
collectors -> application core/API -> projects and drafts
                         |
                         +-> personal Telegram adapter
                         +-> public web client
                         +-> per-user Telegram integration
                         +-> per-user X integration
```

The application core owns source ingestion, filtering, drafts, review state,
publication jobs, and audit history. Telegram, X, and the web UI call this core
through explicit services. They must not import each other's handlers or own
the canonical workflow state.

The first SaaS foundation is now present in the database: `User` owns one or
more `Workspace` records, projects and web settings carry `workspace_id`, and
Telegram/X credentials live in workspace-specific integration tables. The
current owner-only web app selects workspace `1` until authentication is
implemented; this is an explicit compatibility adapter, not the final SaaS
identity model.

## Free-first stage

- Run SQLite with one application instance.
- Run the web dashboard only on localhost.
- Keep the personal Telegram bot as the scanner and review client.
- Use AirdropAlert and selected public X mirrors as priority sources.
- Use Groq with the local fallback. Keep Gemini optional.
- Use Open in X while official X API access is unavailable.
- Do not accept public registrations or payments until authentication,
  tenant isolation, rate limits, publication idempotency, and backups exist.

## SaaS stage

The public launch requires users, personal workspaces, subscriptions,
integration credentials, source preferences, quotas, and audit logs. Every
project, draft, publication job, and credential must belong to exactly one
workspace. Production should move to PostgreSQL before multiple workers or
customers are enabled. Billing can be added after the free closed beta proves
retention.

An optional session layer is available through `/api/auth/register`,
`/api/auth/login`, `/api/auth/logout`, and `/api/auth/me`. Set
`WEB_AUTH_MODE=required` only after the frontend has a login flow; the default
`off` mode preserves the local MVP behavior.
