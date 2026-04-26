# 🤖 Core Concepts: Agents

Agents are the central orchestrators in the NexAU framework. They combine an LLM, a system prompt, and a set of tools to accomplish tasks. You can define agents either programmatically in Python or declaratively using YAML files.

## Programmatic Agent Creation

Creating an agent in Python gives you maximum flexibility. This is ideal for dynamic setups or when integrating into existing applications.

**Example from `examples/deep_research/quickstart.py`:**

```python

import os
from datetime import datetime
from nexau import Agent, AgentConfig, Tool, LLMConfig
from nexau.archs.tool.builtin.web_tools import google_web_search, web_fetch

def main():
    # Create tools from YAML configurations
    web_search_tool = Tool.from_yaml("tools/WebSearch.yaml", binding=google_web_search)
    web_read_tool = Tool.from_yaml("tools/WebRead.yaml", binding=web_fetch)

    # Configure the LLM
    llm_config = LLMConfig(
        model=os.getenv("LLM_MODEL"),
        base_url=os.getenv("LLM_BASE_URL"),
        api_key=os.getenv("LLM_API_KEY")
    )

    # Create the agent instance
    agent_config = AgentConfig(
        name="research_agent",
        tools=[web_search_tool, web_read_tool],
        llm_config=llm_config,
        system_prompt="You are a research agent. Use web_search and web_read tools to find information.",
    )
    research_agent = Agent(config=agent_config)

    # Run the agent
    response = research_agent.run(
        "What's the latest news about AI developments?"
    )
    print(response)

if __name__ == "__main__":
    main()
```

## YAML-Based Agent Configuration

For a more declarative approach, you can define an agent's entire configuration in a YAML file. This makes it easy to manage and version different agent personalities and capabilities.

1.  **Create an agent configuration file:**

    **File: `agents/my_agent.yaml`**
    ```yaml
    type: agent
    name: my_research_agent
    max_context_tokens: 100000
    system_prompt: |
      Date: {{date}}. You are a research agent specialized in finding and analyzing information.
      Use web_search to find relevant information, then web_read to get detailed content.
    system_prompt_type: string
    llm_config:
      temperature: 0.7
      max_tokens: 4096
    tools:
      - name: web_search
          yaml_path: ./tools/WebSearch.yaml
        binding: nexau.archs.tool.builtin.web_tools:google_web_search
      - name: web_read
          yaml_path: ./tools/WebRead.yaml
        binding: nexau.archs.tool.builtin.web_tools:web_fetch
    sub_agents: []
    ```

2.  **Load and use the agent in Python:**

    ```python

    import os
    from datetime import datetime
    from nexau import Agent, AgentConfig, LLMConfig
    from pathlib import Path

    def main():

        agent_config = AgentConfig.from_yaml(
            Path("agent/my_agent.yaml")
        )
        agent_config.llm_config = LLMConfig(
            model = os.getenv("LLM_MODEL"),
            base_url = os.getenv("LLM_BASE_URL"),
            api_key = os.getenv("LLM_API_KEY"),
        )

        # Load the agent from agent_config
        agent = Agent(
            config=agent_config
        )

        # Use the agent
        response = agent.run(
            "Research the latest developments in quantum computing",
            context={"date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        )
        print(response)

    if __name__ == "__main__":
        main()
    ```
