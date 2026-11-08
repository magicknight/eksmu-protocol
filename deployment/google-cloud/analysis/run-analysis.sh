#!/usr/bin/env bash
# Run the Eksmu analysis queue. The platform model is chosen by the first key in
# the operator config (ANTHROPIC_API -> Haiku 5.5, OPENAI_API -> GPT-6 Luna,
# DEEPSEEK_API -> DeepSeek Flash). The worker key seals author-supplied keys.
set -euo pipefail
R="${EKSMU_ROOT:-$HOME/.local/share/eksmu-public-alpha-20261007}"
C="${EKSMU_CONFIG_DIR:-$HOME/.config/eksmu}"
[ -s "$C/config.yml" ] || { echo "no operator config; nothing to do"; exit 0; }
cd "$R/app"
exec "$R/venv/bin/python" -m eksmu --db "$R/data/community.sqlite" run-analysis-queue \
  --config "$C/config.yml" --worker-key "$C/analysis-worker.key" --budget-usd "${EKSMU_ANALYSIS_BUDGET:-10}"
