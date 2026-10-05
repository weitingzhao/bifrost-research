# Licence

This directory (`pine-runner/`) is licensed **AGPL-3.0-only**, unlike the rest of
bifrost-research. It links [PineTS](https://github.com/LuxAlgo/PineTS) (AGPL-3.0-only).
Full licence text: https://www.gnu.org/licenses/agpl-3.0.txt

It runs as a separate process and is reached only over HTTP (`POST /run`,
`GET /health`). Do not import it, or PineTS, into bifrost-research's Python code or
bifrost-trade-frontend.

"Pine Script" and "TradingView" are trademarks of TradingView, Inc. This project is
not affiliated with TradingView.
