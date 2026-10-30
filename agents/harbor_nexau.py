"""Keep Harbor's on-demand sandbox install on AHE's pinned NexAU version."""

import os
from pathlib import Path

from harbor.agents.installed.nexau import NexAU


class PinnedNexAU(NexAU):
    @property
    def _install_agent_template_path(self) -> Path:
        if os.environ.get("USE_BP_E2B", "True") == "True":
            return super()._install_agent_template_path
        return Path(__file__).with_name("install_nexau.sh.j2")
