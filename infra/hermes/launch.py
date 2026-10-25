"""Запуск Hermes gateway с надстройкой CenterInvest (ci_patch)."""
import sys

import ci_patch

ci_patch.apply()
from hermes_cli.main import main  # noqa: E402

sys.argv = ["hermes", "gateway", "run"]
sys.exit(main())
