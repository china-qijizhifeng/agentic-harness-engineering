# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Test the code agent loading from YAML configuration."""

import logging
import os
from datetime import datetime
from pathlib import Path


from nexau import Agent, AgentConfig
from nexau.archs.main_sub.execution.middleware.agent_events_middleware import AgentEventsMiddleware, Event

logging.basicConfig(level=logging.INFO)


def get_date():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def handler(event: Event) -> None:
    print(event)


def main():
    """Test the code agent with YAML-based agent configuration."""
    print("Testing Code Agent (YAML-based)")
    print("=" * 60)

    try:
        # Load agent from YAML configuration
        print("Loading Code agent from YAML configuration...")

        script_dir = Path(__file__).parent
        config = AgentConfig.from_yaml(config_path=script_dir / "code_agent.yaml")
        event_middleware = AgentEventsMiddleware(session_id="test", on_event=handler)
        if config.middlewares:
            config.middlewares.append(event_middleware)
        else:
            config.middlewares = [event_middleware]

        claude_code_agent = Agent(config=config)
        print("✓ Agent loaded successfully from YAML")

        sandbox = claude_code_agent.sandbox_manager.instance
        if sandbox is None:
            print("✗ Sandbox failed to start")
            return False
        print("✓ Sandbox constructed successfully: {}".format(sandbox.sandbox_id))

        print("\nTesting Code Agent...")
        user_message = input("Enter your task: ")
        # user_message = "read /Users/hanzhenhua/nexau/examples/code_agent/image.png and describe the image"
        print(f"\nUser: {user_message}")
        print("\nAgent Response:")
        print("-" * 30)

        response = claude_code_agent.run(
            message=user_message,
            context={
                "date": get_date(),
                "username": os.getenv("USER"),
                "working_directory": str(sandbox.work_dir if sandbox else os.getcwd()),
                "env_content": {
                    "date": get_date(),
                    "username": os.getenv("USER"),
                    "working_directory": str(sandbox.work_dir if sandbox else os.getcwd()),
                },
            },
        )
        print(response)

    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback

        traceback.print_exc()
        return False

    return True


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
