# Data-source and provider policy

Taffy integrates optional services to provide community features. An integration is not an endorsement, partnership, or guarantee of data accuracy. Operators must obtain their own credentials and comply with each provider's terms, rate limits, attribution rules, and regional requirements.

| Capability | Typical source | Configuration | Behaviour when unavailable |
| --- | --- | --- | --- |
| QQ events | A OneBot V11 implementation | `ONEBOT_V11_*` | Bot cannot receive/send events until the connection is healthy. |
| Conversational responses | Operator-selected compatible model provider | `TAFFY_*`, optional provider-specific keys | Feature returns a clear failure response; it must not expose credentials. |
| CS2 market data | SteamDT and configured market services | `STEAMDT_API_KEY` and feature settings | Cached/validated data may be used within its policy; otherwise state is unavailable. |
| Official CS2 news | Valve public news interface | No model fallback | Render structured source fields only. |
| Esports schedules/results | PandaScore, when configured | `PANDASCORE_TOKEN` | Omit incomplete or unconfigured data. |
| Inventory/inspection metadata | Configured upstream services | Feature-specific settings | Respect budgets and return an explicit unavailable state. |

## Rules for contributors

- Do not bypass rate limits, authentication, robots restrictions, or terms of service.
- Do not replace a failed factual data request with a model-generated number.
- Validate response schema, freshness, and bounds before persistence or display.
- Add tests for provider failure, malformed responses, and quota exhaustion when adding an integration.
- Keep credentials in the operator's secret store or `.env`, never in source, tests, examples, or screenshots.

Market information can be delayed or incorrect. It is not investment or trading advice.
