# AI Usage Dashboard

This is an independent Omarchy bar plugin. It reads the existing local provider usage records and adds a separate Codex read-only app-server collector for rate windows and reset-credit grants. It does not change the built-in Agents widget or provider records. The built-in record's `updatedAt` can be a collection scan time rather than the provider's actual sample time, so a displayed fresh age means “record last checked,” not a guaranteed provider-side sample timestamp.

## Requirements

- Omarchy with the shell bar, and its built-in AI agents usage records (`~/.local/state/omarchy/agents/usage/`) for Claude and Codex.
- `python3` (standard library only).
- Optional: the `codex` CLI, signed in, for Codex rate windows and reset credits. The collector starts its app-server with the experimental API enabled.

## Install

The plugin directory must be named `icedeep.ai-usage`:

```sh
git clone https://github.com/Dair2Know/omarchy-ai-usage.git ~/.config/omarchy/plugins/icedeep.ai-usage
```

Then add `{"id": "icedeep.ai-usage"}` to a section of `bar.layout` in `~/.config/omarchy/shell.json`.

Left-click opens the dashboard, right-click refreshes, and middle-click switches provider. Press `R` in the open panel to refresh.

## Provider extension contract

Place a JSON file in `~/.config/omarchy/ai-usage-monitor/providers.d/`. A provider record may contain:

```json
{
  "schemaVersion": 1,
  "id": "example-ai",
  "name": "Example AI",
  "updatedAt": "2026-10-14T12:00:00Z",
  "source": "Example account usage",
  "subscriptionUpdatedAt": "2026-10-14T12:00:00Z",
  "subscription": {
    "plan": "Example Premium",
    "status": "active",
    "periodEndsAt": "2026-11-14T12:00:00Z",
    "periodSourceAt": "2026-10-14T12:00:00Z",
    "source": "Example account settings"
  },
  "limits": [
    {
      "label": "Weekly",
      "percent": 0.42,
      "resetsAt": "2026-10-20T12:00:00Z",
      "windowDurationMins": 10080
    }
  ],
  "supplementalUpdatedAt": "2026-10-14T12:00:00Z",
  "supplemental": {
    "bankedResets": {
      "availableCount": 1,
      "detailsAvailable": true,
      "details": [
        {"title": "Reset credit", "grantedAt": "2026-10-01T12:00:00Z", "expiresAt": null, "expiresAtKnown": true}
      ],
      "source": "Example account usage",
      "sourceAt": "2026-10-14T12:00:00Z"
    },
    "nextFreeResetAt": {"at": null, "status": "not_reported"}
  }
}
```

`percent` is a fraction from 0 to 1. Give each window a real reset timestamp and, where known, `windowDurationSeconds` or `windowDurationMins`; a bounded explicit duration in the label is a fallback. A missing or expired reset does not get guessed. Pacing compares usage with elapsed wall-clock time from the reset window, assuming usage began at zero. `supplementalUpdatedAt` timestamps grant data independently from the usage sample. Grants are separate from purchased billing credits. Do not infer recurring free grants from grant dates; use `status: "not_reported"` when a source does not provide the next offer.

Subscription fields are optional and source-reported. `subscriptionUpdatedAt` tracks plan metadata separately from usage; `periodEndsAt` is shown as a saved paid-period end, not as a cancellation or renewal date. `periodSourceAt` records when the period date itself was checked. The panel never infers price, renewal, cancellation, or active status from a plan name or period end.

Providers with `ready: false` and no reported limits, balance, known grants, or local activity are treated as unconfigured and omitted. Claude and Codex remain visible with an explicit no-data state when their records are absent.

## Codex source and privacy

The optional Codex collector runs at most every 15 minutes while the panel is loaded, with manual refresh available. It starts `codex -s read-only -a on-request app-server`, requests experimental API initialization, then reads only `account/rateLimits/read`. It never sends a reset-credit redemption/consumption request. Its companion record is `~/.local/state/ai-usage-monitor/codex.json`; the file is written with owner-only permissions. Opaque account and grant IDs are not stored. Available grant details are capped and expired grants are marked in the panel. A failed refresh retains the last successful timestamp and data, and reports the latest failure separately.

Codex reset-credit fields follow the account rate-limit response schema. Claude's available OAuth usage response does not report banked reset grants or a next free reset offer, so those fields remain “Not reported by provider.” A grant count is labeled with its last provider-read timestamp because a previously available credit can expire between refreshes.

For Codex's subscription section, the reader extracts only the saved subscription plan, period start, period end, and last-check timestamp from the local ID-token metadata in `~/.codex/auth.json` (or `$CODEX_HOME/auth.json`). Saved period dates are shown only when their plan matches the current provider-reported plan. It does not write or print the token. The period timestamp may be old; the panel shows its check time and does not treat the period end as a cancellation or renewal signal. No price or billing cadence is reported by this source.

Sources: [Claude limits and resets](https://support.claude.com/en/articles/17007452-what-is-a-limit-reset); [OpenAI pricing and plan limits](https://learn.chatgpt.com/docs/pricing); [OpenAI Codex changelog](https://learn.chatgpt.com/docs/changelog) (June 11 reset-credit announcement).

## Uninstall

Remove `{"id": "icedeep.ai-usage"}` from the bar layout in `~/.config/omarchy/shell.json`, then remove `~/.config/omarchy/plugins/icedeep.ai-usage`. The Codex companion record and its lock file can be removed from `~/.local/state/ai-usage-monitor/` if desired.
