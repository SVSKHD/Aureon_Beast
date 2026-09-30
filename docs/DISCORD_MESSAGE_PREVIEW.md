# Discord message preview

Use `/test` to preview the production notification layout using synthetic XAUUSD
examples. It shows bullish (green), bearish (red) and neutral (yellow) cards.

- `/test` — all three cards, visible only to you.
- `/test style:bullish` — green card only.
- `/test style:bearish` — red card only.
- `/test style:neutral` — yellow card only.
- `/test style:all public:true` — post the previews in the current channel.

The usual Aureon user authorization applies. Every card is marked TEST. The command
does not read live prices, store detections, or attach execution buttons. It calls
the same notification renderer as real alerts, so future layout edits appear here.

After deployment, restart the Discord service (or the supervised Aureon stack) so
startup command synchronization registers `/test`. Guild-scoped synchronization
is used when `AUREON_DISCORD_GUILD_ID` is configured; otherwise global registration
may take time to appear in the client.
